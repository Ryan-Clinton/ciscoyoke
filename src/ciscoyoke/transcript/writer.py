"""Incremental transcript writing, so a capture survives what interrupts it.

The existing recorder holds everything in memory and writes once at the end.
That is fine for a scripted operation and wrong for a capture session, where
the interesting events are exactly the ones that end the session badly: the
cable pulled, the device wedged, the process killed, the laptop asleep.

A capture that loses the boot it just recorded because the switch crashed
afterwards is worse than useless -- it burns the one run you had.

JSONL makes this straightforward: the header goes down first, each record is
appended as a line, and the file is valid after every write. Flushing is
explicit rather than left to buffering, because the whole point is surviving
an ending nobody chose.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import TracebackType
from typing import TextIO

from ciscoyoke.transcript.schema import (
    SCHEMA_VERSION,
    Direction,
    Record,
    Transcript,
)


class TranscriptWriter:
    """Appends transcript records to a file as they happen.

    Always writes a *raw* transcript: a capture straight off a device has not
    been scrubbed and must not masquerade as shareable. The file gets
    owner-only permissions where the platform honours them.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None
        self._count = 0

    def open(self) -> TranscriptWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="\n")
        header = {"v": SCHEMA_VERSION, "kind": "raw"}
        self._handle.write(json.dumps(header, separators=(",", ":")) + "\n")
        self._handle.flush()

        # POSIX honours this; Windows relies on the profile ACL instead.
        with contextlib.suppress(OSError, NotImplementedError):
            self.path.chmod(0o600)
        return self

    def write(self, record: Record) -> None:
        if self._handle is None:
            raise RuntimeError("writer is not open")
        self._handle.write(
            json.dumps(record.to_json(), separators=(",", ":")) + "\n"
        )
        # Flushed per record. A capture is worth more than the syscalls: the
        # events worth recording are the ones that stop the process.
        self._handle.flush()
        self._count += 1

    def write_rx(self, timestamp: float, data: bytes) -> None:
        self.write(Record(timestamp, Direction.RX, data))

    def write_tx(self, timestamp: float, data: bytes, *, secret: bool = False) -> None:
        self.write(Record(timestamp, Direction.TX, data, secret=secret))

    @property
    def count(self) -> int:
        return self._count

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None

    def __enter__(self) -> TranscriptWriter:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def transcript_from(records: list[Record]) -> Transcript:
    return Transcript(records=tuple(records), public=False)
