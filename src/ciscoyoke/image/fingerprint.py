"""Image provenance.

A SHA-256 proves exactly one thing: *these are the bytes the user gave us*. It
does not prove the image is authentic, that it matches what Cisco shipped, that
it suits this platform, or that it will boot. Those are four different claims and
this module makes only the first -- see :mod:`ciscoyoke.image.compat` for the
second kind and the boot proof in :mod:`ciscoyoke.image.plan` for the last.

Keeping them apart is the point. "Verified" is a word that quietly grows to mean
whatever the reader hopes, and on a two-hour transfer to a device with no
rollback image, hope is expensive.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1024 * 1024

#: Cisco image filenames follow a convention: platform, feature set, format,
#: then version. Convention is not fact, which is why everything derived from a
#: filename is reported as inferred.
_FILENAME = re.compile(
    r"^(?P<platform>[a-z0-9]+)-(?P<features>[a-z0-9]+)-(?P<format>[a-z]+)"
    r"[._](?P<version>[\w.-]+?)\.bin$",
    re.IGNORECASE,
)


class ImageUnreadableError(OSError):
    """The supplied image could not be read."""


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """What can be established about a local file, and nothing more."""

    path: Path
    size: int
    sha256: str
    platform: str | None = None
    feature_set: str | None = None
    version: str | None = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size_mib(self) -> float:
        return self.size / (1024 * 1024)

    @property
    def short_hash(self) -> str:
        return f"{self.sha256[:4]}...{self.sha256[-4:]}"

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "size_bytes": self.size,
            "sha256": self.sha256,
            "platform_from_filename": self.platform,
            "feature_set_from_filename": self.feature_set,
            "version_from_filename": self.version,
            "note": (
                "sha256 establishes only that these are the supplied bytes; it "
                "says nothing about authenticity, compatibility or bootability"
            ),
        }


def fingerprint(path: Path) -> Fingerprint:
    """Hash an image and read what its filename claims.

    Streamed rather than read whole: these files are several megabytes and there
    is no reason to hold one in memory on a machine that may also be driving a
    transfer.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImageUnreadableError(f"{path}: {exc}") from exc

    if size == 0:
        raise ImageUnreadableError(f"{path}: file is empty")

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
    except OSError as exc:
        raise ImageUnreadableError(f"{path}: {exc}") from exc

    match = _FILENAME.match(path.name)
    return Fingerprint(
        path=path,
        size=size,
        sha256=digest.hexdigest(),
        platform=match.group("platform").lower() if match else None,
        feature_set=match.group("features").lower() if match else None,
        version=match.group("version") if match else None,
    )
