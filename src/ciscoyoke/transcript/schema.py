"""Versioned transcript records.

Two formats, deliberately distinct rather than one format with a flag:

``.ytx``      raw and private. May contain anything the console said, including
              secrets nobody recognised. Never loadable as a test fixture.
``.ytx.pub``  scrubbed and shareable. The only form that may be committed.

Keeping them apart at the *schema* level -- different required fields, checked
on load -- means a raw recording someone forgot to clean cannot be silently
committed as a fixture. It fails to parse rather than leaking.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 1

RAW_SUFFIX = ".ytx"
PUBLIC_SUFFIX = ".ytx.pub"


class Direction(StrEnum):
    RX = "rx"
    TX = "tx"


class TranscriptFormatError(ValueError):
    """A transcript could not be parsed, or was the wrong kind."""


@dataclass(frozen=True, slots=True)
class Record:
    """One I/O event.

    ``secret`` marks payloads that the credential interface flagged *before the
    recorder ever saw them*. It is not a post-hoc annotation; a record written
    with ``secret=True`` never contained the plaintext in the first place.
    """

    t: float
    direction: Direction
    data: bytes
    secret: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "v": SCHEMA_VERSION,
            "t": round(self.t, 4),
            "dir": self.direction.value,
            "b": self.data.decode("latin-1"),
            **({"secret": True} if self.secret else {}),
        }

    @classmethod
    def from_json(cls, obj: dict[str, object]) -> Record:
        # Values arrive from JSON as `object`; coerce through `str` so a
        # malformed field raises ValueError here rather than producing a Record
        # with a nonsense type that fails somewhere less obvious later.
        try:
            return cls(
                t=float(str(obj["t"])),
                direction=Direction(str(obj["dir"])),
                data=str(obj["b"]).encode("latin-1"),
                secret=bool(obj.get("secret", False)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise TranscriptFormatError(f"bad record: {obj!r}") from exc


@dataclass(frozen=True, slots=True)
class Transcript:
    """A recorded session, plus how it came to be shareable."""

    records: tuple[Record, ...]
    public: bool = False
    scrub_version: int | None = None
    source: str | None = None

    def __iter__(self) -> Iterator[Record]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    @property
    def duration(self) -> float:
        return self.records[-1].t if self.records else 0.0

    def header(self) -> dict[str, object]:
        header: dict[str, object] = {
            "v": SCHEMA_VERSION,
            "kind": "public" if self.public else "raw",
        }
        if self.public:
            # Provenance is mandatory on a public transcript: a fixture whose
            # scrub history is unknown cannot be trusted as CI input.
            header["scrub_version"] = self.scrub_version
            header["source"] = self.source
        return header


def write(path: Path, transcript: Transcript) -> None:
    """Write a transcript as JSONL with a header line.

    A raw transcript is written with owner-only permissions where the platform
    supports it. On Windows this is a no-op and the file relies on the user
    profile ACL instead -- stated rather than silently assumed.
    """
    lines = [json.dumps(transcript.header(), separators=(",", ":"))]
    lines.extend(
        json.dumps(record.to_json(), separators=(",", ":")) for record in transcript
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not transcript.public:
        # Best effort: POSIX honours this, Windows does not implement it and the
        # file relies on the user profile ACL instead. Stated in the docstring
        # rather than silently assumed either way.
        with contextlib.suppress(OSError, NotImplementedError):
            path.chmod(0o600)


def read(path: Path, *, require_public: bool = False) -> Transcript:
    """Read a transcript.

    ``require_public`` is how the test suite refuses raw recordings. It is not
    advisory: a raw transcript handed to a fixture loader raises rather than
    being read.
    """
    raw_lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not raw_lines:
        raise TranscriptFormatError(f"{path} is empty")

    try:
        header = json.loads(raw_lines[0])
    except json.JSONDecodeError as exc:
        raise TranscriptFormatError(f"{path}: unreadable header") from exc

    if not isinstance(header, dict) or "kind" not in header:
        raise TranscriptFormatError(f"{path}: missing transcript header")

    public = header.get("kind") == "public"
    if require_public and not public:
        raise TranscriptFormatError(
            f"{path} is a raw transcript and may not be used as a fixture; "
            f"scrub it first (ciscoyoke transcript scrub)"
        )
    if public and header.get("scrub_version") is None:
        raise TranscriptFormatError(f"{path}: public transcript without provenance")

    records = tuple(Record.from_json(json.loads(line)) for line in raw_lines[1:])
    return Transcript(
        records=records,
        public=public,
        scrub_version=header.get("scrub_version"),
        source=header.get("source"),
    )


def from_records(records: Iterable[Record]) -> Transcript:
    return Transcript(records=tuple(records))
