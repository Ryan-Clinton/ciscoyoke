"""Passive capture: listen to a device and write down everything it says.

The single most valuable thing to run against unfamiliar hardware, and
deliberately the least clever. It transmits nothing. A device mid-boot, mid-
transfer or in an unknown state cannot be disturbed by something that only
reads, which makes this safe to point at a box whose condition nobody knows --
and that is exactly the box worth recording.

A cold-boot capture is the highest-value single artifact for this project: one
power-on yields the bootstrap banner, POST results, flash initialisation,
whether password recovery is disabled, the model, the IOS version and the shape
of the prompt. Every one of those is currently guessed at from documentation.

Written incrementally (see :mod:`ciscoyoke.transcript.writer`), because the
capture that matters is often the one that ends badly.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ciscoyoke.stream.tracker import Observation, State, StateTracker
from ciscoyoke.transcript.writer import TranscriptWriter
from ciscoyoke.transport.serial_ import DEFAULT_BAUD, printable_ratio

#: Called with each chunk as it arrives, for live display.
Echo = Callable[[bytes], None]


@dataclass(slots=True)
class CaptureStats:
    """What a capture session saw."""

    bytes_received: int = 0
    records: int = 0
    duration: float = 0.0
    states_seen: list[State] = field(default_factory=list)
    printable_ratio: float = 1.0
    final_state: State = State.UNKNOWN

    @property
    def looks_like_wrong_baud(self) -> bool:
        """Whether the stream looks like noise rather than console text.

        IOS output is effectively ASCII, so a low printable ratio over a
        reasonable sample is the signature of a mismatched line speed. Saying
        so beats letting someone record twenty minutes of garbage and only
        discover it when the fixture will not parse.
        """
        return self.bytes_received > 64 and self.printable_ratio < 0.75

    def render(self, path: Path) -> str:
        lines = [
            "",
            f"  Captured        {self.bytes_received} bytes in "
            f"{self.records} records over {self.duration:.0f}s",
            f"  Final state     {self.final_state.value}",
        ]
        if self.states_seen:
            seen = " -> ".join(state.value for state in self.states_seen[:8])
            lines.append(f"  States seen     {seen}")
        if self.looks_like_wrong_baud:
            lines += [
                "",
                f"  WARNING: only {self.printable_ratio:.0%} of the bytes are "
                f"printable, which usually means the line speed is wrong.",
                "  Try another rate: --baud 115200 (or 38400, 19200).",
            ]
        lines += [
            "",
            f"  Written to      {path}",
            "",
            "  This is a RAW transcript. It is private by default and may contain",
            "  a previous owner's credentials. Scrub it before sharing:",
            f"    ciscoyoke transcript scrub {path}",
        ]
        return "\n".join(lines)


def capture(
    read: Callable[[], bytes],
    writer: TranscriptWriter,
    *,
    duration: float | None = None,
    quiet_after: float | None = None,
    echo: Echo | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    tracker: StateTracker | None = None,
) -> CaptureStats:
    """Record everything a device says until told to stop.

    ``duration`` is a hard ceiling. ``quiet_after`` stops once the device has
    said nothing for that long, which is the useful stop condition for a boot:
    it ends when the device settles rather than after a guessed interval.

    Neither is required -- with both unset this runs until interrupted, which
    is what you want when waiting for someone to press the power switch.
    """
    states = tracker if tracker is not None else StateTracker()
    stats = CaptureStats()

    started = clock()
    last_data_at = started
    printable_total = 0.0
    samples = 0

    while True:
        now = clock()
        stats.duration = now - started

        if duration is not None and stats.duration >= duration:
            break

        data = read()
        if data:
            last_data_at = now
            stats.bytes_received += len(data)
            stats.records += 1
            writer.write_rx(now - started, data)

            printable_total += printable_ratio(data)
            samples += 1
            stats.printable_ratio = printable_total / samples

            for observation in states.feed(data):
                stats.states_seen.append(observation.state)

            if echo is not None:
                echo(data)
        elif quiet_after is not None and now - last_data_at >= quiet_after:
            break
        else:
            sleep(0.02)

    stats.final_state = states.current.state
    return stats


@dataclass(frozen=True, slots=True)
class BaudProbe:
    """One candidate line speed and how console-like its output looked."""

    baud: int
    bytes_seen: int
    printable: float

    @property
    def plausible(self) -> bool:
        return self.bytes_seen > 16 and self.printable >= 0.75

    def render(self) -> str:
        verdict = "looks like console text" if self.plausible else "noise or silence"
        return (
            f"  {self.baud:>7}  {self.bytes_seen:>6} bytes  "
            f"{self.printable:>5.0%} printable  {verdict}"
        )


def best_baud(probes: list[BaudProbe]) -> int | None:
    """Pick the most plausible rate, or none if nothing looked like text.

    Returning None matters: "no rate produced console output" is a real
    finding, usually meaning the cable is wrong way round or the device is off,
    and it should not be dressed up as a best guess.
    """
    candidates = [probe for probe in probes if probe.plausible]
    if not candidates:
        return None
    return max(candidates, key=lambda probe: (probe.printable, probe.bytes_seen)).baud


def summarise(observation: Observation) -> str:
    return f"{observation.state.value} ({observation.confidence.value})"


DEFAULT_QUIET_AFTER = 5.0
DEFAULT_BAUD_PROBE_SECONDS = 3.0

__all__ = [
    "DEFAULT_BAUD",
    "DEFAULT_BAUD_PROBE_SECONDS",
    "DEFAULT_QUIET_AFTER",
    "BaudProbe",
    "CaptureStats",
    "best_baud",
    "capture",
    "summarise",
]
