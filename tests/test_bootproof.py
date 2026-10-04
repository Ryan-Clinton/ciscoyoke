"""Boot proof: a transfer is not a rescue until the device boots that image."""

from __future__ import annotations

from pathlib import Path

from ciscoyoke.image.bootproof import ProofOutcome, prove
from ciscoyoke.image.fingerprint import fingerprint
from ciscoyoke.session import Session
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import Direction, Record, Transcript


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


# -- the image is identified, not just the version ----------------------------

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
    session = booted_session(
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
    session = booted_session(
        (
            Record(0.0, Direction.RX, b"\r\nSwitch#"),
            Record(0.1, Direction.RX, RIGHT_IMAGE),
        )
    )

    proof = prove(session, image_for(tmp_path), console_restored=True)

    assert proof.outcome is ProofOutcome.PASS
    assert proof.running_image is not None
