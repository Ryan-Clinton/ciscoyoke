"""Regression tests for the round-5 review findings.

Every one of these was found by reading the code against Cisco's documented
procedures rather than by running the suite, which was green throughout. That
is the useful lesson: the tests proved properties of the model, and the model
disagreed with the external system.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciscoyoke.image.bootproof import ProofOutcome, prove
from ciscoyoke.image.drivers import (
    CatalystBootloaderDriver,
    Family,
    RouterRommonDriver,
    classify,
)
from ciscoyoke.image.fingerprint import fingerprint
from ciscoyoke.imaging import restore_console_speed
from ciscoyoke.lifecycle import reset
from ciscoyoke.playbook.transfer import Method, Progress, TransferStep
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import Direction, Record, Transcript


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def session_over(records: tuple[Record, ...]) -> Session:
    device = FakeDevice(Transcript(records=records), strict=False)
    return Session(
        FakeTransport(device), clock=device.monotonic, sleep=device.sleeper()
    )


class ReplayingSerialTransport(FakeTransport):
    """A replaying transport that also supports baud changes.

    ``FakeTransport`` deliberately implements only the minimum Transport
    protocol, which has no ``set_baud`` -- so restoration bails out early
    against it. Anything exercising the baud path needs a transport that can
    actually change speed, and this is that.
    """

    def __init__(self, device: FakeDevice) -> None:
        super().__init__(device, name="replay+baud")
        self.baud = 9600

    def set_baud(self, baud: int) -> None:
        self.baud = baud

    def reset_input(self) -> None:
        """No-op: discarding here would throw away the replay itself."""
        return None


def baud_capable_session(records: tuple[Record, ...]) -> Session:
    device = FakeDevice(Transcript(records=records), strict=False)
    return Session(
        ReplayingSerialTransport(device),
        clock=device.monotonic,
        sleep=device.sleeper(),
    )


# -- 1. console restoration must be observed, not assumed ------------------


class SilentTransport:
    """A transport that accepts writes and never answers.

    Models the case that mattered: the device did not come back at the restored
    rate.
    """

    def __init__(self) -> None:
        self.baud = 9600

    @property
    def name(self) -> str:
        return "silent"

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        from ciscoyoke.transport.base import TransportCapabilities

        return TransportCapabilities.local_serial()

    def read(self) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def set_baud(self, baud: int) -> None:
        self.baud = baud

    def reset_input(self) -> None:
        return None


def test_a_silent_device_is_not_reported_as_restored() -> None:
    """The bug: restoration returned bool(tracker.buffer).

    That buffer accumulates the whole session, so after a flash inventory and
    an XMODEM setup it is never empty -- restoration was reported as verified
    whether or not the device said a word, and the journal then recorded the
    mutation as compensated on no evidence at all.
    """
    clock = Clock()
    session = Session(SilentTransport(), clock=clock, sleep=clock.sleep)
    # Prior traffic, exactly as a real session would have accumulated.
    session.tracker.feed(b"\r\nswitch: \r\nlots of earlier output\r\n")

    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)
    proof = restore_console_speed(session, step, CatalystBootloaderDriver())

    assert proof.bytes_observed_after_change is False
    assert proof.restored is False
    assert "✗" in proof.render()


def test_bytes_alone_are_not_enough_to_claim_restoration() -> None:
    """A wrong baud produces bytes too -- just not meaningful ones.

    A recognised prompt is what distinguishes a device talking to us from a
    device talking past us.
    """
    session = baud_capable_session(
        (Record(0.0, Direction.RX, b"\xff\xfe\x81\x9a garbage \xc3"),)
    )
    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)

    proof = restore_console_speed(session, step, CatalystBootloaderDriver())

    assert proof.bytes_observed_after_change is True
    assert proof.prompt_observed is False
    assert proof.restored is False


def test_a_recognised_prompt_after_the_change_is_restoration() -> None:
    session = baud_capable_session((Record(0.0, Direction.RX, b"\r\nswitch: "),))
    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)

    proof = restore_console_speed(session, step, CatalystBootloaderDriver())

    assert proof.restored is True
    assert proof.state is State.BOOTLOADER


# -- 2. reset must answer the prompts IOS actually asks --------------------


def test_write_erase_expects_the_confirmation_it_will_receive() -> None:
    """The bug: step one sent `write erase` and waited for PRIV_EXEC.

    But IOS answers "Erasing the nvram filesystem will remove all files!
    Continue? [confirm]" and will not return to the prompt until that is
    answered -- which was step two. Both sides waited.
    """
    steps = {step.name: step for step in reset.router_reset().steps}

    erase = steps["erase_startup_config"]
    assert State.CONFIRM in erase.expect
    assert State.PRIV_EXEC not in erase.expect

    confirm = steps["confirm_erase"]
    assert State.CONFIRM in confirm.require_state
    assert State.PRIV_EXEC in confirm.expect


def test_reload_handles_the_save_question_then_the_confirmation() -> None:
    steps = [step.name for step in reset.router_reset().steps]
    assert steps.index("decline_save") < steps.index("confirm_reload")

    by_name = {step.name: step for step in reset.router_reset().steps}
    assert State.SAVE_CONFIG_PROMPT in by_name["reload"].expect


def test_deleting_vlan_dat_is_a_three_part_exchange() -> None:
    """IOS asks for the filename, then confirms.

    Blasting three carriage returns worked only if every prompt appeared in the
    assumed order and timing, and gave no way to notice when it did not.
    """
    steps = {step.name: step for step in reset.switch_reset().steps}

    assert State.FILENAME_PROMPT in steps["delete_vlan_database"].expect
    assert "accept_vlan_filename" in steps
    assert "confirm_vlan_delete" in steps


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


# -- 3. boot proof must identify the image, not just the version -----------

WRONG_IMAGE = (
    b"show version\r\n"
    b"Cisco IOS Software, C2950 Software, Version 12.1(22)EA14\r\n"
    b'System image file is "flash:c2950-otherfeatures-mz.121-22.EA14.bin"\r\n'
    b"\r\nSwitch#"
)

RIGHT_IMAGE = (
    b"show version\r\n"
    b"Cisco IOS Software, C2950 Software, Version 12.1(22)EA14\r\n"
    b'System image file is "flash:c2950-i6q4l2-mz.121-22.EA14.bin"\r\n'
    b"\r\nSwitch#"
)


def image_for(tmp_path: Path):  # type: ignore[no-untyped-def]
    path = tmp_path / "c2950-i6q4l2-mz.121-22.EA14.bin"
    path.write_bytes(b"\x7fELF" + b"\x00" * 400)
    return fingerprint(path)


def test_the_same_version_but_a_different_image_is_not_a_pass(
    tmp_path: Path,
) -> None:
    """The feature set is the whole reason someone picked one image over another.

    Comparing versions alone would pass a device running a different image with
    the same IOS version.
    """
    session = session_over(
        (
            Record(0.0, Direction.RX, b"\r\nSwitch#"),
            Record(0.1, Direction.RX, WRONG_IMAGE),
        )
    )

    proof = prove(session, image_for(tmp_path), console_restored=True)

    assert proof.outcome is ProofOutcome.BOOTED_UNVERIFIED
    identity = next(
        c for c in proof.checks if c[0] == "running image is the supplied image"
    )
    assert identity[1] is False


def test_the_matching_image_passes(tmp_path: Path) -> None:
    session = session_over(
        (
            Record(0.0, Direction.RX, b"\r\nSwitch#"),
            Record(0.1, Direction.RX, RIGHT_IMAGE),
        )
    )

    proof = prove(session, image_for(tmp_path), console_restored=True)

    assert proof.outcome is ProofOutcome.PASS
    assert proof.running_image is not None


# -- 4. the platforms are separated ----------------------------------------


def test_state_selects_the_platform_family() -> None:
    assert classify(State.BOOTLOADER, None) is Family.CATALYST_BOOTLOADER
    assert classify(State.ROMMON, None) is Family.ROUTER_ROMMON
    assert classify(State.PRIV_EXEC, None) is Family.BOOTED_IOS
    assert classify(State.LOGIN_PASSWORD, None) is Family.UNSUPPORTED


def test_the_two_platforms_produce_different_commands() -> None:
    catalyst = CatalystBootloaderDriver()
    rommon = RouterRommonDriver()

    assert catalyst.set_baud_command(115200) == b"set BAUD 115200\r"
    assert rommon.set_baud_command(115200) is None

    assert catalyst.restore_baud_command() == b"unset BAUD\r"
    assert rommon.restore_baud_command() is None

    assert b"copy xmodem:" in catalyst.transfer_command("a.bin", 115200)
    assert b"xmodem -c" in rommon.transfer_command("a.bin", 9600)


def test_catalyst_mounts_flash_before_listing_it() -> None:
    commands = CatalystBootloaderDriver().inventory_command()
    assert commands[0] == b"flash_init\r"
    assert b"dir flash:\r" in commands

    # ROMMON needs no mount step.
    assert RouterRommonDriver().inventory_command() == (b"dir flash:\r",)


# -- 5. progress reaches the caller with real elapsed time -----------------


def test_progress_without_elapsed_time_cannot_estimate() -> None:
    """The frozen-clock bug: rate and ETA had nothing to work from.

    The display would have sat on "estimating..." for the whole transfer, which
    is the experience the reporting exists to prevent.
    """
    frozen = Progress(total_bytes=1_000_000, sent_bytes=500_000, started_at=0.0, now=0.0)
    assert frozen.remaining is None
    assert "estimating" in frozen.render()

    moving = Progress(
        total_bytes=1_000_000, sent_bytes=500_000, started_at=0.0, now=60.0
    )
    assert moving.remaining is not None
    assert "min left" in moving.render()
