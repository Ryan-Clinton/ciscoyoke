"""Transport interface and the tri-state capability model.

``send_break=True`` conflates three different claims: that pySerial exposes the
call, that this particular adapter chipset actually drives a break condition,
and that a Cisco device has ever been observed responding to one from this
adapter. Only the third is what a recovery playbook actually needs.

Conflating them means discovering the truth halfway through a recovery, with
the device already in an intermediate state -- the worst possible moment.
Separating them means a playbook whose requirements exceed what the transport
can demonstrate fails at *plan* time, while the device is still untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, runtime_checkable


class Support(StrEnum):
    """How well a capability is established."""

    SUPPORTED = "supported"
    """The layer exposes it and it is expected to work."""

    INFERRED = "inferred"
    """Expected from known chipset behaviour, never demonstrated here."""

    UNSUPPORTED = "unsupported"
    """Known not to be available on this transport."""

    UNKNOWN = "unknown"
    """Not yet probed."""


@dataclass(frozen=True, slots=True)
class Capability:
    """One transport ability, with the strength of the claim behind it."""

    name: str
    api_support: Support = Support.UNKNOWN
    adapter_support: Support = Support.UNKNOWN
    device_verified: bool = False
    verified_on: str | None = None

    @property
    def usable(self) -> bool:
        """Whether a playbook may *attempt* to rely on this.

        Deliberately not the same as ``device_verified``: requiring proof
        before first use would mean no capability could ever be verified. What
        is refused is the combination of an unsupported API and a playbook that
        needs it.
        """
        return self.api_support in (Support.SUPPORTED, Support.INFERRED)

    def verify(self, device: str) -> Capability:
        """Record that a device demonstrably responded to this capability."""
        return replace(self, device_verified=True, verified_on=device)

    def describe(self) -> str:
        verification = (
            f"verified on {self.verified_on}"
            if self.device_verified
            else "not tested"
        )
        return (
            f"{self.name}\n"
            f"  API support          {self.api_support.value}\n"
            f"  adapter support      {self.adapter_support.value}\n"
            f"  device verification  {verification}"
        )


@dataclass(frozen=True, slots=True)
class TransportCapabilities:
    """What a transport can do. Checked before a playbook runs, never during."""

    read_write: Capability
    send_break: Capability
    change_baud: Capability
    modem_lines: Capability
    power_cycle: Capability

    def get(self, name: str) -> Capability:
        try:
            return getattr(self, name)  # type: ignore[no-any-return]
        except AttributeError as exc:
            raise KeyError(f"unknown capability: {name}") from exc

    def missing(self, required: tuple[str, ...]) -> tuple[str, ...]:
        """Which of ``required`` this transport cannot offer."""
        return tuple(name for name in required if not self.get(name).usable)

    @classmethod
    def local_serial(cls) -> TransportCapabilities:
        """A directly attached USB or built-in serial port."""
        return cls(
            read_write=Capability("read_write", Support.SUPPORTED, Support.SUPPORTED),
            send_break=Capability("send_break", Support.SUPPORTED, Support.INFERRED),
            change_baud=Capability("change_baud", Support.SUPPORTED, Support.SUPPORTED),
            modem_lines=Capability("modem_lines", Support.SUPPORTED, Support.INFERRED),
            power_cycle=Capability("power_cycle", Support.UNSUPPORTED),
        )

    @classmethod
    def rfc2217(cls) -> TransportCapabilities:
        """A ser2net or ConsolePi endpoint. Plaintext, but control-capable."""
        return cls(
            read_write=Capability("read_write", Support.SUPPORTED, Support.SUPPORTED),
            send_break=Capability("send_break", Support.SUPPORTED, Support.INFERRED),
            change_baud=Capability("change_baud", Support.SUPPORTED, Support.INFERRED),
            modem_lines=Capability("modem_lines", Support.INFERRED),
            power_cycle=Capability("power_cycle", Support.UNSUPPORTED),
        )

    @classmethod
    def raw_socket(cls) -> TransportCapabilities:
        """A bare ``socket://`` endpoint.

        pySerial is explicit that for this handler "all serial port settings,
        control and status lines are ignored. Only data is transmitted and
        received" -- so break-based ROMMON entry and the baud-drop fallback are
        both impossible here, and a playbook needing them must fail at planning.
        """
        return cls(
            read_write=Capability("read_write", Support.SUPPORTED, Support.SUPPORTED),
            send_break=Capability("send_break", Support.UNSUPPORTED),
            change_baud=Capability("change_baud", Support.UNSUPPORTED),
            modem_lines=Capability("modem_lines", Support.UNSUPPORTED),
            power_cycle=Capability("power_cycle", Support.UNSUPPORTED),
        )


class CapabilityUnavailableError(RuntimeError):
    """A required capability is not offered by the selected transport."""

    def __init__(self, missing: tuple[str, ...], transport: str) -> None:
        self.missing = missing
        self.transport = transport
        super().__init__(
            f"{transport} cannot provide: {', '.join(missing)}. "
            f"Refusing at plan time rather than part-way through the device."
        )


@runtime_checkable
class Transport(Protocol):
    """The minimum a console endpoint must offer."""

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> TransportCapabilities: ...

    def read(self) -> bytes: ...

    def write(self, data: bytes) -> int: ...


def require(transport: Transport, capabilities: tuple[str, ...]) -> None:
    """Raise unless ``transport`` can offer everything in ``capabilities``."""
    missing = transport.capabilities.missing(capabilities)
    if missing:
        raise CapabilityUnavailableError(missing, transport.name)
