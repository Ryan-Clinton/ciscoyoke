"""Replay a recorded transcript as if it were a device.

This is the test spine. Every playbook, every state transition and every
recovery path is exercised against bytes a real Cisco box actually emitted,
on a machine with no hardware attached -- which is the difference between a
tool and a toy.

The fake asserts in both directions. It feeds the recorded RX to the code under
test, and it checks that the code transmits what the real session transmitted.
A change that silently sends a different command fails the test rather than
producing a plausible-looking transcript.
"""

from __future__ import annotations

from dataclasses import dataclass

from ciscoyoke.transcript.schema import Direction, Record, Transcript


class ReplayMismatchError(AssertionError):
    """The code under test diverged from the recorded session."""


@dataclass(slots=True)
class _Pending:
    index: int
    record: Record


class FakeDevice:
    """A transport-shaped replay of a recorded session.

    Timing is preserved in the record stream but not slept on: a four-minute
    reload replays in microseconds. Deadline behaviour is still exercisable
    because :attr:`clock` advances to each record's recorded timestamp, so code
    that waits on elapsed time sees the real durations.
    """

    def __init__(self, transcript: Transcript, *, strict: bool = True) -> None:
        self._records = list(transcript.records)
        self._position = 0
        self._strict = strict
        self._clock = 0.0
        self._sent: list[bytes] = []

    # -- clock -------------------------------------------------------------

    @property
    def clock(self) -> float:
        """Virtual monotonic time, advanced to the last replayed record."""
        return self._clock

    def monotonic(self) -> float:
        """Drop-in for ``time.monotonic`` so waits are virtual, not real."""
        return self._clock

    # -- transport surface -------------------------------------------------

    def read(self) -> bytes:
        """Return the next recorded RX payload, or empty at end of stream."""
        pending = self._next_rx()
        if pending is None:
            return b""
        self._position = pending.index + 1
        self._clock = pending.record.t
        return pending.record.data

    def write(self, data: bytes) -> int:
        """Assert the code transmits what the recording transmitted."""
        self._sent.append(data)
        if not self._strict:
            return len(data)

        expected = self._next_tx()
        if expected is None:
            raise ReplayMismatchError(
                f"code sent {data!r} but the recording has no further TX events"
            )

        self._position = expected.index + 1
        self._clock = expected.record.t

        if expected.record.secret:
            # The recording redacted this, so its content cannot be compared.
            # What is still checkable -- and worth checking -- is that the code
            # chose to send *something* at this point in the exchange.
            return len(data)

        if data != expected.record.data:
            raise ReplayMismatchError(
                f"at record {expected.index}: expected {expected.record.data!r}, "
                f"code sent {data!r}"
            )
        return len(data)

    # -- introspection -----------------------------------------------------

    @property
    def sent(self) -> tuple[bytes, ...]:
        return tuple(self._sent)

    @property
    def exhausted(self) -> bool:
        return self._next_rx() is None and self._next_tx() is None

    @property
    def remaining(self) -> int:
        return len(self._records) - self._position

    # -- internals ---------------------------------------------------------

    def _next_rx(self) -> _Pending | None:
        for index in range(self._position, len(self._records)):
            record = self._records[index]
            if record.direction is Direction.RX:
                return _Pending(index, record)
            # A TX record blocks: the device said nothing more until the code
            # replies. Returning later RX would let a test pass while skipping
            # the command that was supposed to trigger it.
            return None
        return None

    def _next_tx(self) -> _Pending | None:
        for index in range(self._position, len(self._records)):
            record = self._records[index]
            if record.direction is Direction.TX:
                return _Pending(index, record)
            return None
        return None


def chunked(data: bytes, sizes: list[int]) -> list[bytes]:
    """Split ``data`` at the given offsets.

    Used by the chunk-invariance property test to simulate arbitrary serial
    read boundaries.
    """
    if not data:
        return []
    bounds = sorted({size % len(data) for size in sizes if size > 0})
    pieces: list[bytes] = []
    previous = 0
    for bound in bounds:
        if bound > previous:
            pieces.append(data[previous:bound])
            previous = bound
    pieces.append(data[previous:])
    return [piece for piece in pieces if piece]
