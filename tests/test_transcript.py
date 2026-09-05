"""Transcript formats, secret handling, and replay."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciscoyoke.transcript import schema
from ciscoyoke.transcript.fake import FakeDevice, ReplayMismatchError
from ciscoyoke.transcript.recorder import REDACTED, Recorder
from ciscoyoke.transcript.schema import Direction, Record, Transcript


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_recorder_timestamps_are_relative_to_session_start() -> None:
    clock = FakeClock()
    recorder = Recorder(clock=clock)
    clock.advance(1.5)
    recorder.record_rx(b"Router#")
    assert recorder.records[0].t == pytest.approx(1.5)


def test_recorder_never_stores_a_credential() -> None:
    """The credential is discarded before the record is built.

    Not redacted afterwards -- there is no window in which the plaintext exists
    in the transcript waiting to be cleaned up.
    """
    recorder = Recorder(clock=FakeClock())
    recorder.record_secret_tx(b"SuperSecret123\r")

    record = recorder.records[0]
    assert record.secret is True
    assert record.data == REDACTED
    assert b"SuperSecret123" not in record.data

    rendered = str(recorder.transcript())
    assert "SuperSecret123" not in rendered


def test_redaction_hides_credential_length() -> None:
    """Two different passwords must produce identical records.

    Otherwise transcript byte counts leak password length, which is exactly the
    kind of accidental side channel that makes 'we redacted it' untrue.
    """
    short = Recorder(clock=FakeClock())
    short.record_secret_tx(b"a\r")
    long = Recorder(clock=FakeClock())
    long.record_secret_tx(b"a-far-longer-password\r")
    assert short.records[0].data == long.records[0].data


def test_raw_transcript_cannot_be_loaded_as_a_fixture(tmp_path: Path) -> None:
    """The core privacy invariant, enforced by the schema rather than by care.

    A raw recording handed to a fixture loader raises. It cannot be silently
    committed and published.
    """
    path = tmp_path / "session.ytx"
    schema.write(path, Transcript(records=(Record(0.0, Direction.RX, b"Router#"),)))

    assert schema.read(path).public is False

    with pytest.raises(schema.TranscriptFormatError, match="raw transcript"):
        schema.read(path, require_public=True)


def test_public_transcript_requires_provenance(tmp_path: Path) -> None:
    """A fixture whose scrub history is unknown cannot be trusted as CI input."""
    path = tmp_path / "bad.ytx.pub"
    path.write_text('{"v": 1, "kind": "public"}\n', encoding="utf-8")

    with pytest.raises(schema.TranscriptFormatError, match="provenance"):
        schema.read(path)


def test_round_trip_preserves_bytes(tmp_path: Path) -> None:
    original = Transcript(
        records=(
            Record(0.0, Direction.RX, b"\r\nRouter> \xff"),
            Record(1.25, Direction.TX, b"enable\r"),
        ),
        public=True,
        scrub_version=1,
        source="test",
    )
    path = tmp_path / "t.ytx.pub"
    schema.write(path, original)
    assert schema.read(path).records == original.records


def test_fake_device_replays_received_bytes() -> None:
    transcript = Transcript(
        records=(
            Record(0.0, Direction.RX, b"\r\nRouter>"),
            Record(0.5, Direction.TX, b"enable\r"),
            Record(1.0, Direction.RX, b"\r\nPassword: "),
        )
    )
    device = FakeDevice(transcript)

    assert device.read() == b"\r\nRouter>"
    device.write(b"enable\r")
    assert device.read() == b"\r\nPassword: "


def test_fake_device_catches_a_wrong_command() -> None:
    """Replay asserts in both directions.

    Code that silently sends a different command fails the test rather than
    producing a plausible-looking transcript.
    """
    transcript = Transcript(
        records=(
            Record(0.0, Direction.RX, b"\r\nRouter>"),
            Record(0.5, Direction.TX, b"enable\r"),
        )
    )
    device = FakeDevice(transcript)
    device.read()

    with pytest.raises(ReplayMismatchError, match="expected"):
        device.write(b"disable\r")


def test_fake_device_clock_is_virtual() -> None:
    """A four-minute reload replays instantly but still reports four minutes."""
    transcript = Transcript(
        records=(
            Record(0.0, Direction.RX, b"reloading"),
            Record(240.0, Direction.RX, b"\r\nRouter>"),
        )
    )
    device = FakeDevice(transcript)
    device.read()
    device.read()
    assert device.monotonic() == pytest.approx(240.0)


def test_fake_device_will_not_skip_past_an_expected_command() -> None:
    """A test must not pass by reading output it never triggered."""
    transcript = Transcript(
        records=(
            Record(0.0, Direction.TX, b"show version\r"),
            Record(0.5, Direction.RX, b"Cisco IOS Software"),
        )
    )
    device = FakeDevice(transcript)
    assert device.read() == b""
