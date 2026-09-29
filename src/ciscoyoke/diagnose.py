"""What to write down when a run fails, so it can be fixed without the device.

A failure on hardware nobody here owns is only fixable from what the failing run
left behind. "Timed out" is useless; what fixes it is *what the device said
that the tool did not understand*. On the 2950 every defect found came down to
one of three things, and each is captured here:

* a prompt the detectors did not recognise -- the last prompt-looking line is
  recorded verbatim, which usually makes the fix a new pattern;
* a step that expected one state and saw another -- the step, the expectation,
  the observation and the tracker's whole state history with its evidence;
* the host side -- adapter chipset, USB IDs, OS and library versions -- because
  "nothing at all came back" is as often the cable as the switch.

The record is written next to the run's raw transcript and is private like it.
``ciscoyoke report`` is what turns the pair into something shareable.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ciscoyoke import __version__
from ciscoyoke.stream.signals import strip_syslog
from ciscoyoke.stream.tracker import StateTracker
from ciscoyoke.transcript.schema import Direction, Record, Transcript
from ciscoyoke.transcript.scrub import scrub
from ciscoyoke.transport.identity import PortIdentity

# How much of the end of the stream to keep. Enough to see the last exchange
# in context, small enough to read in an issue.
TAIL_CHARS = 1500

# What a prompt ends with: a question, a field, a bracketed default, or an
# exec-style marker. Deliberately loose -- a false positive costs a maintainer
# a glance; a false negative hides the one line that explains the failure.
_PROMPTISH = re.compile(r"(?:\?|:|\]|\)|>|#|\$)\s*$")


def unrecognised_prompt(stream: str) -> str | None:
    """The last line that looks like the device waiting for an answer.

    Log messages are removed first (they are exactly what lands after a
    prompt), then the last non-blank line is returned if it ends like a prompt.
    """
    tail = strip_syslog(stream[-4000:])
    for line in reversed(re.split(r"[\r\n]+", tail)):
        candidate = line.strip()
        if not candidate:
            continue
        return candidate if _PROMPTISH.search(candidate) else None
    return None


def _scrubbed(text: str) -> str:
    """Scrub a free-standing fragment with the transcript scrubber."""
    result = scrub(Transcript(records=(Record(0.0, Direction.RX, text.encode("latin-1")),)))
    return "".join(r.data.decode("latin-1") for r in result.transcript)


def adapter_facts(identity: PortIdentity) -> dict[str, Any]:
    return {
        "device": identity.device,
        "adapter": identity.adapter,
        "vid": f"{identity.vid:04x}" if identity.vid is not None else None,
        "pid": f"{identity.pid:04x}" if identity.pid is not None else None,
        "identity_strength": identity.strength.value,
        "description": identity.description,
        "manufacturer": identity.manufacturer,
    }


def host_facts() -> dict[str, Any]:
    try:
        import serial

        pyserial = str(serial.__version__)
    except ImportError:  # pragma: no cover - pyserial is a hard dependency
        pyserial = None
    return {
        "ciscoyoke": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pyserial": pyserial,
    }


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """Everything a maintainer needs to reason about one failed run."""

    operation: str
    error: str
    failed_step: str | None
    expected: tuple[str, ...]
    observed: str
    unrecognised_prompt: str | None
    last_output: str
    state_history: tuple[dict[str, Any], ...]
    adapter: dict[str, Any]
    host: dict[str, Any]
    profile: dict[str, Any] | None
    model: str | None
    transcript: str | None
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {
            "report_version": 1,
            "operation": self.operation,
            "error": self.error,
            "failed_step": self.failed_step,
            "expected": list(self.expected),
            "observed": self.observed,
            "unrecognised_prompt": self.unrecognised_prompt,
            "last_output": self.last_output,
            "state_history": list(self.state_history),
            "adapter": self.adapter,
            "host": self.host,
            "profile": self.profile,
            "model": self.model,
            "transcript": self.transcript,
            "created_at": self.created_at,
        }

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        return path

    def summary(self) -> str:
        lines = [f"What was seen: {self.observed}"]
        if self.failed_step:
            lines.append(
                f"Failed step:   {self.failed_step} "
                f"(expected {', '.join(self.expected) or '?'})"
            )
        if self.unrecognised_prompt:
            lines.append(f"Last prompt:   {self.unrecognised_prompt!r}")
        return "\n".join(lines)


def record_failure(
    *,
    operation: str,
    error: BaseException,
    tracker: StateTracker,
    identity: PortIdentity,
    profile: dict[str, Any] | None = None,
    model: str | None = None,
    transcript: Path | None = None,
) -> FailureRecord:
    """Build the record for a failure, reading what the exception carries."""
    step = getattr(error, "step", None)
    expected: tuple[str, ...] = ()
    if step is not None:
        # A playbook Step names what it expects; a HumanStep, its evidence.
        states = getattr(step, "expect", None) or getattr(step, "evidence_states", None)
        expected = tuple(state.value for state in states or ())

    stream = tracker.buffer.text
    history = tuple(
        {
            "state": observation.state.value,
            "basis": observation.basis.value,
            "confidence": observation.confidence.value,
            "evidence": [
                {"signal": signal.kind.value, "value": _scrubbed(signal.value)}
                for signal in observation.evidence
            ],
        }
        for observation in tracker.history
    )
    prompt = unrecognised_prompt(stream)
    return FailureRecord(
        operation=operation,
        error=_scrubbed(str(error)),
        failed_step=getattr(step, "name", None),
        expected=expected,
        observed=tracker.current.state.value,
        unrecognised_prompt=_scrubbed(prompt) if prompt else None,
        last_output=_scrubbed(stream[-TAIL_CHARS:]),
        state_history=history,
        adapter=adapter_facts(identity),
        host=host_facts(),
        profile=profile,
        model=model,
        transcript=str(transcript) if transcript else None,
    )


def failure_path(transcript: Path) -> Path:
    """Where a run's failure record lives: beside its transcript."""
    return transcript.with_name(transcript.stem + ".failure.json")
