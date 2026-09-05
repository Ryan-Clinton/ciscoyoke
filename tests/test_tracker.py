"""State resolution, and the contextual reasoning that makes it possible."""

from __future__ import annotations

import pytest

from ciscoyoke.stream.buffer import StreamBuffer
from ciscoyoke.stream.signals import SignalKind, detect, first_anchored
from ciscoyoke.stream.tracker import Basis, Confidence, State, StateTracker


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        (b"\r\nRouter>", State.USER_EXEC),
        (b"\r\nRouter#", State.PRIV_EXEC),
        (b"\r\nRouter(config)#", State.CONFIG_MODE),
        (b"\r\nRouter(config-if)#", State.CONFIG_MODE),
        (b"\r\nswitch: ", State.BOOTLOADER),
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
