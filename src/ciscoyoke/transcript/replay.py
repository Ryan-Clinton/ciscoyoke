"""Replay a recording through the state tracker, to see what the tool concluded.

The maintainer's half of a remote fix. A report from hardware nobody here owns
arrives as a transcript; this shows, byte-accurately, every state the tracker
entered, the evidence for each, every command that was sent, and where the
stream ended -- including the last line that looked like the device waiting for
an answer the tool never gave.

From there the loop is the one the 2950 established: commit the transcript as
a fixture, write the test that replays it and fails, change the code until it
passes. ``FakeDevice`` in :mod:`ciscoyoke.transcript.fake` replays the same file
against a real ``Session``, strictly or not, when a whole playbook needs driving.
"""

from __future__ import annotations

from dataclasses import dataclass

from ciscoyoke.diagnose import unrecognised_prompt
from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.schema import Direction, Transcript


@dataclass(frozen=True, slots=True)
class Event:
    """One line of the timeline: a command sent, or a state entered."""

    t: float
    kind: str
    detail: str
    evidence: str = ""

    def render(self) -> str:
        suffix = f"   <- {self.evidence}" if self.evidence else ""
        return f"{self.t:9.2f}s  {self.kind:<6} {self.detail}{suffix}"

    def to_json(self) -> dict[str, object]:
        return {"t": self.t, "kind": self.kind, "detail": self.detail, "evidence": self.evidence}


@dataclass(frozen=True, slots=True)
class Replay:
    events: tuple[Event, ...]
    final_state: State
    unrecognised_prompt: str | None
    source: str | None

    def render(self) -> str:
        lines = [f"Replay of: {self.source or 'unlabelled transcript'}", ""]
        lines += [event.render() for event in self.events]
        lines += ["", f"Final state: {self.final_state.value}"]
        if self.unrecognised_prompt and self.final_state in (State.UNKNOWN, State.BOOTING):
            lines.append(
                f"Ends waiting at an unrecognised prompt: {self.unrecognised_prompt!r}"
            )
            lines.append(
                "  -> likely fix: a detector for this prompt in "
                "ciscoyoke/stream/signals.py, and the answer to it in the playbook."
            )
        elif self.unrecognised_prompt:
            lines.append(f"Last prompt-like line: {self.unrecognised_prompt!r}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "source": self.source,
            "final_state": self.final_state.value,
            "unrecognised_prompt": self.unrecognised_prompt,
            "events": [event.to_json() for event in self.events],
        }


def _printable(data: bytes) -> str:
    return data.decode("latin-1").replace("\r", "\\r").replace("\n", "\\n")


def replay(transcript: Transcript) -> Replay:
    """Feed a recording through a fresh tracker and record what it concluded."""
    tracker = StateTracker()
    events: list[Event] = []
    for record in transcript:
        if record.direction is Direction.TX:
            tracker.note_sent(record.data)
            shown = "<secret>" if record.secret else _printable(record.data)
            events.append(Event(record.t, "sent", shown))
            continue
        for observation in tracker.feed(record.data):
            evidence = ", ".join(
                f"{signal.kind.value}={signal.value.strip()!r}"
                for signal in observation.evidence
            )
            events.append(
                Event(
                    record.t,
                    "state",
                    f"{observation.state.value} "
                    f"({observation.basis.value}/{observation.confidence.value})",
                    evidence,
                )
            )
    return Replay(
        events=tuple(events),
        final_state=tracker.current.state,
        unrecognised_prompt=unrecognised_prompt(tracker.buffer.text),
        source=transcript.source,
    )
