"""Output must survive the terminal it actually runs in.

Regression test for a real bug: ``ciscoyoke doctor`` crashed with
``UnicodeEncodeError`` on a stock Windows console, because cp1252 cannot encode
``✓``. The existing tests missed it entirely -- pytest captures stdout, so the
encoder is never exercised.

That is worth a test of its own. The project's first-run command failing on the
most common desktop platform is the kind of defect that makes a tool look
broken before anyone reads a line of its code.
"""

from __future__ import annotations

import io

from ciscoyoke.ui import Style, emit, supports_unicode


class LegacyConsole(io.StringIO):
    """A stream that reports cp1252, like a stock Windows terminal."""

    encoding = "cp1252"

    def reconfigure(self, **kwargs: object) -> None:
        # A real legacy console refuses the upgrade; the code must cope rather
        # than assume reconfigure() always works.
        raise ValueError("cannot reconfigure")


class ModernConsole(io.StringIO):
    encoding = "utf-8"

    def reconfigure(self, **kwargs: object) -> None:
        return None


def test_legacy_console_is_detected() -> None:
    assert supports_unicode(LegacyConsole()) is False


def test_utf8_console_is_detected() -> None:
    assert supports_unicode(ModernConsole()) is True


def test_marks_degrade_to_ascii_on_a_legacy_console() -> None:
    style = Style(LegacyConsole())
    assert style.mark("ok") == "[ok]"
    assert style.mark("failed") == "[!!]"
    assert style.dash == "-"


def test_marks_stay_unicode_where_supported() -> None:
    style = Style(ModernConsole())
    assert style.mark("ok") == "✓"
    assert style.dash == "—"


def test_emit_does_not_raise_on_a_legacy_console() -> None:
    """The actual regression: this used to raise UnicodeEncodeError."""
    stream = LegacyConsole()
    emit("  ✓ pyserial — version 3.5", stream=stream)

    written = stream.getvalue()
    assert written.strip()
    # Whatever was written must be encodable, or the real console would have
    # thrown where StringIO tolerates it.
    written.encode("cp1252")


def test_doctor_output_is_encodable_on_a_legacy_console() -> None:
    """End to end: the real command's real output, through a cp1252 encoder."""
    from ciscoyoke import doctor

    diagnosis = doctor.run()
    style = Style(LegacyConsole())

    rendered = "\n".join(
        f"  {style.mark(check.status.value)} {check.name:20} {check.detail}"
        for check in diagnosis.checks
    )
    rendered.encode("cp1252")


def test_unknown_status_falls_back_rather_than_raising() -> None:
    style = Style(ModernConsole())
    assert style.mark("something-unexpected") == "?"
