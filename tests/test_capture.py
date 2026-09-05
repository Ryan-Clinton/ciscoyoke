"""Capture: incremental writing, stop conditions, and baud plausibility."""

from __future__ import annotations

import json
from pathlib import Path

from ciscoyoke.capture import BaudProbe, best_baud, capture
from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.schema import TranscriptFormatError, read
from ciscoyoke.transcript.writer import TranscriptWriter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def scripted(chunks: list[bytes]):  # type: ignore[no-untyped-def]
    """A reader that yields each chunk once, then nothing."""
    queue = list(chunks)

    def read() -> bytes:
        return queue.pop(0) if queue else b""

    return read


# -- incremental writing ---------------------------------------------------


def test_records_are_on_disk_before_the_session_ends(tmp_path: Path) -> None:
    """The whole reason for a streaming writer.

    The captures that matter are often the ones that end badly -- cable pulled,
    device wedged, process killed. A capture held in memory until a clean exit
    loses exactly the run worth having.
    """
    path = tmp_path / "boot.ytx"
    writer = TranscriptWriter(path).open()

    writer.write_rx(0.0, b"System Bootstrap")
    partial = path.read_text(encoding="utf-8")

    assert "System Bootstrap" in partial
    assert writer.count == 1

    writer.close()


def test_a_capture_is_written_as_a_raw_transcript(tmp_path: Path) -> None:
    """Straight off a device and unscrubbed, so it must not look shareable."""
    path = tmp_path / "boot.ytx"
    with TranscriptWriter(path) as writer:
        writer.write_rx(0.0, b"\r\nswitch: ")

    transcript = read(path)
    assert transcript.public is False

    try:
        read(path, require_public=True)
    except TranscriptFormatError as exc:
        assert "raw transcript" in str(exc)
    else:  # pragma: no cover - the loader must refuse
        raise AssertionError("a raw capture was accepted as a fixture")


def test_the_header_makes_a_partial_file_readable(tmp_path: Path) -> None:
    """A file cut off mid-capture should still parse up to the cut."""
    path = tmp_path / "boot.ytx"
    writer = TranscriptWriter(path).open()
    writer.write_rx(0.0, b"first")
    writer.write_rx(0.1, b"second")
    # Deliberately not closed: simulate the process dying.

    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "raw"
    assert len(lines) == 3
    writer.close()


# -- stop conditions -------------------------------------------------------


def test_capture_stops_once_the_device_falls_silent(tmp_path: Path) -> None:
    """The right stop condition for a boot: when the device settles."""
    clock = Clock()
    with TranscriptWriter(tmp_path / "b.ytx") as writer:
        stats = capture(
            scripted([b"System Bootstrap, Version 12.2\r\n", b"\r\nswitch: "]),
            writer,
            quiet_after=1.0,
            clock=clock,
            sleep=clock.sleep,
        )

    assert stats.records == 2
    assert stats.final_state is State.BOOTLOADER


def test_capture_honours_a_hard_duration(tmp_path: Path) -> None:
    """A device that never stops talking must still hit the ceiling.

    The reader advances the clock because reading genuinely costs time on a
    serial line -- a chunk at 9600 baud is milliseconds. Without that, virtual
    time only moves when the capture loop sleeps, which it does not do while
    data keeps arriving, and the deadline never comes.
    """
    clock = Clock()

    def never_quiet() -> bytes:
        clock.now += 0.05
        return b"."

    with TranscriptWriter(tmp_path / "b.ytx") as writer:
        stats = capture(
            never_quiet, writer, duration=2.0, clock=clock, sleep=clock.sleep
        )

    assert stats.duration >= 2.0
    assert stats.records > 1


def test_states_are_tracked_during_capture(tmp_path: Path) -> None:
    """A capture that also tells you what it saw is worth more than bytes."""
    clock = Clock()
    tracker = StateTracker()
    with TranscriptWriter(tmp_path / "b.ytx") as writer:
        stats = capture(
            scripted(
                [
                    b"System Bootstrap, Version 12.2\r\n",
                    b"WARNING: PASSWORD RECOVERY FUNCTIONALITY IS DISABLED\r\n",
                    b"\r\nswitch: ",
                ]
            ),
            writer,
            quiet_after=1.0,
            clock=clock,
            sleep=clock.sleep,
            tracker=tracker,
        )

    assert State.DESTRUCTIVE_RECOVERY_GUARD in stats.states_seen
    # And it latches, so the guard survives to the end of the capture.
    assert stats.final_state is State.DESTRUCTIVE_RECOVERY_GUARD


# -- baud plausibility -----------------------------------------------------


def test_noise_is_flagged_as_a_probable_baud_mismatch(tmp_path: Path) -> None:
    """Twenty minutes of garbage is a bad way to discover the wrong rate."""
    clock = Clock()
    with TranscriptWriter(tmp_path / "b.ytx") as writer:
        stats = capture(
            scripted([bytes(range(128, 256)) * 2]),
            writer,
            quiet_after=1.0,
            clock=clock,
            sleep=clock.sleep,
        )

    assert stats.looks_like_wrong_baud is True
    assert "line speed is wrong" in stats.render(tmp_path / "b.ytx")


def test_console_text_is_not_flagged(tmp_path: Path) -> None:
    clock = Clock()
    with TranscriptWriter(tmp_path / "b.ytx") as writer:
        stats = capture(
            scripted([b"System Bootstrap, Version 12.2(8r)T2\r\n" * 4]),
            writer,
            quiet_after=1.0,
            clock=clock,
            sleep=clock.sleep,
        )

    assert stats.looks_like_wrong_baud is False


def test_the_sweep_picks_the_most_console_like_rate() -> None:
    probes = [
        BaudProbe(9600, 400, 0.99),
        BaudProbe(115200, 380, 0.31),
        BaudProbe(38400, 0, 0.0),
    ]
    assert best_baud(probes) == 9600


def test_the_sweep_reports_nothing_rather_than_guessing() -> None:
    """"No rate produced console output" is a real finding.

    It usually means the cable is reversed or the device is off, and dressing
    it up as a best guess would send someone chasing the wrong problem.
    """
    probes = [BaudProbe(9600, 0, 0.0), BaudProbe(115200, 12, 0.2)]
    assert best_baud(probes) is None
