"""The front door.

Someone who bought a mystery Catalyst does not want to learn an eight-stage
lifecycle before they can find out what they have. ``rescue`` runs the
primitives in order, reports what it found, and recommends the next command --
then stops, because everything it might do next is destructive and that is a
decision for a person.

It is the difference between an engineering framework and a tool people use. The
granular verbs remain for anyone who wants them; this is what the README opens
with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ciscoyoke.identify.probe import Intake, intake
from ciscoyoke.lifecycle.archive import Archive, PreservationStatus, archive
from ciscoyoke.lifecycle.health import HealthReport, assess
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State


class Recommendation(StrEnum):
    """What this device needs next."""

    NOTHING = "nothing"
    """Reachable and configured. Hand it to normal automation."""

    ACCESS_RECOVERY = "access_recovery"
    """Locked. Needs a password recovery, which is platform-specific."""

    IMAGE_RESCUE = "image_rescue"
    """Stranded with no bootable image."""

    RESET = "reset"
    """Reachable but carrying someone else's configuration."""

    INVESTIGATE = "investigate"
    """Something is wrong that this tool should not guess at."""

    CHECK_CABLE = "check_cable"
    """No signal at all."""

    STOP = "stop"
    """Continuing would destroy something. Explicitly refuses to recommend."""


_ADVICE: dict[Recommendation, str] = {
    Recommendation.NOTHING: "Device is reachable. Hand off to netmiko or Ansible.",
    Recommendation.ACCESS_RECOVERY: "ciscoyoke recover access {target}",
    Recommendation.IMAGE_RESCUE: "ciscoyoke recover image {target} --image <ios.bin>",
    Recommendation.RESET: "ciscoyoke reset {target}",
    Recommendation.INVESTIGATE: "ciscoyoke console {target}",
    Recommendation.CHECK_CABLE: "ciscoyoke doctor --port {target}",
    Recommendation.STOP: "Stopping: see the warning above before continuing.",
}


@dataclass(frozen=True, slots=True)
class RescueReport:
    """What one pass over a device established, and what it needs."""

    target: str
    intake: Intake
    archive: Archive
    health: HealthReport
    recommendation: Recommendation
    reason: str = ""
    steps: tuple[tuple[str, str], ...] = field(default=())

    @property
    def next_command(self) -> str:
        return _ADVICE[self.recommendation].format(target=self.target)

    def render(self) -> str:
        lines = [f"RESCUE PLAN — {self.target}", ""]
        for index, (name, outcome) in enumerate(self.steps, start=1):
            lines.append(f"  {index}. {name:<28} {outcome}")

        lines += ["", "Nothing destructive has happened.", ""]
        if self.reason:
            lines += [self.reason, ""]
        lines += ["Recommended next action:", f"  {self.next_command}"]
        return "\n".join(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "target": self.target,
            "state": self.intake.observation.state.value,
            "confidence": self.intake.observation.confidence.value,
            "facts": self.intake.facts.to_json(),
            "preservation": self.archive.status.value,
            "preservation_gaps": [a.name for a in self.archive.at_risk],
            "health": self.health.to_json(),
            "recommendation": self.recommendation.value,
            "reason": self.reason,
            "next_command": self.next_command,
        }


def rescue(session: Session, target: str) -> RescueReport:
    """Diagnose, preserve, and recommend. Changes nothing destructive.

    Every step here is read-only, which is what makes it safe to point at
    hardware in unknown condition -- and therefore safe to recommend as the
    first thing anyone runs.
    """
    steps: list[tuple[str, str]] = []

    steps.append(("Diagnose console", "✓"))

    found = intake(session)
    state = found.observation.state
    steps.append(
        ("Identify current state", f"{state.value.upper()} ({found.observation.confidence.value})")
    )

    bundle = archive(session, found)
    captured = len([a for a in bundle.artifacts if a.status.value == "captured"])
    gaps = len(bundle.at_risk)
    summary = bundle.status.value.upper()
    if bundle.status is PreservationStatus.PARTIAL:
        summary += f" — {captured} captured, {gaps} inaccessible"
    steps.append(("Preserve available evidence", summary))

    report = assess(session.tracker.buffer.text, found.facts)
    steps.append(("Assess health", report.worst.value))

    recommendation, reason = _recommend(found, report)
    steps.append(("Recovery required", recommendation.value.replace("_", " ")))

    return RescueReport(
        target=target,
        intake=found,
        archive=bundle,
        health=report,
        recommendation=recommendation,
        reason=reason,
        steps=tuple(steps),
    )


def _recommend(found: Intake, health: HealthReport) -> tuple[Recommendation, str]:
    state = found.observation.state

    if state is State.DESTRUCTIVE_RECOVERY_GUARD:
        return (
            Recommendation.STOP,
            "This device has password recovery disabled. Interrupting its boot "
            "would destroy the startup configuration, and the archive above "
            "shows what could not be read first.",
        )

    if state in (State.SILENT, State.UNKNOWN):
        return (
            Recommendation.CHECK_CABLE,
            "No signal at this baud rate. Check the cable orientation, the "
            "power, and the line speed.",
        )

    if state in (State.BOOTLOADER, State.ROMMON):
        return (
            Recommendation.IMAGE_RESCUE,
            "The device stopped before IOS started. If no bootable image is "
            "present it needs one supplied; ciscoyoke will never fetch one.",
        )

    if found.locked:
        return (
            Recommendation.ACCESS_RECOVERY,
            "The device is configured but its credentials are unknown. Recovery "
            "is platform-specific and, on a Catalyst, needs someone at the "
            "device to hold the Mode button.",
        )

    if health.faults:
        detail = "; ".join(f"{c.name}: {c.detail}" for c in health.faults)
        return (
            Recommendation.INVESTIGATE,
            f"The device is reachable but reported problems — {detail}",
        )

    if state in (State.PRIV_EXEC, State.USER_EXEC):
        return (
            Recommendation.RESET,
            "The device is reachable and carrying a configuration. Resetting "
            "gives a known-empty baseline; archive first if that configuration "
            "matters.",
        )

    return (
        Recommendation.INVESTIGATE,
        f"The device is at {state.value}, which has no automatic next step.",
    )
