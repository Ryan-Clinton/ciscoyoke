"""Platform-specific image recovery, kept apart because the procedures differ.

A Catalyst bootloader and a router ROMMON are not two dialects of one thing.
They are different programs with different commands, different flash listings
and different ways of changing the console speed:

    Catalyst ``switch:``          Router ``rommon>``
    ---------------------         ------------------
    set BAUD 115200               xmodem -s115200 (rate is a transfer flag)
    copy xmodem: flash:<file>     xmodem -c <file>
    dir flash:                    dir flash:
      index perms size date name    size checksum name
    boot flash:<file>             boot flash:<file>

Sending Catalyst commands to a ROMMON prompt produces a device that ignores
them, and parsing a ROMMON flash listing with a Catalyst-shaped parser produces
an empty inventory -- which the planner would read as "no images present" and
act on. That combination is how a rescue turns into a wipe.

So each platform gets a driver, and a device that matches none of them is
refused rather than handed a procedure that was written for something else.
This mirrors the separation already applied to access recovery, where the same
reasoning holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ciscoyoke.image.plan import FlashFile, FlashInventory
from ciscoyoke.stream.tracker import State


class Family(StrEnum):
    CATALYST_BOOTLOADER = "catalyst_bootloader"
    ROUTER_ROMMON = "router_rommon"
    BOOTED_IOS = "booted_ios"
    UNSUPPORTED = "unsupported"


class NoDriverError(RuntimeError):
    """No verified image-recovery procedure exists for this device and state."""


# Catalyst `dir flash:` rows: index, permissions, size, date, filename.
_CATALYST_ROW = re.compile(
    r"^\s*\d+\s+\S+\s+(?P<size>\d+)\s+.*?\s(?P<name>\S+\.bin)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# ROMMON `dir flash:` rows: size (with a hex form in brackets), checksum, name.
# Cisco's own example is:
#     5358032 bytes (0x51c1d0)   0x7b16    c2600-i-mz.122-10b.bin
_ROMMON_ROW = re.compile(
    r"^\s*(?P<size>\d+)\s+bytes\s+\([^)]*\)\s+0x[0-9a-f]+\s+(?P<name>\S+\.bin)",
    re.MULTILINE | re.IGNORECASE,
)

_FREE_SPACE = re.compile(r"(\d+)\s+bytes (?:available|free)", re.IGNORECASE)
_TOTAL_SPACE = re.compile(r"(\d+)\s+bytes total", re.IGNORECASE)


class ImageRecoveryDriver(Protocol):
    """What a platform must be able to do before an image can be rescued on it."""

    @property
    def family(self) -> Family: ...

    @property
    def can_change_baud(self) -> bool: ...

    def inventory_command(self) -> tuple[bytes, ...]: ...

    def parse_inventory(self, output: str) -> FlashInventory: ...

    def set_baud_command(self, baud: int) -> bytes | None: ...

    def restore_baud_command(self) -> bytes | None: ...

    def transfer_command(self, filename: str, baud: int) -> bytes: ...

    def boot_command(self, filename: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class CatalystBootloaderDriver:
    """Fixed-configuration Catalyst at the ``switch:`` prompt."""

    @property
    def family(self) -> Family:
        return Family.CATALYST_BOOTLOADER

    @property
    def can_change_baud(self) -> bool:
        return True

    def inventory_command(self) -> tuple[bytes, ...]:
        # Flash must be mounted before it can be listed, and load_helper is
        # required on the older platforms this targets.
        return (b"flash_init\r", b"load_helper\r", b"dir flash:\r")

    def parse_inventory(self, output: str) -> FlashInventory:
        return _parse(output, _CATALYST_ROW)

    def set_baud_command(self, baud: int) -> bytes | None:
        return f"set BAUD {baud}\r".encode("ascii")

    def restore_baud_command(self) -> bytes | None:
        # Returns the platform's own default rather than a rate this code
        # guessed at.
        return b"unset BAUD\r"

    def transfer_command(self, filename: str, baud: int) -> bytes:
        del baud  # speed is set separately on this platform
        return f"copy xmodem: flash:{filename}\r".encode("ascii")

    def boot_command(self, filename: str) -> bytes:
        return f"boot flash:{filename}\r".encode("ascii")


@dataclass(frozen=True, slots=True)
class RouterRommonDriver:
    """IOS router at the ``rommon>`` prompt.

    The console rate is a *flag on the transfer* here rather than a separate
    mode change, which is why ``set_baud_command`` returns nothing: there is no
    standalone command to send, and pretending otherwise was the original bug.
    """

    @property
    def family(self) -> Family:
        return Family.ROUTER_ROMMON

    @property
    def can_change_baud(self) -> bool:
        # Not as a separate step. `xmodem -s<rate>` carries it.
        return False

    def inventory_command(self) -> tuple[bytes, ...]:
        return (b"dir flash:\r",)

    def parse_inventory(self, output: str) -> FlashInventory:
        return _parse(output, _ROMMON_ROW)

    def set_baud_command(self, baud: int) -> bytes | None:
        del baud
        return None

    def restore_baud_command(self) -> bytes | None:
        return None

    def transfer_command(self, filename: str, baud: int) -> bytes:
        # `-c` is CRC-16 error checking; `-s` sets the transfer speed inline.
        flags = f"-c{'' if baud == 9600 else f's{baud}'}"
        return f"xmodem {flags} {filename}\r".encode("ascii")

    def boot_command(self, filename: str) -> bytes:
        return f"boot flash:{filename}\r".encode("ascii")


def _parse(output: str, row: re.Pattern[str]) -> FlashInventory:
    files = tuple(
        FlashFile(name=match.group("name"), size=int(match.group("size")))
        for match in row.finditer(output)
    )
    free = _FREE_SPACE.search(output)
    total = _TOTAL_SPACE.search(output)
    return FlashInventory(
        files=files,
        free_bytes=int(free.group(1)) if free else None,
        total_bytes=int(total.group(1)) if total else None,
    )


def classify(state: State, model: str | None) -> Family:
    """Work out which recovery family applies, from state first.

    State is more reliable than model here: a device sitting at ``switch:`` is
    a Catalyst bootloader whatever its label says, and a device that never
    identified itself still announces which prompt it is at.
    """
    if state is State.BOOTLOADER:
        return Family.CATALYST_BOOTLOADER
    if state is State.ROMMON:
        return Family.ROUTER_ROMMON
    if state in (State.PRIV_EXEC, State.USER_EXEC):
        return Family.BOOTED_IOS
    del model
    return Family.UNSUPPORTED


def driver_for(state: State, model: str | None = None) -> ImageRecoveryDriver:
    """Pick a driver, or refuse.

    Refusing is the important half. A device in an unrecognised state used to
    fall through to the Catalyst path, which meant a router in ROMMON was sent
    ``set BAUD`` and ``copy xmodem: flash:`` -- commands it does not have.
    """
    family = classify(state, model)

    if family is Family.CATALYST_BOOTLOADER:
        return CatalystBootloaderDriver()
    if family is Family.ROUTER_ROMMON:
        return RouterRommonDriver()

    if family is Family.BOOTED_IOS:
        raise NoDriverError(
            "this device has booted IOS, so it does not need an image rescue. "
            "Copy the image with the device's own tools, or reload into the "
            "bootloader first."
        )

    raise NoDriverError(
        f"no verified image-recovery procedure for a device at "
        f"{state.value}. Image rescue is only supported from a Catalyst "
        f"bootloader (switch:) or a router ROMMON (rommon>), because those are "
        f"the only two procedures with a documented command set."
    )
