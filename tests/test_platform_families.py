"""Platform families, and why a missing one is worse than no opinion.

An unknown family is not treated as "no information" -- ``_platform_matches``
returns False, which the compatibility check reads as a positive mismatch and
the planner turns into a refusal. So a platform absent from the table has its
*own* image rejected as belonging to something else.

That was live for the 1800 series until real hardware turned up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciscoyoke.identify.facts import from_show_version
from ciscoyoke.image.compat import Verdict, assess
from ciscoyoke.image.fingerprint import fingerprint

# `show version` shapes, in the form each platform actually prints.
SHOW_VERSION = {
    "1812": (
        "Cisco IOS Software, C181X Software (C181X-ADVIPSERVICESK9-M), "
        "Version 12.4(15)T14\n"
        "cisco 1812 (MPC8500) processor (revision 0x300) with "
        "236544K/25600K bytes of memory.\n"
        "Processor board ID FHK1123F0AB\n"
    ),
    "2950": (
        "Cisco IOS Software, C2950 Software (C2950-I6Q4L2-M), "
        "Version 12.1(22)EA14\n"
        "cisco WS-C2950-24 (RC32300) processor\n"
        "Model number             : WS-C2950-24\n"
    ),
}


def image(tmp_path: Path, name: str) -> object:
    path = tmp_path / name
    path.write_bytes(b"\x7fELF" + b"\x00" * 4096)
    return fingerprint(path)


@pytest.mark.parametrize(
    ("model_key", "image_name"),
    [
        ("1812", "c181x-advipservicesk9-mz.124-15.T14.bin"),
        ("2950", "c2950-i6q4l2-mz.121-22.EA14.bin"),
    ],
)
def test_a_platform_accepts_its_own_image(
    tmp_path: Path, model_key: str, image_name: str
) -> None:
    """The image a device actually ships with must not read as foreign."""
    facts = from_show_version(SHOW_VERSION[model_key])
    result = assess(image(tmp_path, image_name), facts)  # type: ignore[arg-type]

    assert result.verdict is not Verdict.INCOMPATIBLE
    platform = next(e for e in result.evidence if e.check == "filename platform")
    assert platform.passed is True, platform.detail


def test_an_image_from_another_platform_is_still_refused(tmp_path: Path) -> None:
    """Widening the table must not soften the check it exists for."""
    facts = from_show_version(SHOW_VERSION["1812"])
    result = assess(
        image(tmp_path, "c2950-i6q4l2-mz.121-22.EA14.bin"), facts  # type: ignore[arg-type]
    )

    assert result.verdict is Verdict.INCOMPATIBLE


def test_an_unknown_family_is_reported_as_a_mismatch_not_silence() -> None:
    """Documents the sharp edge, so the next gap is noticed rather than hit.

    A platform token absent from the table produces a *failed* check, not an
    undetermined one -- so adding hardware means adding its family, or its own
    image will be rejected.
    """
    from ciscoyoke.image.compat import _platform_matches

    assert _platform_matches("c181x", "CISCO1812") is True
    assert _platform_matches("c9999", "CISCO9999") is False
