"""Moving an image onto a device, with the progress a long transfer needs.

Cisco warns an XMODEM transfer can take up to two hours. The engineering problem
there is not the protocol -- it is that two hours of silence is where people
conclude the tool has hung and pull the power, which on a device whose only
image was just deleted is the worst possible moment.

So progress is a first-class output, not a nicety: bytes moved, elapsed, rate,
and an estimate that is honest about being an estimate.

The console speed increase is the other half. Cisco's own recovery guidance
raises the console to 115200 for the transfer, which turns hours into minutes --
and leaves the device at a speed nobody expects if the transfer is interrupted.
That is exactly why the baud change is a journalled, compensatable mutation
rather than an implementation detail.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ciscoyoke.journal import model
from ciscoyoke.journal.model import Mutation

#: Speed Cisco's recovery documentation recommends for XMODEM.
TRANSFER_BAUD = 115200

#: Roughly what XMODEM achieves after protocol overhead, in bytes per second.
_XMODEM_THROUGHPUT = {9600: 900, 19200: 1800, 38400: 3600, 57600: 5400, 115200: 10800}


class Method(StrEnum):
    XMODEM = "xmodem"
    TFTP = "tftp"


@dataclass(frozen=True, slots=True)
class Estimate:
    """How long this is likely to take, and on what basis."""

    method: Method
    size_bytes: int
    baud: int | None
    seconds: float

    @property
    def human(self) -> str:
        minutes = self.seconds / 60
        if minutes < 1:
            return f"~{self.seconds:.0f} seconds"
        if minutes < 90:
            return f"~{minutes:.0f} minutes"
        return f"~{minutes / 60:.1f} hours"

    @property
    def basis(self) -> str:
        if self.method is Method.TFTP:
            return "TFTP over Ethernet; network-dependent"
        return f"XMODEM at {self.baud} baud, after protocol overhead"


def estimate(size_bytes: int, method: Method, baud: int = 9600) -> Estimate:
    """Predict transfer duration.

    Deliberately pessimistic for XMODEM: the throughput figures account for
    per-block acknowledgement, and an estimate that runs under is far better
    received than one that runs over.
    """
    if method is Method.TFTP:
        # A conservative 200 KB/s: enough to say "minutes, not hours" without
        # promising a number the network may not deliver.
        seconds = size_bytes / (200 * 1024)
        return Estimate(method, size_bytes, None, seconds)

    throughput = _XMODEM_THROUGHPUT.get(baud, 900)
    return Estimate(method, size_bytes, baud, size_bytes / throughput)


@dataclass(slots=True)
class Progress:
    """Live transfer state, for something to render."""

    total_bytes: int
    sent_bytes: int = 0
    started_at: float = 0.0
    now: float = 0.0

    @property
    def fraction(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(self.sent_bytes / self.total_bytes, 1.0)

    @property
    def elapsed(self) -> float:
        return max(self.now - self.started_at, 0.0)

    @property
    def rate(self) -> float:
        """Bytes per second so far, or zero before anything has moved."""
        return self.sent_bytes / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def remaining(self) -> float | None:
        """Seconds left, or None when there is not yet enough to extrapolate.

        Returning None rather than a wild number matters: an ETA that swings
        between four minutes and four hours in the first seconds destroys
        confidence in every figure the tool prints afterwards.
        """
        if self.rate <= 0 or self.sent_bytes < 4096:
            return None
        return (self.total_bytes - self.sent_bytes) / self.rate

    def render(self) -> str:
        percent = self.fraction * 100
        moved = self.sent_bytes / 1048576
        total = self.total_bytes / 1048576
        line = f"  {percent:5.1f}%  {moved:5.2f} / {total:5.2f} MiB"
        if self.rate > 0:
            line += f"  {self.rate / 1024:5.1f} KiB/s"
        remaining = self.remaining
        if remaining is not None:
            line += f"  {remaining / 60:4.1f} min left"
        else:
            line += "  estimating..."
        return line


#: Called with each progress update.
Reporter = Callable[[Progress], None]


@dataclass(frozen=True, slots=True)
class TransferStep:
    """A transfer, and the console changes it needs to make first.

    ``mutations()`` is what the journal records. The baud change appears there
    even though it is temporary -- especially because it is temporary, since an
    interrupted transfer is precisely when someone needs to know the device is
    no longer at 9600.
    """

    method: Method
    size_bytes: int
    filename: str
    from_baud: int = 9600
    to_baud: int = TRANSFER_BAUD

    @property
    def changes_baud(self) -> bool:
        return self.method is Method.XMODEM and self.to_baud != self.from_baud

    def mutations(self) -> tuple[Mutation, ...]:
        if not self.changes_baud:
            return ()
        return (model.console_baud(self.from_baud, self.to_baud),)

    def estimate(self) -> Estimate:
        baud = self.to_baud if self.changes_baud else self.from_baud
        return estimate(self.size_bytes, self.method, baud)

    def required_capabilities(self) -> tuple[str, ...]:
        return ("change_baud",) if self.changes_baud else ()

    def render(self) -> str:
        prediction = self.estimate()
        lines = [
            f"  Transfer         {self.method.value.upper()}"
            + (f" @ {self.to_baud}" if self.changes_baud else ""),
            f"  Estimated time   {prediction.human}  ({prediction.basis})",
        ]
        if self.changes_baud:
            lines.append(
                f"  Temporary change console baud {self.from_baud} -> "
                f"{self.to_baud}  (compensation recorded)"
            )
        return "\n".join(lines)


def track(
    total_bytes: int,
    sent: Callable[[], int],
    report: Reporter,
    *,
    clock: Callable[[], float] = time.monotonic,
    interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Progress:
    """Poll a transfer and report progress until it completes.

    Separated from any particular transfer implementation so the reporting can
    be tested against a stub in virtual time -- there is no way to test two
    hours of XMODEM otherwise, and untested progress reporting is how a
    percentage ends up stuck at 0% for an hour.
    """
    started = clock()
    progress = Progress(total_bytes=total_bytes, started_at=started, now=started)

    while progress.sent_bytes < total_bytes:
        progress.sent_bytes = sent()
        progress.now = clock()
        report(progress)
        if progress.sent_bytes >= total_bytes:
            break
        sleep(interval)

    return progress
