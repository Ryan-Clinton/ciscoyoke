"""Regression tests for four defects found in review.

Each of these is a case where the code's stated intent and its actual behaviour
had diverged. They are grouped here rather than scattered so the failures stay
legible as a set, and so it is obvious what a review round bought.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ciscoyoke.image.bootproof import ProofOutcome, prove
from ciscoyoke.image.drivers import (
    CatalystBootloaderDriver,
    NoDriverError,
    RouterRommonDriver,
    driver_for,
)
from ciscoyoke.image.fingerprint import fingerprint
from ciscoyoke.imaging import raise_console_speed, restore_console_speed
from ciscoyoke.playbook import xmodem
from ciscoyoke.playbook.transfer import Method, TransferStep
from ciscoyoke.playbook.xmodem import BLOCK_SIZE, Control, XmodemError
from ciscoyoke.session import Session
from ciscoyoke.stream.buffer import TAIL_WINDOW
from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport, chunked
from ciscoyoke.transcript.schema import Direction, Record, Transcript

WARNING = b"WARNING: PASSWORD RECOVERY FUNCTIONALITY IS DISABLED\r\n"


# -- 1. the destructive guard must latch -----------------------------------


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


# -- 2. XMODEM must not declare success on an unanswered EOT ---------------


class Receiver:
    def __init__(self, responses: list[int], *, final: list[int] | None = None) -> None:
        self.responses = responses
        self.final = final if final is not None else []
        self.sent: list[bytes] = []
        self._started = False

    def read(self) -> bytes:
        if not self._started:
            self._started = True
            return bytes([Control.CRC])
        if self.responses:
            return bytes([self.responses.pop(0)])
        if self.final:
            return bytes([self.final.pop(0)])
        return b""

    def write(self, data: bytes) -> int:
        self.sent.append(data)
        return len(data)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_an_unacknowledged_eot_is_not_success() -> None:
    """The bug: the EOT response was read and discarded.

    Every block acknowledged but the receiver silent at the end meant the
    device never confirmed it wrote the file -- and after ninety minutes, on a
    box whose only image was replaced, optimism is the wrong default.
    """
    clock = Clock()
    receiver = Receiver([Control.ACK])

    with pytest.raises(XmodemError, match="never acknowledged the end"):
        xmodem.send(
            b"hello",
            receiver.read,
            receiver.write,
            max_retries=3,
            block_timeout=1.0,
            clock=clock,
            sleep=clock.sleep,
        )


def test_the_classic_eot_nak_eot_ack_exchange_is_accepted() -> None:
    """Several receivers NAK the first EOT and acknowledge the second."""
    clock = Clock()
    receiver = Receiver([Control.ACK], final=[Control.NAK, Control.ACK])

    outcome = xmodem.send(
        b"hello",
        receiver.read,
        receiver.write,
        block_timeout=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert outcome.complete is True


def test_cancellation_at_the_end_says_the_image_is_incomplete() -> None:
    clock = Clock()
    receiver = Receiver([Control.ACK], final=[Control.CAN])

    with pytest.raises(xmodem.XmodemCancelledError, match="incomplete"):
        xmodem.send(
            b"hello",
            receiver.read,
            receiver.write,
            block_timeout=1.0,
            clock=clock,
            sleep=clock.sleep,
        )


# -- 3. the baud change must reach the device first ------------------------


class RecordingTransport:
    """A transport that records the order of writes and baud changes."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.baud = 9600

    @property
    def name(self) -> str:
        return "recording"

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        from ciscoyoke.transport.base import TransportCapabilities

        return TransportCapabilities.local_serial()

    def read(self) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        self.events.append(f"tx:{data.decode('latin-1').strip()}")
        return len(data)

    def set_baud(self, baud: int) -> None:
        self.events.append(f"host_baud:{baud}")
        self.baud = baud

    def reset_input(self) -> None:
        self.events.append("reset_input")


def recording_session() -> tuple[Session, RecordingTransport]:
    """A session over a recording transport, in advancing virtual time.

    A clock that never moves turns every deadline loop into an infinite one --
    which is exactly what an earlier version of this test did.
    """
    transport = RecordingTransport()
    clock = Clock()
    session = Session(transport, clock=clock, sleep=clock.sleep)
    return session, transport


def test_the_device_baud_command_precedes_the_host_change() -> None:
    """The bug: the host changed first, so the device saw line noise.

    Cisco's procedure is `set BAUD 115200` and then "the screen goes blank" --
    the device is now talking at a rate the host is not listening at. Doing it
    the other way round leaves a device that appears to have hung.
    """
    session, transport = recording_session()
    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)

    raise_console_speed(session, step, CatalystBootloaderDriver())

    device_index = transport.events.index("tx:set BAUD 115200")
    host_index = transport.events.index("host_baud:115200")
    assert device_index < host_index


def test_restoring_uses_unset_baud_and_puts_the_host_back() -> None:
    session, transport = recording_session()
    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)

    restore_console_speed(session, step, CatalystBootloaderDriver())

    assert "tx:unset BAUD" in transport.events
    device_index = transport.events.index("tx:unset BAUD")
    host_index = transport.events.index("host_baud:9600")
    assert device_index < host_index


def test_the_xmodem_destination_carries_a_filename() -> None:
    """`copy xmodem: flash:` with no name is rejected by the bootloader.

    Cisco documents `copy xmodem: flash:c2955-i6q4l2-mz.121-13.EA1.bin`.

    The previous version of this test read imaging.py and asserted a substring
    was present, which a comment would have satisfied. This asserts the bytes
    the driver actually produces.
    """
    command = CatalystBootloaderDriver().transfer_command("c2950-foo.bin", 115200)

    assert command == b"copy xmodem: flash:c2950-foo.bin\r"


def test_a_router_in_rommon_gets_rommon_commands_not_catalyst_ones() -> None:
    """The two are different programs, not two dialects of one.

    Sending `set BAUD` and `copy xmodem: flash:` to a ROMMON prompt produces a
    device that ignores them -- and the flash listing shapes differ too, so a
    Catalyst parser yields an empty inventory that the planner reads as "no
    images present".
    """
    rommon = RouterRommonDriver()

    assert rommon.transfer_command("c2600-i-mz.bin", 115200) == (
        b"xmodem -cs115200 c2600-i-mz.bin\r"
    )
    assert rommon.transfer_command("c2600-i-mz.bin", 9600) == b"xmodem -c c2600-i-mz.bin\r"
    # ROMMON carries the rate on the transfer, so there is no standalone
    # command -- and returning None is what stops a Catalyst command being sent.
    assert rommon.set_baud_command(115200) is None
    assert rommon.can_change_baud is False


def test_each_platform_parses_its_own_flash_listing() -> None:
    catalyst_output = (
        "Directory of flash:/\n"
        "    2  -rwx     5001728   Mar 01 1993 00:01:24  c2950-i6q4l2-mz.bin\n"
        "7741440 bytes total (2739712 bytes free)\n"
    )
    rommon_output = (
        "File size           Checksum   File name\n"
        "5358032 bytes (0x51c1d0)   0x7b16    c2600-i-mz.122-10b.bin\n"
    )

    catalyst = CatalystBootloaderDriver().parse_inventory(catalyst_output)
    rommon = RouterRommonDriver().parse_inventory(rommon_output)

    assert [f.name for f in catalyst.images] == ["c2950-i6q4l2-mz.bin"]
    assert catalyst.free_bytes == 2739712
    assert [f.name for f in rommon.images] == ["c2600-i-mz.122-10b.bin"]
    assert rommon.images[0].size == 5358032

    # The crucial half: each parser must NOT silently read the other's output
    # as an empty inventory, which the planner would act on.
    assert CatalystBootloaderDriver().parse_inventory(rommon_output).images == ()


def test_a_device_in_an_unsupported_state_is_refused_a_driver() -> None:
    with pytest.raises(NoDriverError, match="no verified image-recovery"):
        driver_for(State.LOGIN_PASSWORD, None)


def test_a_booted_device_is_told_it_does_not_need_a_rescue() -> None:
    with pytest.raises(NoDriverError, match="does not need an image rescue"):
        driver_for(State.PRIV_EXEC, "WS-C2950-24")


# -- 4. a transfer is not a rescue until the device boots ------------------


def booted_session(records: tuple[Record, ...]) -> Session:
    device = FakeDevice(Transcript(records=records), strict=False)
    return Session(
        FakeTransport(device), clock=device.monotonic, sleep=device.sleeper()
    )


def make_image(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x7fELF" + b"\x00" * 500)
    return path


SHOW_VERSION = (
    b"show version\r\n"
    b"Cisco IOS Software, C2950 Software (C2950-I6Q4L2-M), "
    b"Version 12.1(22)EA14, RELEASE SOFTWARE\r\n"
    b'System image file is "flash:c2950-i6q4l2-mz.121-22.EA14.bin"\r\n'
    b"\r\nSwitch#"
)


def test_a_device_that_never_boots_is_reported_as_such(tmp_path: Path) -> None:
    """The definitive success criterion is the boot, not the transfer."""
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.EA14.bin"))
    session = booted_session(())

    proof = prove(session, image, boot_timeout=0.5)

    assert proof.outcome is ProofOutcome.DID_NOT_BOOT
    assert proof.proven is False
    assert "not that they boot" in proof.render()


def test_a_booted_device_with_a_matching_version_passes(tmp_path: Path) -> None:
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.EA14.bin"))
    session = booted_session(
        (
            Record(0.0, Direction.RX, b"\r\nSwitch#"),
            Record(0.1, Direction.RX, SHOW_VERSION),
        )
    )

    proof = prove(session, image, console_restored=True)

    assert proof.outcome is ProofOutcome.PASS
    assert proof.proven is True
    assert proof.running_version == "12.1(22)EA14"
    assert proof.running_image is not None


def test_booting_without_restoring_the_console_is_not_a_pass(
    tmp_path: Path,
) -> None:
    """All four conditions, or it is not proven.

    A device left at 115200 looks dead to the next person who connects.
    """
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.EA14.bin"))
    session = booted_session(
        (
            Record(0.0, Direction.RX, b"\r\nSwitch#"),
            Record(0.1, Direction.RX, SHOW_VERSION),
        )
    )

    proof = prove(session, image, console_restored=False)

    assert proof.outcome is ProofOutcome.BOOTED_UNVERIFIED
    restored = next(c for c in proof.checks if c[0] == "console speed restored")
    assert restored[1] is False


def test_an_unreadable_version_is_undetermined_not_a_failure(
    tmp_path: Path,
) -> None:
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.EA14.bin"))
    session = booted_session((Record(0.0, Direction.RX, b"\r\nSwitch#"),))

    proof = prove(session, image, console_restored=True)

    assert proof.outcome is ProofOutcome.BOOTED_UNVERIFIED
    assert proof.proven is False


def test_progress_is_reported_to_the_caller() -> None:
    """The product insight was 'do not leave someone staring at nothing'.

    The reporter was accepted and then dropped, which turned it back into
    exactly that.
    """
    clock = Clock()
    payload = b"x" * (BLOCK_SIZE * 4)
    receiver = Receiver([Control.ACK] * 4, final=[Control.ACK])

    seen: list[int] = []
    xmodem.send(
        payload,
        receiver.read,
        receiver.write,
        progress=lambda sent, _total: seen.append(sent),
        block_timeout=1.0,
        clock=clock,
        sleep=clock.sleep,
    )

    assert len(seen) == 4
    assert seen[-1] == len(payload)
