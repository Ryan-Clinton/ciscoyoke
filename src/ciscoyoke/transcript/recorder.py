"""Secret-aware session recording.

Redaction happens at the moment of writing, not as a cleanup pass afterwards.
A credential passed through :meth:`Recorder.record_secret_tx` is never held in
the transcript at all -- the record is created already redacted, so there is no
window in which the plaintext exists on disk waiting to be tidied up.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ciscoyoke.transcript.schema import Direction, Record, Transcript

REDACTED = b"<redacted>"


class Recorder:
    """Collects records with monotonic timestamps relative to session start."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock if clock is not None else time.monotonic
        self._start = self._clock()
        self._records: list[Record] = []

    def _now(self) -> float:
        return self._clock() - self._start

    def record_rx(self, data: bytes) -> None:
        """Record bytes received from the device."""
        if data:
            self._records.append(Record(self._now(), Direction.RX, data))

    def record_tx(self, data: bytes) -> None:
        """Record bytes transmitted to the device."""
        if data:
            self._records.append(Record(self._now(), Direction.TX, data))

    def record_secret_tx(self, data: bytes) -> None:
        """Record that a credential was sent, without recording the credential.

        ``data`` is accepted so callers do not need a separate code path, but
        its content is discarded immediately. Only its length reaches the
        transcript, and only as the fixed redaction marker -- so transcript
        length cannot be used to infer password length either.
        """
        del data
        self._records.append(Record(self._now(), Direction.TX, REDACTED, secret=True))

    @property
    def records(self) -> tuple[Record, ...]:
        return tuple(self._records)

    def transcript(self) -> Transcript:
        """The raw, private transcript. Not shareable without scrubbing."""
        return Transcript(records=tuple(self._records), public=False)

    def __len__(self) -> int:
        return len(self._records)
