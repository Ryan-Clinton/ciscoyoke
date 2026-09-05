"""The real lifecycle engine, replayed against recorded device behaviour.

These tests drive the production ``Session`` and ``intake`` -- not a stand-in --
against committed transcripts, on a machine with nothing plugged in.

Every fixture here is ``synthetic:`` provenance: constructed from Cisco's
published output, never captured from hardware. They pin the parser and the
state machine against the documented output shape, which is worth having, but
they do not and cannot demonstrate the tool works on a real device. That is why
the support matrix is still empty. See tests/fixtures/README.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciscoyoke.identify.probe import intake
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import Basis, State
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import TranscriptFormatError, read

FIXTURES = Path(__file__).parent / "fixtures"


def session_for(name: str, *, strict: bool = False) -> tuple[Session, FakeDevice]:
    """Build a real Session over a recorded transcript, in virtual time."""
    transcript = read(FIXTURES / name, require_public=True)
    device = FakeDevice(transcript, strict=strict)
    session = Session(
        FakeTransport(device),
        clock=device.monotonic,
        sleep=device.sleeper(),
    )
    return session, device


def test_every_fixture_is_public_with_provenance() -> None:
    """A fixture whose scrub history is unknown is not trustworthy CI input."""
    fixtures = sorted(FIXTURES.glob("*.ytx.pub"))
    assert fixtures, "no fixtures found"

    for path in fixtures:
        transcript = read(path, require_public=True)
        assert transcript.scrub_version is not None
        assert transcript.source


def test_no_raw_transcript_is_present() -> None:
    """Raw recordings must never reach the repository.

    Enforced here as well as by .gitignore and CI, because a credential
    committed to git history cannot be taken back.
    """
    raw = [p for p in FIXTURES.glob("*.ytx") if not p.name.endswith(".ytx.pub")]
    assert raw == []


def test_fixture_provenance_is_declared_honestly() -> None:
    """Synthetic fixtures must say so.

    If this ever fails because a fixture claims `hardware:`, the support matrix
    in the README must be updated to match -- and only then.
    """
    for path in sorted(FIXTURES.glob("*.ytx.pub")):
        source = read(path, require_public=True).source or ""
        assert source.startswith(("synthetic:", "hardware:")), path.name


def test_router_intake_identifies_the_device() -> None:
    session, _ = session_for("router-1760-intake.ytx.pub", strict=True)
    result = intake(session)

    assert result.observation.state is State.USER_EXEC
    assert result.interrogated is True

    facts = result.facts
    assert facts.model.value == "1760"
    assert facts.model.basis is Basis.OBSERVED
    assert facts.ios_version.value == "12.4(25c)"
    assert facts.serial_number.value == "FTX0812A1BC"
    assert facts.config_register.value == "0x2102"
    assert facts.flash_kb.value == "32768"
    # 121856K main + 9216K I/O is the installed DRAM.
    assert facts.dram_kb.value == "131072"


def test_locked_switch_reports_why_identity_is_unavailable() -> None:
    """The honest failure. A locked device cannot be identified, and says so."""
    session, _ = session_for("switch-2950-locked.ytx.pub")
    result = intake(session)

    assert result.locked is True
    assert result.interrogated is False
    assert result.facts.model.value is None
    assert "credentials" in result.note

    payload = result.to_json()
    assert payload["facts"]["model"]["basis"] == "unknown"
    assert payload["facts"]["model"]["reason"]


def test_bootloader_with_missing_image_is_recognised() -> None:
    session, _ = session_for("switch-2950-bootloader.ytx.pub")
    result = intake(session)

    assert result.observation.state is State.BOOTLOADER
    assert "no IOS booted" in result.note


def test_recovery_disabled_is_flagged_before_anything_is_attempted() -> None:
    """The interlock, reached through the real engine over a real transcript."""
    session, _ = session_for("switch-2950-recovery-disabled.ytx.pub")
    result = intake(session)

    assert result.observation.state is State.DESTRUCTIVE_RECOVERY_GUARD
    assert "may destroy the startup configuration" in result.note
    assert result.interrogated is False


def test_rommon_is_recognised() -> None:
    session, _ = session_for("router-1760-rommon.ytx.pub")
    result = intake(session)

    assert result.observation.state is State.ROMMON
    assert "ROMMON" in result.note


def test_intake_can_be_told_to_transmit_nothing() -> None:
    """A caller may need certainty that not one byte was sent."""
    session, device = session_for("router-1760-intake.ytx.pub")
    result = intake(session, interrogate=False)

    assert result.interrogated is False
    assert device.sent == ()


def test_a_raw_fixture_would_be_refused() -> None:
    """Belt and braces: the loader itself refuses a raw recording."""
    from ciscoyoke.transcript.schema import Direction, Record, Transcript, write

    raw = FIXTURES / "_tmp_raw.ytx"
    try:
        write(raw, Transcript(records=(Record(0.0, Direction.RX, b"Router#"),)))
        with pytest.raises(TranscriptFormatError):
            read(raw, require_public=True)
    finally:
        raw.unlink(missing_ok=True)
