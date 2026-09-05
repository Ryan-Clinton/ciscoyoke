"""Terminal output that survives the terminal it is actually running in.

A Windows console defaults to a legacy code page -- cp1252 on a UK/US install --
which cannot encode ``✓``. Printing one raises ``UnicodeEncodeError`` and kills
the command. It is a small thing that makes a tool look broken on first run, and
this project's first-run command is ``ciscoyoke doctor`` on exactly that
platform.

So: ask the stream to speak UTF-8 if it can, and degrade to ASCII if it cannot.
Never assume, and never crash on a decoration.
"""

from __future__ import annotations

import contextlib
import sys
from typing import TextIO

_UNICODE_MARKS = {"ok": "✓", "failed": "✗", "unknown": "?"}
_ASCII_MARKS = {"ok": "[ok]", "failed": "[!!]", "unknown": "[??]"}

DASH = "—"
ASCII_DASH = "-"

# Every decoration this project might emit, and what it degrades to. Anything
# not listed here is still handled -- render() falls back to encode(errors=
# "replace") -- but a named substitution stays readable where a "?" would not.
_TRANSLITERATIONS = {
    "✓": "[ok]",
    "✗": "[!!]",
    "●": "*",
    "◐": "o",
    "○": ".",
    "→": "->",
    "──": "--",
    DASH: ASCII_DASH,
}


def _try_utf8(stream: TextIO) -> None:
    """Ask a stream to re-encode as UTF-8.

    Available on Python 3.7+ for real files; a captured or wrapped stream may
    not offer it, which is not an error.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    with contextlib.suppress(OSError, ValueError):  # platform dependent
        reconfigure(encoding="utf-8")


def supports_unicode(stream: TextIO | None = None) -> bool:
    """Whether ``stream`` can actually encode the decorations we would use."""
    target = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None)
    if not encoding:
        # No declared encoding (a StringIO under test, say). Unicode is safe
        # because nothing will be encoded to bytes on the way out.
        return True
    try:
        "".join(_UNICODE_MARKS.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Style:
    """Decoration set chosen once, from what the stream can actually render."""

    def __init__(self, stream: TextIO | None = None) -> None:
        target = stream if stream is not None else sys.stdout
        _try_utf8(target)
        self.unicode = supports_unicode(target)
        self.encoding = getattr(target, "encoding", None) or "ascii"

    def mark(self, status: str) -> str:
        table = _UNICODE_MARKS if self.unicode else _ASCII_MARKS
        return table.get(status, table["unknown"])

    @property
    def dash(self) -> str:
        return DASH if self.unicode else ASCII_DASH

    def render(self, text: str) -> str:
        """Make ``text`` safe for this stream.

        Substituting only the characters this module happens to emit is not
        enough: callers assemble their own strings, and a decoration that
        arrives from anywhere else would still reach the encoder and raise.
        So known decorations get a readable ASCII equivalent, and *anything*
        else the target encoding cannot represent is replaced rather than
        allowed to become an exception mid-command.
        """
        if self.unicode:
            return text

        for unicode_form, ascii_form in _TRANSLITERATIONS.items():
            text = text.replace(unicode_form, ascii_form)

        return text.encode(self.encoding, "replace").decode(self.encoding)


def emit(text: str, style: Style | None = None, stream: TextIO | None = None) -> None:
    """Print ``text``, degrading gracefully instead of raising on encoding."""
    target = stream if stream is not None else sys.stdout
    active = style if style is not None else Style(target)
    rendered = active.render(text)
    try:
        print(rendered, file=target)
    except UnicodeEncodeError:  # pragma: no cover - last-resort belt and braces
        encoding = getattr(target, "encoding", "ascii") or "ascii"
        print(rendered.encode(encoding, "replace").decode(encoding), file=target)
