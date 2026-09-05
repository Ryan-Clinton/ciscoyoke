"""CLI contract: exit codes and the machine-readable schema.

Exit codes and the JSON shape are a public interface -- a shell script, a CI job
or an Ansible wrapper branches on them -- so changing one is a breaking change
and is covered here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciscoyoke.cli import main
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import SCHEMA_VERSION, Finding, Result
from ciscoyoke.stream.tracker import Basis, Confidence, State, StateTracker
from ciscoyoke.transcript import schema as transcript_schema
from ciscoyoke.transcript.schema import Direction, Record, Transcript


def test_exit_codes_are_stable() -> None:
    """These numbers are the interface. Pinned deliberately."""
    assert ExitCode.SUCCESS == 0
    assert ExitCode.DEVICE_NOT_FOUND == 10
    assert ExitCode.STATE_UNCERTAIN == 11
    assert ExitCode.AUTHENTICATION_REQUIRED == 20
    assert ExitCode.DESTRUCTIVE_ACTION_REFUSED == 30
    assert ExitCode.TRANSPORT_CAPABILITY_UNAVAILABLE == 40
    assert ExitCode.TRANSFER_FAILED == 50
    assert ExitCode.INTERRUPTED_RECOVERY == 60
    assert ExitCode.TARGET_LEASE_HELD == 70


def test_every_exit_code_explains_itself() -> None:
    for code in ExitCode:
        assert code.explanation


def test_doctor_runs_and_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--json", "doctor"])
    payload = json.loads(capsys.readouterr().out)

    assert code == ExitCode.SUCCESS
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "doctor"
    assert isinstance(payload["checks"], list)


def test_doctor_human_output_offers_alternatives_when_tftp_is_blocked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blocked privileged port must never produce a 'run as root' suggestion.

    This process handles transcripts, journals and firmware images; escalating
    all of it to bind one UDP port is the wrong trade, so the alternatives are
    an external server or XMODEM.
    """
    main(["doctor"])
    output = capsys.readouterr().out

    assert "sudo" not in output.lower()
    assert "as root" not in output.lower()


def test_ports_reports_absence_as_a_result_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running with no adapters attached is informative, not an error state."""
    code = main(["--json", "ports"])
    payload = json.loads(capsys.readouterr().out)

    assert code in (ExitCode.SUCCESS, ExitCode.DEVICE_NOT_FOUND)
    assert "ports" in payload


def test_transcript_scrub_writes_a_public_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = tmp_path / "session.ytx"
    transcript_schema.write(
        raw,
        Transcript(
            records=(
                Record(0.0, Direction.RX, b"hostname CORE-01\nenable secret 5 $1$xyz\n"),
            )
        ),
    )

    code = main(["--json", "transcript", "scrub", str(raw)])
    payload = json.loads(capsys.readouterr().out)

    assert code == ExitCode.SUCCESS
    assert payload["credentials_redacted"] >= 1

    produced = Path(payload["output"])
    # The output is loadable as a fixture; the input still is not.
    assert transcript_schema.read(produced, require_public=True).public is True
    with pytest.raises(transcript_schema.TranscriptFormatError):
        transcript_schema.read(raw, require_public=True)

    assert "cannot prove" in " ".join(payload["notes"])


def test_result_json_carries_basis_and_evidence_not_a_float() -> None:
    """No ``state_confidence: 0.98``.

    A float would look scientific without being calibrated against anything.
    The schema carries what the tool actually has.
    """
    tracker = StateTracker()
    tracker.feed(b"\r\nRouter#")
    finding = Finding.from_observation(tracker.current)
    payload = Result("scan", 0, {"device": finding.to_json()}).to_json()

    device = payload["device"]
    assert device["value"] == State.PRIV_EXEC.value
    assert device["basis"] == Basis.OBSERVED.value
    assert device["confidence"] == Confidence.HIGH.value
    assert device["evidence"][0]["signal"] == "priv_exec_prompt"
    assert not any(isinstance(value, float) for value in device.values())


def test_unknown_finding_explains_why() -> None:
    """An absent field tells the reader nothing; an explained one is actionable."""
    finding = Finding.unknown("identity requires boot evidence or credentials")
    payload = finding.to_json()

    assert payload["value"] is None
    assert payload["basis"] == Basis.UNKNOWN.value
    assert "credentials" in payload["reason"]
