"""Whether an image plausibly suits a device, and how sure we are.

Never asserted from a filename. ``c2950-i6q4l2-mz.121-22.EA14.bin`` is *named*
for a 2950 by a convention Cisco follows, but a renamed file is still a renamed
file, and the cost of being wrong here is a device that will not boot at the end
of a two-hour transfer.

So compatibility is assembled from several pieces of evidence, weighted, and
reported with a confidence and the reasons behind it. Where a requirement cannot
be determined at all -- DRAM, most of the time, because it is not derivable from
the image file -- that is stated rather than assumed satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ciscoyoke.identify.facts import DeviceFacts
from ciscoyoke.image.fingerprint import Fingerprint
from ciscoyoke.stream.tracker import Confidence


class Verdict(StrEnum):
    COMPATIBLE = "compatible"
    """Every check that could be made passed."""

    INCOMPATIBLE = "incompatible"
    """Something checkable failed. Do not transfer."""

    UNDETERMINED = "undetermined"
    """Not enough is known. Reported, never assumed either way."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """One check, its outcome, and what it was based on."""

    check: str
    passed: bool | None
    detail: str

    @property
    def determined(self) -> bool:
        return self.passed is not None

    def to_json(self) -> dict[str, object]:
        return {"check": self.check, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Compatibility:
    """The assembled judgement."""

    verdict: Verdict
    confidence: Confidence
    evidence: tuple[Evidence, ...] = field(default=())

    @property
    def blocking(self) -> tuple[Evidence, ...]:
        return tuple(e for e in self.evidence if e.passed is False)

    def render(self) -> str:
        lines = ["Compatibility"]
        for item in self.evidence:
            mark = {True: "ok", False: "FAIL", None: "unknown"}[item.passed]
            lines.append(f"  {mark:<8} {item.check:<22} {item.detail}")
        lines.append(f"  verdict  {self.verdict.value} ({self.confidence.value} confidence)")
        return "\n".join(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "confidence": self.confidence.value,
            "evidence": [item.to_json() for item in self.evidence],
        }


# Filename platform tokens mapped to the device model families they belong to.
_PLATFORM_FAMILIES: dict[str, tuple[str, ...]] = {
    "c1700": ("1720", "1721", "1751", "1760", "CISCO1760", "CISCO1721"),
    "c2600": ("2610", "2611", "2620", "2621", "2650", "2651"),
    "c2800": ("2801", "2811", "2821", "2851"),
    "c2950": ("WS-C2950",),
    "c2960": ("WS-C2960",),
    "c3550": ("WS-C3550",),
    "c3560": ("WS-C3560",),
}


def _platform_matches(platform: str, model: str) -> bool:
    families = _PLATFORM_FAMILIES.get(platform.lower())
    if not families:
        return False
    upper = model.upper()
    return any(upper.startswith(f.upper()) or f.upper() in upper for f in families)


def assess(
    image: Fingerprint,
    facts: DeviceFacts,
    *,
    flash_free_bytes: int | None = None,
) -> Compatibility:
    """Weigh what is known about an image against what is known about a device."""
    evidence: list[Evidence] = []

    model = facts.model.value
    if image.platform is None:
        evidence.append(
            Evidence(
                "filename platform",
                None,
                f"{image.name!r} does not follow the usual naming convention",
            )
        )
    elif model is None:
        evidence.append(
            Evidence(
                "filename platform",
                None,
                f"image names platform {image.platform!r}; device model unknown",
            )
        )
    elif _platform_matches(image.platform, model):
        evidence.append(
            Evidence(
                "filename platform",
                True,
                f"image names {image.platform!r}; device reports {model}",
            )
        )
    else:
        evidence.append(
            Evidence(
                "filename platform",
                False,
                f"image names {image.platform!r}, which does not match {model}",
            )
        )

    # Flash capacity is reported, never judged. "Does this image belong on
    # this device?" and "can it be staged safely?" are different questions,
    # and answering the first with the second made the whole deletion and
    # stranding apparatus unreachable: insufficient space returned
    # INCOMPATIBLE, and the planner short-circuits on that before it ever
    # considers freeing room. Capacity belongs to the planner.
    if flash_free_bytes is None:
        evidence.append(
            Evidence("flash space", None, "free space not established")
        )
    else:
        fits = flash_free_bytes >= image.size
        evidence.append(
            Evidence(
                "flash space",
                None,
                f"{flash_free_bytes / 1048576:.1f} MiB free / "
                f"{image.size_mib:.1f} MiB required"
                + ("" if fits else " -- the planner will decide how to make room"),
            )
        )

    # DRAM is the honest gap. An IOS image's memory requirement is published in
    # release notes, not encoded in the file, so it cannot be checked here and
    # must not be quietly treated as satisfied.
    evidence.append(
        Evidence(
            "DRAM requirement",
            None,
            "not derivable from the image file; check the release notes",
        )
    )

    return Compatibility(
        verdict=_verdict(evidence),
        confidence=_confidence(evidence),
        evidence=tuple(evidence),
    )


def _verdict(evidence: list[Evidence]) -> Verdict:
    if any(item.passed is False for item in evidence):
        return Verdict.INCOMPATIBLE
    if any(item.passed is True for item in evidence):
        return Verdict.COMPATIBLE
    return Verdict.UNDETERMINED


def _confidence(evidence: list[Evidence]) -> Confidence:
    """Confidence follows how much could actually be checked.

    Never HIGH: a filename and a byte count cannot establish that an image is
    genuine and will boot, and reporting high confidence from those inputs would
    be the exact overstatement this module exists to avoid.
    """
    determined = sum(1 for item in evidence if item.determined)
    if any(item.passed is False for item in evidence):
        return Confidence.HIGH  # a definite failure is a confident answer
    if determined >= 2:
        return Confidence.MEDIUM
    return Confidence.LOW
