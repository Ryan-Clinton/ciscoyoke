"""What is known about each Catalyst family, and how well it is known.

Recovery on a fixed-configuration Catalyst is the same *shape* everywhere --
hold Mode, reach ``switch:``, move the configuration aside, boot -- and
different in the details that decide whether it works: which LED to watch before
releasing the button, whether ``load_helper`` exists, how long a boot takes,
what kind of console port the front panel has.

Those details used to be written into the playbooks as the 2950's, because the
2950 was the only switch that had been connected. A 2960 would then have been
told to release Mode "when the STAT LED goes out", which is not what a 2960
does, and the bootloader would have been missed. So they live here, one entry
per family, each saying where its knowledge came from:

* ``HARDWARE``: run end to end against a real device; a committed ``hw-*``
  fixture backs it.
* ``DOCUMENTATION``: taken from Cisco's published procedure; never executed.
* ``GENERIC``: a Catalyst this table does not know. Best effort, with longer
  waits and an instruction that points at Cisco's guide rather than inventing
  one.

The distinction is shown to the operator before anything runs, and destructive
work on a ``GENERIC`` profile needs an explicit acknowledgement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

PASSWORD_RECOVERY_GUIDE = (
    "https://www.cisco.com/c/en/us/support/docs/switches/"
    "catalyst-2950-series-switches/12040-pswdrec-2900xl.html"
)
_2960_GUIDE = (
    "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst2960/software/"
    "release/15-0_2_se/configuration/guide/scg2960/swtrbl.html"
)
_2960S_BOOTLOADER = (
    "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst2960/software/"
    "release/15-2_2_e/command/reference/cr_2960/bootldr.html"
)

_USB_CONSOLE = (
    "The front panel also has a USB mini-B console port. On Windows it needs "
    "Cisco's USB console driver (it appears as USB VID 05A6 PID 0009); macOS and "
    "Linux need none. When both are connected the USB port takes over and the "
    "RJ-45 port goes quiet."
)


class Evidence(StrEnum):
    """Where a profile's knowledge came from. Ordered strongest first."""

    HARDWARE = "hardware"
    DOCUMENTATION = "documentation"
    GENERIC = "generic"

    @property
    def label(self) -> str:
        return {
            Evidence.HARDWARE: "verified on hardware",
            Evidence.DOCUMENTATION: "from Cisco documentation, not yet run on hardware",
            Evidence.GENERIC: "unknown family: best effort, unverified",
        }[self]


@dataclass(frozen=True, slots=True)
class CatalystProfile:
    """One Catalyst family's recovery details, and their provenance."""

    family: str
    prefixes: tuple[str, ...]
    mode_release: str
    """When to let go of Mode, in the words of this family's procedure."""

    load_helper: bool
    """Whether to send ``load_helper``. False where Cisco's bootloader
    reference lists no such command; a switch without it answers with an error
    and the prompt, so sending it is harmless but noisy."""

    evidence: Evidence
    source: str
    """The fixture or document the profile rests on."""

    human_timeout: float = 180.0
    boot_timeout: float = 300.0
    console: str = "RJ-45 console port, 9600 8N1."

    @property
    def verified(self) -> bool:
        return self.evidence is Evidence.HARDWARE

    def describe(self) -> str:
        return f"Catalyst {self.family} profile ({self.evidence.label})"

    def to_json(self) -> dict[str, object]:
        return {
            "family": self.family,
            "evidence": self.evidence.value,
            "source": self.source,
            "load_helper": self.load_helper,
            "human_timeout": self.human_timeout,
            "boot_timeout": self.boot_timeout,
        }


CATALYST_2950 = CatalystProfile(
    family="2950",
    prefixes=("WS-C2950",),
    mode_release="release it when the STAT LED goes out (about 5 seconds)",
    load_helper=True,
    evidence=Evidence.HARDWARE,
    source="tests/fixtures/hw-switch-2950-recover-no-restore.ytx.pub",
)

CATALYST_2960 = CatalystProfile(
    family="2960",
    prefixes=("WS-C2960-", "WS-C2960G"),
    mode_release=(
        "keep holding until the SYST LED turns briefly amber and then solid "
        "green, then release it"
    ),
    load_helper=True,
    evidence=Evidence.DOCUMENTATION,
    source=_2960_GUIDE,
    # IOS 15 on this hardware takes noticeably longer to come up than 12.1 on
    # a 2950; the ceiling is generous because timing out mid-boot is the one
    # failure that leaves nothing useful behind.
    boot_timeout=420.0,
)

CATALYST_2960_STACKABLE = CatalystProfile(
    family="2960-S/X/Plus",
    prefixes=("WS-C2960S", "WS-C2960X", "WS-C2960XR", "WS-C2960+", "WS-C2960C"),
    mode_release=(
        "keep holding until the SYST LED turns briefly amber and then solid "
        "green, then release it"
    ),
    load_helper=False,
    evidence=Evidence.DOCUMENTATION,
    source=_2960S_BOOTLOADER,
    boot_timeout=480.0,
    console="RJ-45 console port, 9600 8N1. " + _USB_CONSOLE,
)

GENERIC_CATALYST = CatalystProfile(
    family="(unrecognised)",
    prefixes=(),
    mode_release=(
        "release it when the system LED changes (on most models it turns amber "
        f"and then green; check Cisco's guide for yours: {PASSWORD_RECOVERY_GUIDE})"
    ),
    load_helper=True,
    evidence=Evidence.GENERIC,
    source=PASSWORD_RECOVERY_GUIDE,
    human_timeout=300.0,
    boot_timeout=600.0,
)

PROFILES: tuple[CatalystProfile, ...] = (
    CATALYST_2950,
    CATALYST_2960_STACKABLE,
    CATALYST_2960,
)


def unverified_refusal(profile: CatalystProfile, command: str) -> str | None:
    """Why a destructive run on this profile needs an explicit acknowledgement.

    Only a ``GENERIC`` profile is gated. A ``DOCUMENTATION`` one follows
    Cisco's published procedure for that exact family, and is labelled as
    unrun rather than blocked -- blocking it would stop the very first run that
    could turn it into a ``HARDWARE`` one.
    """
    if profile.evidence is not Evidence.GENERIC:
        return None
    return (
        "This switch family is not one ciscoyoke knows, so the procedure is "
        "Cisco's generic one with longer waits, and nothing about it has been "
        f"checked on this hardware. Re-run with --accept-unverified to go "
        f"ahead ({command}), and if it fails, `ciscoyoke report` collects what "
        "is needed to add support."
    )


def catalyst_profile(model: str | None) -> CatalystProfile:
    """The profile for a Catalyst model, by longest matching prefix.

    ``WS-C2960X-24TS-L`` must reach the stackable profile rather than the plain
    2960 one, which is why the longest prefix wins rather than the first.
    """
    if model:
        upper = model.upper()
        matches = [
            (len(prefix), profile)
            for profile in PROFILES
            for prefix in profile.prefixes
            if upper.startswith(prefix)
        ]
        if matches:
            return max(matches, key=lambda match: match[0])[1]
    return GENERIC_CATALYST
