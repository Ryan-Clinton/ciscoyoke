"""Extracting device facts from console text, with the basis for each.

Pure functions over strings, so every parser is testable against real captured
output without a device.

Two sources, and the distinction matters. ``show version`` requires a working
login and gives a great deal; the **boot banner** requires nothing at all and is
emitted before any authentication, which is the only identity available on a
locked device. A tool that can only identify a device it can log into is useless
for exactly the devices this project exists to rescue.

Every fact carries how it was obtained, so a caller can tell "the device told us
its model" from "the filename of the boot image implies a model".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ciscoyoke.result.schema import Finding
from ciscoyoke.stream.tracker import Basis, Confidence

# `show version` forms, spanning IOS 12.x through 15.x.
_MODEL_SHOW_VERSION = (
    re.compile(r"^cisco\s+(\S+)\s+\(.*?\)\s+processor", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Model number\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^cisco\s+(WS-\S+)", re.IGNORECASE | re.MULTILINE),
)

_IOS_VERSION = re.compile(
    r"(?:IOS|Internetwork Operating System).*?Version\s+([\w.()]+)",
    re.IGNORECASE | re.DOTALL,
)

_SERIAL = (
    re.compile(r"System serial number\s*:\s*(\S+)", re.IGNORECASE),
    re.compile(r"Processor board ID\s+(\S+)", re.IGNORECASE),
)

_CONFIG_REGISTER = re.compile(r"Configuration register is\s+(0x[0-9A-Fa-f]+)")

_DRAM = re.compile(r"with\s+(\d+)K(?:/(\d+)K)?\s+bytes of memory", re.IGNORECASE)

_FLASH = re.compile(
    r"(\d+)K bytes of (?:processor board System )?flash", re.IGNORECASE
)

# Boot-banner forms, available before any login.
_BOOTSTRAP_MODEL = re.compile(
    r"System Bootstrap, Version [^\n]*?\n?[^\n]*?(C\d{4}|WS-C\d{4}\S*)",
    re.IGNORECASE,
)
_BOOT_IMAGE = re.compile(r"(?:Loading|booting)\s+\"?(\S+\.bin)", re.IGNORECASE)

# A boot image filename encodes the platform by convention, e.g.
# c2950-i6q4l2-mz.121-22.EA14.bin. Convention is not fact, so this is only ever
# reported as inferred.
_IMAGE_PLATFORM = re.compile(r"^(c\d{3,4}|cat\d{4})[-_]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DeviceFacts:
    """What is known about a device, and how well."""

    model: Finding
    ios_version: Finding
    serial_number: Finding
    config_register: Finding
    dram_kb: Finding
    flash_kb: Finding

    def to_json(self) -> dict[str, object]:
        return {
            "model": self.model.to_json(),
            "ios_version": self.ios_version.to_json(),
            "serial_number": self.serial_number.to_json(),
            "config_register": self.config_register.to_json(),
            "dram_kb": self.dram_kb.to_json(),
            "flash_kb": self.flash_kb.to_json(),
        }

    @property
    def identified(self) -> bool:
        return self.model.value is not None


_NO_CREDENTIALS = "requires boot evidence or authenticated `show version`"


def unknown_facts(reason: str = _NO_CREDENTIALS) -> DeviceFacts:
    """Everything unknown, each field carrying why.

    Used for a locked device. An absent field tells the reader nothing; a field
    that says it is unknown and why is actionable.
    """
    return DeviceFacts(
        model=Finding.unknown(reason),
        ios_version=Finding.unknown(reason),
        serial_number=Finding.unknown(reason),
        config_register=Finding.unknown(reason),
        dram_kb=Finding.unknown(reason),
        flash_kb=Finding.unknown(reason),
    )


def _first(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _observed(value: str | None, reason: str) -> Finding:
    if value is None:
        return Finding.unknown(reason)
    return Finding(value=value, basis=Basis.OBSERVED, confidence=Confidence.HIGH)


def from_show_version(text: str) -> DeviceFacts:
    """Parse authenticated ``show version`` output.

    Everything here is observed rather than inferred: the device is stating
    these facts about itself.
    """
    absent = "not present in show version output"

    dram = _DRAM.search(text)
    dram_value = None
    if dram:
        # `with 60416K/5120K bytes of memory` -- the two figures are main and
        # I/O memory. Their sum is the installed DRAM, which is what a platform
        # requirement is quoted against.
        total = int(dram.group(1)) + (int(dram.group(2)) if dram.group(2) else 0)
        dram_value = str(total)

    flash = _FLASH.search(text)

    return DeviceFacts(
        model=_observed(_first(_MODEL_SHOW_VERSION, text), absent),
        ios_version=_observed(
            match.group(1) if (match := _IOS_VERSION.search(text)) else None, absent
        ),
        serial_number=_observed(_first(_SERIAL, text), absent),
        config_register=_observed(
            match.group(1) if (match := _CONFIG_REGISTER.search(text)) else None, absent
        ),
        dram_kb=_observed(dram_value, absent),
        flash_kb=_observed(flash.group(1) if flash else None, absent),
    )


def from_boot_banner(text: str) -> DeviceFacts:
    """Parse whatever the device announced while booting.

    This is the only identity available on a device nobody can log into, so it
    is worth extracting carefully -- and worth labelling honestly, because a
    platform read from an image filename is a convention, not a statement.
    """
    facts = unknown_facts("not stated in the boot output observed so far")

    bootstrap = _BOOTSTRAP_MODEL.search(text)
    if bootstrap:
        return DeviceFacts(
            model=Finding(
                value=bootstrap.group(1).upper(),
                basis=Basis.OBSERVED,
                confidence=Confidence.MEDIUM,
            ),
            ios_version=facts.ios_version,
            serial_number=facts.serial_number,
            config_register=facts.config_register,
            dram_kb=facts.dram_kb,
            flash_kb=facts.flash_kb,
        )

    image = _BOOT_IMAGE.search(text)
    if image:
        platform = _IMAGE_PLATFORM.match(image.group(1))
        if platform:
            return DeviceFacts(
                model=Finding(
                    value=platform.group(1).lower(),
                    basis=Basis.INFERRED,
                    confidence=Confidence.LOW,
                    reason=(
                        f"inferred from boot image filename {image.group(1)!r}; "
                        f"filenames follow convention but are not authoritative"
                    ),
                ),
                ios_version=facts.ios_version,
                serial_number=facts.serial_number,
                config_register=facts.config_register,
                dram_kb=facts.dram_kb,
                flash_kb=facts.flash_kb,
            )

    return facts
