"""Byte-boundary-safe buffering for an unstructured console stream.

A serial console delivers bytes in arbitrary chunks. A prompt can straddle two
reads -- ``Router`` in one, ``#`` in the next -- so anything that inspects reads
in isolation will miss it and then hang until timeout. This module is the layer
that makes that impossible: callers never see chunk boundaries at all.

Decoding is latin-1 rather than utf-8, deliberately. latin-1 is a total function
from bytes to text: every byte decodes, one byte becomes exactly one character,
and no partial multi-byte sequence can ever split across a chunk boundary. Byte
offsets and character offsets stay identical, which is what lets the tracker
reason about positions without tracking a separate encoding state. Cisco console
output is effectively ASCII; the rare stray high byte from line noise becomes a
harmless character rather than a UnicodeDecodeError in the middle of a recovery.
"""

from __future__ import annotations

# How much of the tail the state detectors are allowed to look at. A Cisco
# prompt plus its surrounding context is far shorter than this; the cap keeps
# per-byte evaluation cheap on a long boot transcript.
TAIL_WINDOW = 512


class StreamBuffer:
    """Accumulates console bytes and exposes a stable decoded view.

    The buffer keeps everything it has been given so a transcript can be
    replayed or a boot banner re-read, but exposes :meth:`tail` for detectors
    that only care about what the device most recently said.
    """

    __slots__ = ("_data",)

    def __init__(self, initial: bytes = b"") -> None:
        self._data = bytearray(initial)

    def feed(self, data: bytes) -> None:
        """Append newly arrived bytes. Chunk size is irrelevant by design."""
        self._data.extend(data)

    @property
    def raw(self) -> bytes:
        """Everything received, unmodified."""
        return bytes(self._data)

    @property
    def text(self) -> str:
        """Everything received, decoded losslessly."""
        return self._data.decode("latin-1")

    def tail(self, window: int = TAIL_WINDOW) -> str:
        """The last ``window`` characters, for anchored prompt detection."""
        if window >= len(self._data):
            return self._data.decode("latin-1")
        return self._data[-window:].decode("latin-1")

    def __len__(self) -> int:
        return len(self._data)

    def __bool__(self) -> bool:
        # Explicit: an empty buffer is falsey, but so is a buffer of NUL bytes
        # under a naive implementation. Length is the only thing meant here.
        return len(self._data) > 0

    def clear(self) -> None:
        self._data.clear()

    def __repr__(self) -> str:
        return f"StreamBuffer({len(self._data)} bytes)"
