"""Versioned machine-readable results.

Every command emits this under ``--json`` so ciscoyoke is usable from shell
scripts, CI, other Python tools and any future TUI.

Confidence is carried as ``basis`` plus an ordinal ``confidence`` plus the
``evidence`` that produced it -- never as a float. A ``state_confidence`` of
0.98 would look scientific without being calibrated against anything, and would
invite arithmetic nobody has earned the right to do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ciscoyoke.stream.signals import Signal
from ciscoyoke.stream.tracker import Basis, Confidence, Observation

SCHEMA_VERSION = 1


def _signal_json(signal: Signal) -> dict[str, str]:
    return {
        "signal": signal.kind.value,
        "value": signal.value,
        "context": signal.context,
    }


@dataclass(frozen=True, slots=True)
class Finding:
    """One qualified conclusion about one attribute."""

    value: str | None
    basis: Basis = Basis.UNKNOWN
    confidence: Confidence = Confidence.LOW
    evidence: tuple[Signal, ...] = field(default=())
    reason: str | None = None
    """Why the value is what it is -- required reading when it is ``None``."""

    @classmethod
    def unknown(cls, reason: str) -> Finding:
        """An explicit non-answer, carrying why.

        A field that is absent tells the reader nothing; a field that says it
        is unknown *and why* is actionable. ``reason`` is mandatory here for
        exactly that purpose.
        """
        return cls(
            value=None,
            basis=Basis.UNKNOWN,
            confidence=Confidence.LOW,
            evidence=(),
            reason=reason,
        )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "value": self.value,
            "basis": self.basis.value,
            "confidence": self.confidence.value,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.evidence:
            payload["evidence"] = [_signal_json(signal) for signal in self.evidence]
        return payload

    @classmethod
    def from_observation(cls, observation: Observation) -> Finding:
        return cls(
            value=observation.state.value,
            basis=observation.basis,
            confidence=observation.confidence,
            evidence=observation.evidence,
        )


@dataclass(frozen=True, slots=True)
class Result:
    """A command's machine-readable output."""

    command: str
    exit_code: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "command": self.command,
            "exit_code": self.exit_code,
            **self.data,
        }
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload

    def render(self) -> str:
        return json.dumps(self.to_json(), indent=2, default=_fallback)


def _fallback(obj: Any) -> Any:
    if hasattr(obj, "to_json"):
        return obj.to_json()
    if isinstance(obj, Signal):
        return _signal_json(obj)
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)
