"""Preservation: partial by nature, and honest about it."""

from __future__ import annotations

import json
from pathlib import Path

from ciscoyoke.identify.probe import intake
from ciscoyoke.lifecycle.archive import (
    ArtifactStatus,
    PreservationStatus,
    archive,
)
from ciscoyoke.lifecycle.reset import decide
from ciscoyoke.session import Session
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import read

FIXTURES = Path(__file__).parent / "fixtures"


def session_for(name: str) -> Session:
    device = FakeDevice(read(FIXTURES / name, require_public=True), strict=False)
    return Session(
        FakeTransport(device), clock=device.monotonic, sleep=device.sleeper()
    )


def archive_for(name: str):  # type: ignore[no-untyped-def]
    session = session_for(name)
    return archive(session, intake(session))


# -- the locked device, which is the case that matters ---------------------


def test_a_locked_device_yields_a_partial_archive() -> None:
    """The honest outcome, and the common one.

    You cannot read a startup-config you cannot authenticate to. An earlier
    draft promised "nothing is lost"; this is what that promise actually meets.
    """
    bundle = archive_for("switch-2950-locked.ytx.pub")

    assert bundle.status is PreservationStatus.PARTIAL
    assert bundle.complete is False


def test_the_boot_transcript_is_always_captured() -> None:
    """On a locked device it is the only preservation possible.

    Which is exactly why it is captured first, rather than treated as a
    consolation prize.
    """
    bundle = archive_for("switch-2950-locked.ytx.pub")
    transcript = next(a for a in bundle.artifacts if a.name == "boot_transcript")

    assert transcript.status is ArtifactStatus.CAPTURED
    assert transcript.sha256


def test_inaccessible_artifacts_say_why_and_what_it_costs() -> None:
    """A gap without a reason tells the operator nothing actionable."""
    bundle = archive_for("switch-2950-locked.ytx.pub")
    startup = next(a for a in bundle.artifacts if a.name == "startup_config")

    assert startup.status is ArtifactStatus.INACCESSIBLE
    assert "authentication required" in startup.reason
    assert "destroyed" in startup.risk_if_continuing
    assert startup.at_risk is True


def test_the_rendered_report_warns_about_the_gaps() -> None:
    rendered = archive_for("switch-2950-locked.ytx.pub").render()

    assert "PARTIAL" in rendered
    assert "unrecoverable" in rendered


def test_cryptographic_material_is_reported_as_unknown_not_absent() -> None:
    """Not knowing is different from knowing there is nothing there."""
    bundle = archive_for("switch-2950-locked.ytx.pub")
    keys = next(a for a in bundle.artifacts if a.name == "cryptographic_keys")

    assert keys.status is ArtifactStatus.UNKNOWN
    assert keys.at_risk is True


# -- manifest and on-disk bundle -------------------------------------------


def test_the_manifest_records_gaps_as_well_as_contents(tmp_path: Path) -> None:
    """A bundle must be self-describing to someone who finds it later."""
    bundle = archive_for("switch-2950-locked.ytx.pub")
    directory = bundle.write(tmp_path / "bundle")

    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["status"] == "partial"
    statuses = {a["artifact"]: a["status"] for a in manifest["artifacts"]}
    assert statuses["boot_transcript"] == "captured"
    assert statuses["startup_config"] == "inaccessible"
    assert any("risk_if_continuing" in a for a in manifest["artifacts"])


def test_salvaged_configuration_carries_a_warning_header(tmp_path: Path) -> None:
    """The file will outlive the moment and the context.

    Someone finding a salvaged config later must be able to tell it is not
    theirs.
    """
    session = session_for("router-1760-intake.ytx.pub")
    result = intake(session)
    bundle = archive(session, result)
    directory = bundle.write(tmp_path / "bundle")

    written = [p.name for p in directory.iterdir()]
    assert "boot_transcript.txt" in written

    for path in directory.glob("*config.txt"):
        assert "previous owner" in path.read_text(encoding="utf-8")


def test_an_identified_device_records_its_facts() -> None:
    session = session_for("router-1760-intake.ytx.pub")
    bundle = archive(session, intake(session))
    facts = next(a for a in bundle.artifacts if a.name == "device_facts")

    assert facts.status is ArtifactStatus.CAPTURED
    assert facts.content is not None
    assert "1760" in facts.content


# -- the reset gate --------------------------------------------------------


def test_reset_is_refused_without_an_archive() -> None:
    decision = decide(None)

    assert decision.allowed is False
    assert "has not been archived" in decision.reason


def test_reset_may_proceed_without_an_archive_if_loss_is_accepted() -> None:
    decision = decide(None, accept_config_loss=True)

    assert decision.allowed is True
    assert "accepted explicitly" in decision.reason


def test_a_partial_archive_permits_reset_but_states_the_cost() -> None:
    """Refusing here would make the tool useless for its main case.

    A locked second-hand device can only ever produce a PARTIAL archive. The
    right response is to force the cost into the confirmation, not to block.
    """
    bundle = archive_for("switch-2950-locked.ytx.pub")
    decision = decide(bundle)

    assert decision.allowed is True
    assert "PARTIAL" in decision.reason
    assert "startup_config" in decision.at_risk

    rendered = decision.render()
    assert "about to be destroyed" in rendered
    assert "startup_config" in rendered


def test_the_decision_lists_every_artifact_at_risk() -> None:
    bundle = archive_for("switch-2950-locked.ytx.pub")
    at_risk = set(decide(bundle).at_risk)

    assert {"startup_config", "running_config", "cryptographic_keys"} <= at_risk
