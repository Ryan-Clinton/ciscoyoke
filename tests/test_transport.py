"""Capability semantics and adapter identity."""

from __future__ import annotations

import pytest

from ciscoyoke.transport.base import (
    CapabilityUnavailableError,
    Support,
    TransportCapabilities,
    require,
)
from ciscoyoke.transport.identity import (
    IdentityStrength,
    PortIdentity,
    find_ambiguities,
)


class StubTransport:
    def __init__(self, name: str, capabilities: TransportCapabilities) -> None:
        self._name = name
        self._capabilities = capabilities

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> TransportCapabilities:
        return self._capabilities

    def read(self) -> bytes:  # pragma: no cover - not exercised here
        return b""

    def write(self, data: bytes) -> int:  # pragma: no cover - not exercised here
        return len(data)


def test_raw_socket_cannot_break_or_change_baud() -> None:
    """pySerial ignores all control lines on ``socket://``.

    So break-based ROMMON entry and the baud-drop fallback are both impossible
    there, and a playbook needing them has to fail before it starts.
    """
    capabilities = TransportCapabilities.raw_socket()
    assert capabilities.send_break.usable is False
    assert capabilities.change_baud.usable is False


def test_playbook_requirements_fail_at_plan_time() -> None:
    """The whole point of the capability model.

    Discovering a missing capability halfway through leaves the device in an
    intermediate state -- the worst possible moment. This fails while the
    device is still untouched.
    """
    transport = StubTransport("socket://consolepi:7001", TransportCapabilities.raw_socket())

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        require(transport, ("send_break", "change_baud"))

    assert excinfo.value.missing == ("send_break", "change_baud")
    assert "plan time" in str(excinfo.value)


def test_local_serial_supports_recovery_paths() -> None:
    transport = StubTransport("COM3", TransportCapabilities.local_serial())
    require(transport, ("read_write", "send_break", "change_baud"))


def test_capability_is_usable_before_it_is_verified() -> None:
    """Requiring proof before first use would mean nothing could ever be proven.

    ``usable`` gates whether a playbook may attempt something; ``device_verified``
    records whether a device has actually demonstrated it.
    """
    capability = TransportCapabilities.local_serial().send_break
    assert capability.adapter_support is Support.INFERRED
    assert capability.usable is True
    assert capability.device_verified is False


def test_verification_is_earned_and_attributed() -> None:
    capability = TransportCapabilities.local_serial().send_break
    verified = capability.verify("CISCO1760")

    assert verified.device_verified is True
    assert verified.verified_on == "CISCO1760"
    assert "verified on CISCO1760" in verified.describe()
    # Immutable: verifying returns a new capability rather than mutating shared
    # state that another transport might be relying on.
    assert capability.device_verified is False


def test_serial_number_gives_the_strongest_identity() -> None:
    identity = PortIdentity(
        device="COM3", vid=0x0403, pid=0x6001, serial_number="A10K8Z4P"
    )
    assert identity.adapter == "FTDI"
    assert identity.strength is IdentityStrength.SERIAL
    assert identity.key == "usb:0403:6001:A10K8Z4P"
    assert identity.is_ambiguous is False


def test_location_is_the_fallback_identity() -> None:
    identity = PortIdentity(device="COM5", vid=0x067B, pid=0x2303, location="1-4.3")
    assert identity.adapter == "Prolific"
    assert identity.strength is IdentityStrength.LOCATION
    assert identity.key == "loc:1-4.3"


def test_indistinguishable_adapters_are_reported_not_guessed() -> None:
    """Two identical no-serial adapters genuinely cannot be told apart.

    Pretending otherwise is exactly the failure that leads to erasing the wrong
    device, so they are surfaced as a group and a label must be refused.
    """
    identities = (
        PortIdentity(device="COM6", vid=0x067B, pid=0x2303),
        PortIdentity(device="COM7", vid=0x067B, pid=0x2303),
        PortIdentity(device="COM3", vid=0x0403, pid=0x6001, serial_number="A1"),
    )
    groups = find_ambiguities(identities)

    assert len(groups) == 1
    assert {identity.device for identity in groups[0]} == {"COM6", "COM7"}


def test_distinct_adapters_are_not_flagged() -> None:
    identities = (
        PortIdentity(device="COM3", vid=0x0403, pid=0x6001, serial_number="A1"),
        PortIdentity(device="COM4", vid=0x0403, pid=0x6001, serial_number="A2"),
    )
    assert find_ambiguities(identities) == ()
