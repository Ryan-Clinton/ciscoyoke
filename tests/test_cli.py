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


def test_doctor_never_recommends_running_elevated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Diagnostics must never tell anyone to escalate.

    This process handles transcripts, journals and firmware images; running all
    of it as root to bind one UDP port is the wrong trade, so the alternatives
    offered are an external TFTP server or XMODEM.

    The check is for a *recommendation*, not for the word. On Linux the port
    advice deliberately reads "add your user to the dialout group rather than
    using sudo" -- steering away from elevation while naming it is exactly the
    behaviour wanted, and an earlier version of this test banned the substring
    and so failed on the very message it should have been protecting.
    """
    main(["doctor"])
    output = capsys.readouterr().out.lower()

    recommendations = (
        "use sudo",
        "using sudo to",
        "run with sudo",
        "sudo ciscoyoke",
        "run as root",
        "as administrator",
        "elevated privileges",
    )
    for phrase in recommendations:
        assert phrase not in output, f"doctor recommended elevation: {phrase!r}"


@pytest.mark.parametrize(("platform", "offered"), [("linux", True), ("darwin", False)])
def test_doctor_offers_the_linux_capability_only_on_linux(
    platform: str,
    offered: bool,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seen on a macOS runner: advice to grant a capability macOS lacks."""
    from ciscoyoke import doctor

    # Diagnosed on the real platform first: only the rendering is under test.
    diagnosis = doctor.run()
    monkeypatch.setattr(doctor, "run", lambda: diagnosis)
    monkeypatch.setattr(
        type(diagnosis), "embedded_tftp_available", property(lambda self: False)
    )
    monkeypatch.setattr("sys.platform", platform)

    main(["doctor"])
    output = capsys.readouterr().out

    assert "Embedded TFTP unavailable." in output
    assert ("bind-service capability" in output) is offered
    assert "--via xmodem" in output


def test_doctor_steers_away_from_elevation_where_it_mentions_it() -> None:
    """Wherever elevation is named, it is named as the wrong answer."""
    from ciscoyoke.doctor import check_port_permissions
    from ciscoyoke.transport.identity import PortIdentity

    check = check_port_permissions((PortIdentity(device="/dev/ttyS-absent"),))

    if "sudo" in check.detail.lower():
        assert "rather than" in check.detail.lower()


def test_ports_reports_absence_as_a_result_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running with no adapters attached is informative, not an error state."""
    code = main(["--json", "ports"])
    payload = json.loads(capsys.readouterr().out)

    assert code in (ExitCode.SUCCESS, ExitCode.DEVICE_NOT_FOUND)
    assert "ports" in payload


@pytest.mark.parametrize(
    "argv",
    [
        ["rescue", "bench-left"],
        ["health", "bench-left"],
        ["archive", "bench-left"],
        ["reset", "bench-left"],
        ["intake", "bench-left"],
    ],
)
def test_a_label_is_accepted_wherever_a_port_is(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rescue bench-left` is the reason labels exist.

    Only the later commands resolved a label; the five here passed the label
    itself to the serial layer, which reported that no such port existed.
    """
    from ciscoyoke import commands
    from ciscoyoke.cli import identify
    from ciscoyoke.transport import labels

    monkeypatch.setattr(
        labels, "resolve_target", lambda value: "COM9" if value == "bench-left" else value
    )
    opened: list[str] = []

    def stop(port: str, *args: object, **kwargs: object) -> None:
        opened.append(port)
        raise commands.CommandError("stopped by the test", ExitCode.DEVICE_NOT_FOUND)

    for name in ("do_rescue", "do_health", "do_archive", "do_reset"):
        monkeypatch.setattr(commands, name, stop)

    def unavailable(port: str, baud: int) -> None:
        opened.append(port)
        raise identify.SerialUnavailableError("stopped by the test")

    monkeypatch.setattr(identify, "_intake_one", unavailable)

    assert main(argv) == ExitCode.DEVICE_NOT_FOUND
    assert opened == ["COM9"]


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


def test_cli_carries_argcomplete_marker_and_hooks_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shell completion activates when argcomplete is installed and degrades cleanly without it."""
    import builtins
    import sys
    from types import ModuleType

    import ciscoyoke.cli as cli_mod

    cli_source = Path(cli_mod.__file__).read_text(encoding="utf-8")
    assert "# PYTHON_ARGCOMPLETE_OK" in cli_source.splitlines()[:5]

    parser = cli_mod.build_parser()
    called_with: list[object] = []
    fake_argcomplete = ModuleType("argcomplete")
    fake_argcomplete.autocomplete = lambda p: called_with.append(p)  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "argcomplete", fake_argcomplete)
    cli_mod._enable_completion(parser)
    assert called_with == [parser]

    monkeypatch.delitem(sys.modules, "argcomplete", raising=False)
    real_import = builtins.__import__

    def _no_argcomplete(name: str, *args: object, **kwargs: object) -> object:
        if name == "argcomplete":
            raise ImportError("No module named 'argcomplete'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _no_argcomplete)
    cli_mod._enable_completion(parser)
