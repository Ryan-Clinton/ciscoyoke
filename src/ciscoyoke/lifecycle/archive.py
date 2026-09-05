"""Preserve what can be read; disclose what cannot.

An earlier draft of the specification described archive as meaning "nothing is
lost". That is not achievable and was worth correcting: you cannot read a
startup-config you cannot authenticate to, and a device that will not identify
itself will not hand over its flash listing either. Promising total preservation
would make the guarantee least true exactly where it matters most -- on the
locked second-hand device whose previous owner's configuration is about to be
erased.

The honest claim, which this module implements:

    **Archive captures everything safely observable before mutation, and
    explicitly records everything it could not preserve.**

Every artifact gets a status and a reason, and every gap carries what
continuing would cost. A ``PARTIAL`` archive does not block a recovery -- it
informs the confirmation, so the operator decides knowing precisely what they
may be about to lose rather than discovering it afterwards.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ciscoyoke.identify.probe import Intake
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State

ARCHIVE_VERSION = 1

# A salvaged configuration is somebody else's network. It gets a header saying
# so, because the file will outlive the moment and the context.
_WARNING_HEADER = (
    "! Salvaged by ciscoyoke from a second-hand device.\n"
    "! This configuration may belong to a previous owner and may contain their\n"
    "! addressing, credentials and cryptographic material. Do not publish it.\n"
    "!\n"
)


class ArtifactStatus(StrEnum):
    """Whether one thing could be preserved."""

    CAPTURED = "captured"
    INACCESSIBLE = "inaccessible"
    UNKNOWN = "unknown"
    ABSENT = "absent"


class PreservationStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Artifact:
    """One thing the archive tried to capture."""

    name: str
    status: ArtifactStatus
    reason: str = ""
    risk_if_continuing: str = ""
    content: str | None = field(default=None, repr=False)
    sha256: str | None = None

    @property
    def at_risk(self) -> bool:
        """Whether continuing could destroy something never captured."""
        return self.status in (ArtifactStatus.INACCESSIBLE, ArtifactStatus.UNKNOWN)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "artifact": self.name,
            "status": self.status.value,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.risk_if_continuing:
            payload["risk_if_continuing"] = self.risk_if_continuing
        if self.sha256:
            payload["sha256"] = self.sha256
        return payload


def _captured(name: str, content: str) -> Artifact:
    digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
    return Artifact(
        name=name, status=ArtifactStatus.CAPTURED, content=content, sha256=digest
    )


def _inaccessible(name: str, reason: str, risk: str) -> Artifact:
    return Artifact(
        name=name,
        status=ArtifactStatus.INACCESSIBLE,
        reason=reason,
        risk_if_continuing=risk,
    )


def _unknown(name: str, reason: str, risk: str) -> Artifact:
    return Artifact(
        name=name,
        status=ArtifactStatus.UNKNOWN,
        reason=reason,
        risk_if_continuing=risk,
    )


@dataclass(frozen=True, slots=True)
class Archive:
    """The evidence bundle, and an honest account of its gaps."""

    artifacts: tuple[Artifact, ...]
    device_identity: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def status(self) -> PreservationStatus:
        captured = [a for a in self.artifacts if a.status is ArtifactStatus.CAPTURED]
        if not captured:
            return PreservationStatus.NONE
        if any(a.at_risk for a in self.artifacts):
            return PreservationStatus.PARTIAL
        return PreservationStatus.COMPLETE

    @property
    def at_risk(self) -> tuple[Artifact, ...]:
        """Artifacts that continuing could destroy without ever having read."""
        return tuple(a for a in self.artifacts if a.at_risk)

    @property
    def complete(self) -> bool:
        return self.status is PreservationStatus.COMPLETE

    def render(self) -> str:
        lines = [f"Preservation status: {self.status.value.upper()}", ""]
        marks = {
            ArtifactStatus.CAPTURED: "✓",
            ArtifactStatus.INACCESSIBLE: "?",
            ArtifactStatus.UNKNOWN: "?",
            ArtifactStatus.ABSENT: "-",
        }
        for artifact in self.artifacts:
            detail = f"   {artifact.reason}" if artifact.reason else ""
            lines.append(f"  {marks[artifact.status]} {artifact.name:22}{detail}")

        if self.at_risk:
            lines += [
                "",
                "Destructive recovery may make the unavailable items "
                "unrecoverable.",
            ]
        return "\n".join(lines)

    def manifest(self) -> dict[str, Any]:
        return {
            "archive_version": ARCHIVE_VERSION,
            "created_at": self.created_at,
            "status": self.status.value,
            "device_identity": self.device_identity,
            "artifacts": [artifact.to_json() for artifact in self.artifacts],
        }

    def write(self, directory: Path) -> Path:
        """Write the bundle, and return the directory it went to.

        Captured configuration is written with a warning header and owner-only
        permissions. The manifest records the gaps as well as the contents, so
        the bundle is self-describing even to someone who finds it later with no
        memory of the session.
        """
        directory.mkdir(parents=True, exist_ok=True)
        # POSIX honours this; Windows relies on the profile ACL instead.
        with contextlib.suppress(OSError, NotImplementedError):
            directory.chmod(0o700)

        for artifact in self.artifacts:
            if artifact.content is None:
                continue
            path = directory / f"{artifact.name}.txt"
            body = artifact.content
            if artifact.name.endswith("config"):
                body = _WARNING_HEADER + body
            path.write_text(body, encoding="utf-8")
            with contextlib.suppress(OSError, NotImplementedError):
                path.chmod(0o600)

        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps(self.manifest(), indent=2), encoding="utf-8")
        return directory


# States from which a configuration can actually be read back.
_PRIVILEGED = (State.PRIV_EXEC,)

_AUTH_REQUIRED = "authentication required"
_MAY_BE_LOST = "may_be_lost"


def archive(session: Session, intake_result: Intake) -> Archive:
    """Capture what this device will disclose, and record what it will not.

    Read-only. Runs `show` commands where a privileged prompt is available and
    records an explicit gap where it is not -- which is the common case on the
    devices this tool exists for.
    """
    artifacts: list[Artifact] = []

    # Always available: what the device said while we were listening. On a
    # locked device this is the only preservation possible, which is exactly
    # why it is captured first rather than treated as a consolation prize.
    artifacts.append(_captured("boot_transcript", session.tracker.buffer.text))

    facts = intake_result.facts
    if facts.identified:
        artifacts.append(
            _captured("device_facts", json.dumps(facts.to_json(), indent=2))
        )
    else:
        artifacts.append(
            _unknown(
                "device_facts",
                intake_result.note,
                "identity will be unrecoverable once the device is erased",
            )
        )

    state = intake_result.observation.state

    if state not in _PRIVILEGED:
        reason = (
            _AUTH_REQUIRED
            if intake_result.locked
            else f"device is at {state.value}, not a privileged prompt"
        )
        for name in ("running_config", "startup_config", "flash_listing"):
            artifacts.append(
                _inaccessible(
                    name,
                    reason,
                    "will be destroyed by erase or recovery" if "config" in name
                    else "contents will not be recorded before any change",
                )
            )
        artifacts.append(
            _unknown(
                "private_config",
                "not readable on any platform without privileged access",
                _MAY_BE_LOST,
            )
        )
        artifacts.append(
            _unknown(
                "cryptographic_keys",
                "presence not determinable without privileged access",
                _MAY_BE_LOST,
            )
        )
        return Archive(
            artifacts=tuple(artifacts),
            device_identity=facts.to_json(),
        )

    for name, command in (
        ("running_config", b"show running-config\r"),
        ("startup_config", b"show startup-config\r"),
        ("flash_listing", b"show flash:\r"),
    ):
        artifacts.append(_capture_command(session, name, command))

    # Even from a privileged prompt these are not readable, and saying so is
    # more useful than omitting them: an operator comparing a bundle against a
    # device needs to know what was never in scope.
    artifacts.append(
        _unknown(
            "private_config",
            "not readable through the CLI on this platform",
            _MAY_BE_LOST,
        )
    )
    artifacts.append(
        _unknown(
            "cryptographic_keys",
            "private keys cannot be exported through the CLI",
            _MAY_BE_LOST,
        )
    )

    return Archive(artifacts=tuple(artifacts), device_identity=facts.to_json())


def _capture_command(session: Session, name: str, command: bytes) -> Artifact:
    before = len(session.tracker.buffer)
    session.send(command)
    session.settle(timeout=20.0)
    session.drain_pager()
    output = session.tracker.buffer.text[before:]

    if "% Invalid input" in output or "Incomplete command" in output:
        return Artifact(
            name=name,
            status=ArtifactStatus.ABSENT,
            reason="command not supported on this platform",
        )
    if not output.strip():
        return _unknown(
            name, "device returned nothing", "may exist but was not captured"
        )
    return _captured(name, output)
