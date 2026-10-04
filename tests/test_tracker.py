"""State resolution, and the contextual reasoning that makes it possible."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ciscoyoke.stream.buffer import TAIL_WINDOW, StreamBuffer
from ciscoyoke.stream.signals import SignalKind, detect, first_anchored
from ciscoyoke.stream.tracker import Basis, Confidence, State, StateTracker
from ciscoyoke.transcript.fake import chunked


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        (b"\r\nRouter>", State.USER_EXEC),
        (b"\r\nRouter#", State.PRIV_EXEC),
        (b"\r\nRouter(config)#", State.CONFIG_MODE),
        (b"\r\nRouter(config-if)#", State.CONFIG_MODE),
        (b"\r\nswitch: ", State.BOOTLOADER),
        # Byte-for-byte what a real WS-C2950G-24-EI bootloader sends: LF *then*
        # CR. Every synthetic fixture used CRLF, and recovery timed out against
        # hardware sitting at this exact prompt.
        (b"\n\rswitch: ", State.BOOTLOADER),
        (b"\n\rRouter#", State.PRIV_EXEC),
        (b"\r\nrommon 1 > ", State.ROMMON),
        (b"\r\nrommon 17 > ", State.ROMMON),
        (b"\r\n--More-- ", State.PAGER),
        (b"\r\nUsername: ", State.LOGIN_USERNAME),
        (
            b"Would you like to enter the initial configuration dialog? [yes/no]: ",
            State.SETUP_DIALOG,
        ),
    ],
)
def test_prompts_resolve_to_states(stream: bytes, expected: State) -> None:
    tracker = StateTracker()
    tracker.feed(stream)
    assert tracker.current.state is expected
    assert tracker.current.basis is Basis.OBSERVED


def test_fatal_memory_self_test_is_a_conclusive_state() -> None:
    tracker = StateTracker()
    tracker.feed(
        b"System Bootstrap, Version 12.3(8r)YH6, RELEASE SOFTWARE (fc1)\r\n"
        b"Failed all 0x00000000 test\r\n"
        b"Bad RAM at location 0x80000000: wrote 0x00000000, read 0xFFFF0000\r\n"
        b"DDR memory test failed.  Resetting the router ...\r\n"
    )

    assert tracker.current.state is State.SELF_TEST_FAILURE
    assert tracker.current.basis is Basis.OBSERVED
    assert tracker.current.confidence is Confidence.HIGH
    assert tracker.current.evidence[0].kind is SignalKind.SELF_TEST_FAILURE


def test_config_prompt_beats_priv_exec() -> None:
    """``Router(config)#`` also satisfies the priv-exec pattern.

    The more specific reading has to win, or every config-mode device would be
    mistaken for a privileged prompt and commands would go to the wrong place.
    """
    signals = detect("\r\nRouter(config)#")
    anchored = first_anchored(signals)
    assert anchored is not None
    assert anchored.kind is SignalKind.CONFIG_PROMPT


def test_password_after_enable_is_an_enable_prompt() -> None:
    tracker = StateTracker()
    tracker.note_sent(b"enable\r")
    tracker.feed(b"\r\nPassword: ")
    assert tracker.current.state is State.ENABLE_PASSWORD
    assert tracker.current.basis is Basis.INFERRED


def test_password_without_enable_is_a_login_prompt() -> None:
    tracker = StateTracker()
    tracker.note_sent(b"\r")
    tracker.feed(b"\r\nPassword: ")
    assert tracker.current.state is State.LOGIN_PASSWORD


def test_password_on_a_cold_attach_is_low_confidence() -> None:
    """Attaching to a stream already at a prompt tells us the least.

    Nothing was sent, so there is no context to disambiguate with. The tool
    must say so rather than guess confidently.
    """
    tracker = StateTracker()
    tracker.feed(b"\r\nPassword: ")
    assert tracker.current.state is State.LOGIN_PASSWORD
    assert tracker.current.confidence is Confidence.LOW


def test_guard_outranks_a_later_prompt() -> None:
    """The recovery-disabled guard must not be escapable by seeing a prompt.

    This is what blocks destructive steps. If a bootloader prompt arriving
    afterwards could clear it, the tool's most important refusal would silently
    stop working.
    """
    tracker = StateTracker()
    tracker.feed(b"WARNING: PASSWORD RECOVERY FUNCTIONALITY IS DISABLED\r\n")
    assert tracker.current.state is State.DESTRUCTIVE_RECOVERY_GUARD

    tracker.feed(b"switch: ")
    assert tracker.current.state is State.DESTRUCTIVE_RECOVERY_GUARD


def test_prompt_inside_output_is_not_a_prompt() -> None:
    """A prompt-shaped string mid-output is evidence of nothing.

    ``Router#`` appears constantly inside captured configuration and pasted
    session logs. Only a prompt the device is *sitting at* -- at the end of the
    stream, with nothing after it -- means the device is waiting.
    """
    tracker = StateTracker()
    tracker.feed(b"\r\nRouter#show run\r\nBuilding configuration...\r\n")
    assert tracker.current.state is not State.PRIV_EXEC


def test_states_are_deduplicated() -> None:
    """A sustained state is reported when entered, not on every byte."""
    tracker = StateTracker()
    tracker.feed(b"\r\nRouter#")
    tracker.feed(b"")
    assert tracker.states == (State.UNKNOWN, State.PRIV_EXEC)


def test_observation_carries_its_evidence() -> None:
    tracker = StateTracker()
    tracker.feed(b"\r\nrommon 1 > ")
    evidence = tracker.current.evidence
    assert evidence
    assert evidence[0].kind is SignalKind.ROMMON_PROMPT
    assert evidence[0].value == "rommon 1 >"


def test_buffer_is_lossless_across_chunks() -> None:
    buffer = StreamBuffer()
    for byte in b"Router#":
        buffer.feed(bytes([byte]))
    assert buffer.raw == b"Router#"
    assert buffer.text == "Router#"


def test_buffer_decodes_high_bytes_without_raising() -> None:
    """Line noise must not crash a recovery.

    latin-1 is a total function: every byte decodes to exactly one character,
    so a stray 0xFF from a marginal cable becomes a harmless character rather
    than a UnicodeDecodeError halfway through a ROMMON sequence.
    """
    buffer = StreamBuffer()
    buffer.feed(b"\xff\xfe garbage \x00")
    assert len(buffer.text) == len(buffer.raw)


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (b"System Bootstrap, Version 12.3\r\n\r\nrommon 1 > ", State.ROMMON),
        (b"\r\nRouter>", State.USER_EXEC),
        (b"\r\nRouter#show log\r\n%SYS-3: memory test failed on slot 1\r\nRouter#", State.PRIV_EXEC),
    ],
)
def test_prompt_outranks_prior_self_test_failure(suffix: bytes, expected: State) -> None:
    """A live anchored prompt is stronger evidence than earlier boot/log text."""
    failure = (
        b"Failed all 0x00000000 test\r\n"
        b"Bad RAM at location 0x80000000\r\n"
        b"DDR memory test failed. Resetting the router ...\r\n"
    )
    tracker = StateTracker()
    tracker.feed(failure + suffix)
    assert tracker.current.state is expected
    assert tracker.current.basis is Basis.OBSERVED
    assert tracker.current.confidence is Confidence.HIGH


# -- the destructive guard latches -------------------------------------------

WARNING = b"WARNING: PASSWORD RECOVERY FUNCTIONALITY IS DISABLED\r\n"


def test_the_guard_survives_output_longer_than_the_detector_window() -> None:
    """The bug: the guard was re-derived from a bounded tail every byte.

    Detection reads the last TAIL_WINDOW characters, so once the device emitted
    more than that after the banner, the warning scrolled out of view and the
    guard silently lifted -- at exactly the point a destructive step was about
    to run. The banner is a fact about the device, not about the last 512
    bytes.
    """
    tracker = StateTracker()
    tracker.feed(WARNING)
    assert tracker.current.state is State.DESTRUCTIVE_RECOVERY_GUARD

    # Far more output than the detector can see at once, ending at a prompt
    # that would otherwise be read as a perfectly ordinary bootloader.
    tracker.feed(b"." * (TAIL_WINDOW * 4))
    tracker.feed(b"\r\nswitch: ")

    assert tracker.current.state is State.DESTRUCTIVE_RECOVERY_GUARD
    assert tracker.recovery_guard_latched is True


@settings(max_examples=200, deadline=None)
@given(
    filler=st.integers(min_value=0, max_value=4000),
    splits=st.lists(st.integers(min_value=1, max_value=500), max_size=12),
)
def test_the_guard_survives_any_amount_of_later_output(
    filler: int, splits: list[int]
) -> None:
    """Chunking was already property-tested; trailing *volume* was not.

    That gap is why the original test passed while the invariant was broken:
    both signals sat inside one window.
    """
    stream = WARNING + (b"x" * filler) + b"\r\nRouter#"
    tracker = StateTracker()
    for piece in chunked(stream, splits):
        tracker.feed(piece)

    assert tracker.current.state is State.DESTRUCTIVE_RECOVERY_GUARD


def test_clearing_the_guard_is_deliberate_and_needs_a_reason() -> None:
    """Nothing in the byte stream can release it."""
    tracker = StateTracker()
    tracker.feed(WARNING)

    with pytest.raises(ValueError, match="requires a reason"):
        tracker.clear_recovery_guard("")

    tracker.clear_recovery_guard("device power-cycled and rebooted without the banner")
    tracker.feed(b"\r\nswitch: ")
    assert tracker.current.state is State.BOOTLOADER


def test_a_fresh_tracker_starts_unlatched() -> None:
    assert StateTracker().recovery_guard_latched is False


# -- the questions IOS asks during a reset -----------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            b"Erasing the nvram filesystem will remove all files! "
            b"Continue? [confirm]",
            State.CONFIRM,
        ),
        (
            b"System configuration has been modified. Save? [yes/no]: ",
            State.SAVE_CONFIG_PROMPT,
        ),
        (b"Delete filename [vlan.dat]? ", State.FILENAME_PROMPT),
        (b"Delete flash:vlan.dat? [confirm]", State.CONFIRM),
        (b"Proceed with reload? [confirm]", State.CONFIRM),
    ],
)
def test_the_real_prompt_shapes_are_recognised(
    output: bytes, expected: State
) -> None:
    tracker = StateTracker()
    tracker.feed(output)
    assert tracker.current.state is expected
