"""USB adapter fingerprinting and stable port identity.

``COM3`` today, ``COM5`` tomorrow; ``/dev/ttyUSB0`` this boot, ``/dev/ttyUSB2``
after a reconnect. Port names are assigned in enumeration order and enumeration
order is not stable, which is the daily indignity of a multi-device bench -- and
the direct cause of the worst hazard in this tool, which is erasing the wrong
device.

So ports are identified by what the USB descriptor says, not by what the OS
called them this time. Where an adapter reports a serial number that identity is
strong; where it does not -- most cheap Prolific clones report nothing -- the
fallback is physical topology, and the difference is reported rather than
smoothed over. Two identical no-serial adapters genuinely cannot be told apart
by software, and pretending otherwise would be the exact failure this module
exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Vendor IDs of the chipsets that make up essentially all USB console cables.
_VENDORS: dict[int, str] = {
    0x0403: "FTDI",
    0x067B: "Prolific",
    0x10C4: "Silicon Labs",
    0x1A86: "CH340",
    0x2341: "Arduino",
    0x0BDA: "Realtek",
    0x05AD: "Anchor",
    0x0557: "ATEN",
}

# Cisco's own USB console ports, which appear as CDC-ACM rather than a
# USB-serial bridge.
_CISCO_VENDOR = 0x1CF1
_VENDORS[_CISCO_VENDOR] = "Cisco"


class IdentityStrength(StrEnum):
    """How reliably a port can be recognised again later."""

    SERIAL = "serial"
    """The adapter reports a unique serial number. Survives replugging."""

    LOCATION = "location"
    """Only physical topology is available. Survives replug into the same hub
    port, not into a different one."""

    NAME_ONLY = "name_only"
    """Neither. The port name is all there is, and it is not stable."""


@dataclass(frozen=True, slots=True)
class PortIdentity:
    """A console endpoint and everything known about how to recognise it."""

    device: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    location: str | None = None
    description: str | None = None
    manufacturer: str | None = None

    @property
    def adapter(self) -> str:
        """Human name for the chipset, e.g. ``FTDI`` or ``Prolific``."""
        if self.vid is None:
            return "unknown"
        return _VENDORS.get(self.vid, f"{self.vid:04x}")

    @property
    def strength(self) -> IdentityStrength:
        if self.serial_number:
            return IdentityStrength.SERIAL
        if self.location:
            return IdentityStrength.LOCATION
        return IdentityStrength.NAME_ONLY

    @property
    def key(self) -> str:
        """Stable identity used for labels and for the transport lease.

        The lease keys on this rather than on the port name so that two labels
        resolving to the same physical adapter collide correctly.
        """
        if self.serial_number:
            return f"usb:{self.vid:04x}:{self.pid:04x}:{self.serial_number}"
        if self.location:
            return f"loc:{self.location}"
        return f"name:{self.device}"

    @property
    def is_ambiguous(self) -> bool:
        """True when this port cannot be distinguished from an identical twin."""
        return self.strength is IdentityStrength.NAME_ONLY

    def describe(self) -> str:
        parts = [self.adapter]
        if self.description and self.description != "n/a":
            parts.append(self.description)
        return " ".join(parts)


def enumerate_ports() -> tuple[PortIdentity, ...]:
    """List local serial ports with their USB descriptors.

    Returns an empty tuple rather than raising when pyserial is unavailable or
    the platform exposes nothing -- ``scan`` on a machine with no adapters is a
    legitimate, informative result, not an error.
    """
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover - pyserial is a hard dependency
        return ()

    identities: list[PortIdentity] = []
    for port in list_ports.comports():
        identities.append(
            PortIdentity(
                device=port.device,
                vid=port.vid,
                pid=port.pid,
                serial_number=port.serial_number or None,
                location=getattr(port, "location", None) or None,
                description=port.description or None,
                manufacturer=port.manufacturer or None,
            )
        )
    return tuple(sorted(identities, key=lambda identity: identity.device))


def find_ambiguities(
    identities: tuple[PortIdentity, ...],
) -> tuple[tuple[PortIdentity, ...], ...]:
    """Group ports that cannot be told apart from one another.

    Adapters sharing a vendor and product ID with no serial number and no
    location are genuinely indistinguishable. Callers should refuse to bind a
    label to any member of such a group rather than pick one.
    """
    groups: dict[tuple[int | None, int | None], list[PortIdentity]] = {}
    for identity in identities:
        if identity.strength is not IdentityStrength.NAME_ONLY:
            continue
        groups.setdefault((identity.vid, identity.pid), []).append(identity)
    return tuple(tuple(group) for group in groups.values() if len(group) > 1)
