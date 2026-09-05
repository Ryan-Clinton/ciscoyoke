"""Flash-space planning, and the rule that stops a device being stranded.

The dangerous part of image rescue is not the transfer. It is the arithmetic
before it.

    flash total    8 MB
    existing IOS   5 MB
    new IOS        5.5 MB
    free           2 MB

Deleting the existing image makes room. It also means that if the transfer then
fails -- a nudged cable, a laptop asleep, two hours in -- the device has **no
bootable image at all** and is in a worse state than when the operator started.
They came to this tool because their device would not boot; sending them away
with one that cannot even reach ROMMON's helper is the single worst outcome
available.

Hence the policy, enforced as an invariant rather than a warning:

    **The only known-bootable image is never deleted to make room for a
    replacement without a separate, explicit risk acknowledgement.**

Deleting a *spare* image is fine. Deleting the last one is a decision somebody
has to make on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from ciscoyoke.image.compat import Compatibility, Verdict
from ciscoyoke.image.fingerprint import Fingerprint
from ciscoyoke.journal import model
from ciscoyoke.journal.model import Mutation

#: Guard authorising deletion of the last bootable image.
ACCEPT_STRANDED_RISK = "accept_stranded_risk"

#: Guard authorising deletion of a spare image to make room.
ACCEPT_IMAGE_DELETION = "accept_image_deletion"

# `dir flash:` output: index, permissions, size, date, name.
_DIR_ENTRY = re.compile(
    r"^\s*\d+\s+\S+\s+(?P<size>\d+)\s+.*?\s(?P<name>\S+\.bin)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_FREE_SPACE = re.compile(r"(\d+)\s+bytes (?:available|free)", re.IGNORECASE)
_TOTAL_SPACE = re.compile(r"(\d+)\s+bytes total", re.IGNORECASE)


class Strategy(StrEnum):
    """How the image gets onto the device."""

    ALREADY_PRESENT = "already_present"
    """The exact image is already in flash. Boot it; transfer nothing."""

    BOOT_EXISTING = "boot_existing"
    """A different but valid image is present. Offer it before transferring."""

    TRANSFER_ALONGSIDE = "transfer_alongside"
    """Enough free space. Nothing needs deleting."""

    TRANSFER_AFTER_DELETING_SPARE = "transfer_after_deleting_spare"
    """Room can be made without touching the last bootable image."""

    BLOCKED_WOULD_STRAND = "blocked_would_strand"
    """Room can only be made by deleting the only bootable image."""

    BLOCKED_INCOMPATIBLE = "blocked_incompatible"
    """A compatibility check failed outright."""

    BLOCKED_NO_ROOM = "blocked_no_room"
    """Even deleting everything would not fit the image."""

    BLOCKED_TRANSFER_ERASES = "blocked_transfer_erases"
    """The transfer procedure itself destroys the existing images, so the
    stranding rule applies even though nothing would be explicitly deleted."""


@dataclass(frozen=True, slots=True)
class FlashFile:
    name: str
    size: int

    @property
    def size_mib(self) -> float:
        return self.size / (1024 * 1024)


@dataclass(frozen=True, slots=True)
class FlashInventory:
    """What is on the device's flash, as far as it disclosed."""

    files: tuple[FlashFile, ...] = ()
    free_bytes: int | None = None
    total_bytes: int | None = None

    @property
    def images(self) -> tuple[FlashFile, ...]:
        """Files that *look* like images.

        A ``.bin`` in flash is a candidate, not a known-good image: the name
        says nothing about whether it is complete, uncorrupted, compatible, or
        has ever booted. On the hardware this tool exists for, all four are
        live possibilities. Callers that need certainty have to earn it -- see
        the boot proof.
        """
        return tuple(f for f in self.files if f.name.lower().endswith(".bin"))

    @property
    def candidate_images(self) -> tuple[FlashFile, ...]:
        """Clearer name for the same thing. Prefer this at call sites."""
        return self.images

    def find(self, name: str) -> FlashFile | None:
        target = name.lower().rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        for candidate in self.images:
            if candidate.name.lower().rsplit("/", 1)[-1].rsplit(":", 1)[-1] == target:
                return candidate
        return None


def parse_flash(output: str) -> FlashInventory:
    """Read a ``dir flash:`` listing.

    Tolerant by design: an unparseable listing yields an inventory with unknown
    free space rather than an exception, because "we could not read the flash"
    is a legitimate state that the planner must handle -- it is exactly what a
    half-broken device produces.
    """
    files = tuple(
        FlashFile(name=match.group("name"), size=int(match.group("size")))
        for match in _DIR_ENTRY.finditer(output)
    )
    free = _FREE_SPACE.search(output)
    total = _TOTAL_SPACE.search(output)
    return FlashInventory(
        files=files,
        free_bytes=int(free.group(1)) if free else None,
        total_bytes=int(total.group(1)) if total else None,
    )


@dataclass(frozen=True, slots=True)
class TransferPlan:
    """What image rescue would do, decided before anything is sent."""

    strategy: Strategy
    image: Fingerprint
    inventory: FlashInventory
    compatibility: Compatibility
    delete: tuple[FlashFile, ...] = ()
    boot_target: str | None = None
    reason: str = ""
    candidate_images: tuple[FlashFile, ...] = field(default=())

    @property
    def blocked(self) -> bool:
        return self.strategy.value.startswith("blocked")

    @property
    def requires_transfer(self) -> bool:
        return self.strategy in (
            Strategy.TRANSFER_ALONGSIDE,
            Strategy.TRANSFER_AFTER_DELETING_SPARE,
        )

    def mutations(self) -> tuple[Mutation, ...]:
        """The deletions this plan would perform, as journalled mutations."""
        return tuple(
            model.delete_flash_file(
                file.name,
                guard=ACCEPT_IMAGE_DELETION,
                only_bootable=len(self.candidate_images) <= 1,
            )
            for file in self.delete
        )

    def render(self) -> str:
        free = self.inventory.free_bytes
        free_text = f"{free / 1048576:.1f} MiB" if free is not None else "unknown"

        lines = [
            "IMAGE RESCUE PLAN",
            "",
            f"  Image            {self.image.name}",
            f"  SHA-256          {self.image.short_hash}",
            f"  Size             {self.image.size_mib:.1f} MiB",
            f"  Flash free       {free_text}",
            f"  Images present   {len(self.inventory.images)}",
            f"  Strategy         {self.strategy.value}",
        ]
        if self.delete:
            lines.append(
                "  Would delete     "
                + ", ".join(f"{f.name} ({f.size_mib:.1f} MiB)" for f in self.delete)
            )
        if self.boot_target:
            lines.append(f"  Boot             {self.boot_target}")
        if self.reason:
            lines += ["", self.reason]
        return "\n".join(lines)


def make_plan(
    image: Fingerprint,
    inventory: FlashInventory,
    compatibility: Compatibility,
    *,
    transfer_erases_partition: bool = False,
) -> TransferPlan:
    """Decide how -- or whether -- to get this image onto the device.

    Ordered so the cheapest safe answer wins: an image already present beats a
    two-hour transfer, and an existing bootable image beats deleting anything.
    """
    bootable = inventory.candidate_images

    if compatibility.verdict is Verdict.INCOMPATIBLE:
        failures = "; ".join(e.detail for e in compatibility.blocking)
        return TransferPlan(
            strategy=Strategy.BLOCKED_INCOMPATIBLE,
            image=image,
            inventory=inventory,
            compatibility=compatibility,
            candidate_images=bootable,
            reason=f"Compatibility check failed: {failures}",
        )

    # Some transfer procedures destroy the partition before receiving --
    # ROMMON XMODEM warns that all existing data in bootflash will be lost.
    # The stranding rule has to cover that too, or a plan that deletes nothing
    # itself still ends with no rollback image.
    if transfer_erases_partition and bootable:
        return TransferPlan(
            strategy=Strategy.BLOCKED_TRANSFER_ERASES,
            image=image,
            inventory=inventory,
            compatibility=compatibility,
            candidate_images=bootable,
            reason=(
                "BLOCKED\n\n"
                "This platform's transfer procedure erases the target flash "
                "before receiving the replacement, so the "
                f"{len(bootable)} image(s) already present will not survive it "
                "-- even though nothing would be explicitly deleted.\n\n"
                "A failed transfer would leave no bootable image at all.\n\n"
                "Options:\n"
                "  1. Abort                                        (default)\n"
                "  2. Boot and retain an existing image instead\n"
                "  3. Accept stranded-device risk   --accept-stranded-risk"
            ),
        )

    existing = inventory.find(image.name)
    if existing is not None and existing.size == image.size:
        return TransferPlan(
            strategy=Strategy.ALREADY_PRESENT,
            image=image,
            inventory=inventory,
            compatibility=compatibility,
            boot_target=existing.name,
            candidate_images=bootable,
            reason=(
                "An image of this name and size is already in flash. Booting it "
                "avoids a transfer entirely."
            ),
        )

    if bootable and inventory.free_bytes is not None and inventory.free_bytes < image.size:
        # Cisco's own guidance is to look for a valid image before transferring.
        # Offering that first is both faster and safer than making room.
        pass

    free = inventory.free_bytes
    if free is None:
        return TransferPlan(
            strategy=Strategy.TRANSFER_ALONGSIDE,
            image=image,
            inventory=inventory,
            compatibility=compatibility,
            candidate_images=bootable,
            reason=(
                "Free space could not be determined. The transfer may fail for "
                "lack of room; nothing will be deleted to prevent that."
            ),
        )

    if free >= image.size:
        return TransferPlan(
            strategy=Strategy.TRANSFER_ALONGSIDE,
            image=image,
            inventory=inventory,
            compatibility=compatibility,
            candidate_images=bootable,
            reason="Sufficient free space; no deletion required.",
        )

    spares = tuple(f for f in bootable if f.name != (existing.name if existing else ""))
    if len(bootable) <= 1:
        return TransferPlan(
            strategy=Strategy.BLOCKED_WOULD_STRAND,
            image=image,
            inventory=inventory,
            compatibility=compatibility,
            delete=bootable,
            candidate_images=bootable,
            reason=_stranded_message(image, bootable, free),
        )

    # More than one image: free the smallest spares until it fits, oldest-named
    # last so the choice is stable and explicable.
    freed = free
    to_delete: list[FlashFile] = []
    for candidate in sorted(spares, key=lambda f: f.size):
        if freed >= image.size:
            break
        to_delete.append(candidate)
        freed += candidate.size

    if freed < image.size:
        return TransferPlan(
            strategy=Strategy.BLOCKED_NO_ROOM,
            image=image,
            inventory=inventory,
            compatibility=compatibility,
            candidate_images=bootable,
            reason=(
                f"Even after deleting every spare image, only "
                f"{freed / 1048576:.1f} MiB would be free for a "
                f"{image.size_mib:.1f} MiB image."
            ),
        )

    return TransferPlan(
        strategy=Strategy.TRANSFER_AFTER_DELETING_SPARE,
        image=image,
        inventory=inventory,
        compatibility=compatibility,
        delete=tuple(to_delete),
        candidate_images=bootable,
        reason=(
            "Room can be made without touching the last bootable image, so a "
            "failed transfer still leaves the device able to boot."
        ),
    )


def _stranded_message(
    image: Fingerprint, bootable: tuple[FlashFile, ...], free: int
) -> str:
    existing = bootable[0] if bootable else None
    lines = [
        "BLOCKED",
        "",
        "The supplied image cannot be staged alongside the only known bootable",
        "image.",
        "",
    ]
    if existing is not None:
        lines.append(
            f"  Existing bootable image:  {existing.name} "
            f"({existing.size_mib:.1f} MiB)"
        )
    lines += [
        f"  Supplied image:           {image.name} ({image.size_mib:.1f} MiB)",
        f"  Free now:                 {free / 1048576:.1f} MiB",
        "  Risk:                     no rollback image during transfer",
        "",
        "Options:",
        "  1. Abort                                        (default)",
        "  2. Boot and retain the existing image instead",
        "  3. Accept stranded-device risk and replace it   --accept-stranded-risk",
    ]
    return "\n".join(lines)


def _overridable(strategy: Strategy) -> bool:
    return strategy in (
        Strategy.BLOCKED_WOULD_STRAND,
        Strategy.BLOCKED_TRANSFER_ERASES,
    )


def override_stranded(plan: TransferPlan) -> TransferPlan:
    """Convert a stranding block into an acknowledged deletion.

    Only reachable when the operator has explicitly accepted the risk. The
    resulting mutation carries ``only_bootable=True`` and the
    ``accept_stranded_risk`` guard, so the journal records that the device was
    deliberately left without a rollback image rather than accidentally.
    """
    if not _overridable(plan.strategy):
        return plan
    return TransferPlan(
        strategy=Strategy.TRANSFER_AFTER_DELETING_SPARE,
        image=plan.image,
        inventory=plan.inventory,
        compatibility=plan.compatibility,
        delete=plan.delete,
        candidate_images=plan.candidate_images,
        reason=(
            "Stranded-device risk accepted explicitly: the only bootable image "
            "will be deleted, and a failed transfer will leave no image."
        ),
    )
