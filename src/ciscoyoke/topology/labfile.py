"""The lab file: declaring a topology you cannot instantiate.

Containerlab's shape, adapted for hardware. The essential difference is that a
virtual lab *creates* its nodes, whereas a physical one can only bind to what is
already on the bench -- so ``devices`` describes how to *find* each box rather
than how to build it.

Matching resolves in specificity order: serial number, then model, then adapter
label, then port. Two rules make that safe rather than convenient:

* a match carries the confidence of the identity it matched on, and a
  low-confidence match on a device about to be written to is refused;
* an ambiguous match is an error naming both candidates, never a coin flip.

Written against the standard library only. YAML would be a pleasanter surface,
but adding a parser dependency to read a dozen keys is not a trade worth making
for a tool whose selling point is that it runs anywhere with a serial port.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ciscoyoke.stream.tracker import Confidence
from ciscoyoke.transport.identity import IdentityStrength, PortIdentity

#: `r1:FastEthernet0/0`
_ENDPOINT = re.compile(r"^\s*(?P<device>[\w.-]+)\s*:\s*(?P<interface>[\w./-]+)\s*$")


class LabFileError(ValueError):
    """The lab file could not be understood."""


class AmbiguousMatchError(LabFileError):
    """A device selector matched more than one endpoint."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One end of a cable."""

    device: str
    interface: str

    def __str__(self) -> str:
        return f"{self.device}:{self.interface}"

    @classmethod
    def parse(cls, text: str) -> Endpoint:
        match = _ENDPOINT.match(text)
        if not match:
            raise LabFileError(
                f"{text!r} is not a valid endpoint; expected device:interface"
            )
        return cls(match.group("device"), match.group("interface"))


@dataclass(frozen=True, slots=True)
class Link:
    """A declared cable between two endpoints."""

    a: Endpoint
    b: Endpoint

    def __str__(self) -> str:
        return f"{self.a} <-> {self.b}"

    @property
    def devices(self) -> tuple[str, str]:
        return (self.a.device, self.b.device)


@dataclass(frozen=True, slots=True)
class DeviceMatch:
    """How to find one declared device on the bench."""

    serial: str | None = None
    model: str | None = None
    label: str | None = None
    port: str | None = None

    @property
    def specificity(self) -> int:
        """Higher is more specific. Drives resolution order."""
        if self.serial:
            return 4
        if self.model:
            return 3
        if self.label:
            return 2
        if self.port:
            return 1
        return 0

    @property
    def confidence(self) -> Confidence:
        """How much a successful match on this selector is worth.

        A serial number identifies a specific chassis. A port name identifies
        whatever happened to enumerate there this boot, which is why it is the
        weakest and why writing to it is gated.
        """
        return {
            4: Confidence.HIGH,
            3: Confidence.MEDIUM,
            2: Confidence.MEDIUM,
            1: Confidence.LOW,
        }.get(self.specificity, Confidence.LOW)

    def describe(self) -> str:
        for name in ("serial", "model", "label", "port"):
            value = getattr(self, name)
            if value:
                return f"{name}={value}"
        return "unspecified"


@dataclass(frozen=True, slots=True)
class DeviceSpec:
    """A declared device and the configuration it should receive."""

    name: str
    match: DeviceMatch
    config: str | None = None


@dataclass(frozen=True, slots=True)
class LabFile:
    """A parsed lab definition."""

    name: str
    devices: tuple[DeviceSpec, ...]
    cabling: tuple[Link, ...] = ()
    description: str = ""
    source: Path | None = field(default=None)

    def device(self, name: str) -> DeviceSpec | None:
        for spec in self.devices:
            if spec.name == name:
                return spec
        return None

    def validate(self) -> tuple[str, ...]:
        """Structural problems, as messages. Empty means well-formed."""
        problems: list[str] = []
        names = {spec.name for spec in self.devices}

        seen: set[str] = set()
        for spec in self.devices:
            if spec.name in seen:
                problems.append(f"device {spec.name!r} is declared more than once")
            seen.add(spec.name)
            if spec.match.specificity == 0:
                problems.append(
                    f"device {spec.name!r} has no match criteria; it cannot be found"
                )

        for link in self.cabling:
            for endpoint in (link.a, link.b):
                if endpoint.device not in names:
                    problems.append(
                        f"cabling references undeclared device "
                        f"{endpoint.device!r} in {link}"
                    )
            if link.a.device == link.b.device and link.a.interface == link.b.interface:
                problems.append(f"{link} connects an interface to itself")

        return tuple(problems)


def parse(text: str, *, source: Path | None = None) -> LabFile:
    """Read a lab file.

    JSON, because it is in the standard library and a lab file is a dozen keys.
    The schema mirrors the YAML in the specification exactly, so moving to YAML
    later is a parser swap and not a format change.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LabFileError(f"not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise LabFileError("a lab file must be an object")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise LabFileError("a lab file must have a name")

    devices_raw = raw.get("devices")
    if not isinstance(devices_raw, dict) or not devices_raw:
        raise LabFileError("a lab file must declare at least one device")

    devices: list[DeviceSpec] = []
    for device_name, body in devices_raw.items():
        if not isinstance(body, dict):
            raise LabFileError(f"device {device_name!r} must be an object")
        match_raw = body.get("match")
        if not isinstance(match_raw, dict):
            raise LabFileError(f"device {device_name!r} must have a match block")
        devices.append(
            DeviceSpec(
                name=str(device_name),
                match=DeviceMatch(
                    serial=_optional_str(match_raw, "serial"),
                    model=_optional_str(match_raw, "model"),
                    label=_optional_str(match_raw, "label"),
                    port=_optional_str(match_raw, "port"),
                ),
                config=_optional_str(body, "config"),
            )
        )

    cabling: list[Link] = []
    for entry in raw.get("cabling") or ():
        if not isinstance(entry, list) or len(entry) != 2:
            raise LabFileError(
                f"cabling entry {entry!r} must be a pair of endpoints"
            )
        cabling.append(Link(Endpoint.parse(entry[0]), Endpoint.parse(entry[1])))

    lab = LabFile(
        name=name,
        devices=tuple(devices),
        cabling=tuple(cabling),
        description=str(raw.get("description") or ""),
        source=source,
    )

    problems = lab.validate()
    if problems:
        raise LabFileError("; ".join(problems))
    return lab


def load(path: Path) -> LabFile:
    return parse(path.read_text(encoding="utf-8"), source=path)


def _optional_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    return str(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class Resolution:
    """A declared device bound to a real endpoint."""

    spec: DeviceSpec
    identity: PortIdentity
    confidence: Confidence
    basis: str

    @property
    def safe_to_write(self) -> bool:
        """Whether this binding is strong enough to write to.

        A low-confidence match means the tool is about to configure -- or erase --
        whatever happened to enumerate at a port name this boot. That is the
        wrong-device hazard exactly, so it is refused rather than confirmed.
        """
        return self.confidence is not Confidence.LOW


def resolve(
    spec: DeviceSpec,
    candidates: dict[PortIdentity, dict[str, str | None]],
) -> Resolution:
    """Bind one declared device to an endpoint, or explain why not.

    ``candidates`` maps each discovered port to what is known about the device
    behind it -- ``{"model": ..., "serial": ..., "label": ...}`` -- so matching
    can use device identity where it exists and fall back explicitly where it
    does not.
    """
    matches: list[tuple[PortIdentity, str]] = []

    for identity, facts in candidates.items():
        if spec.match.serial and facts.get("serial") == spec.match.serial:
            matches.append((identity, f"serial {spec.match.serial}"))
        elif spec.match.model and facts.get("model") == spec.match.model:
            matches.append((identity, f"model {spec.match.model}"))
        elif spec.match.label and facts.get("label") == spec.match.label:
            matches.append((identity, f"label {spec.match.label}"))
        elif spec.match.port and identity.device == spec.match.port:
            matches.append((identity, f"port {spec.match.port}"))

    if not matches:
        raise LabFileError(
            f"device {spec.name!r} ({spec.match.describe()}) was not found on "
            f"the bench"
        )
    if len(matches) > 1:
        names = ", ".join(identity.device for identity, _ in matches)
        raise AmbiguousMatchError(
            f"device {spec.name!r} ({spec.match.describe()}) matches more than "
            f"one endpoint: {names}. Narrow the match rather than guessing."
        )

    identity, basis = matches[0]
    confidence = spec.match.confidence
    if (
        confidence is not Confidence.LOW
        and identity.strength is IdentityStrength.NAME_ONLY
    ):
        # The selector was specific, but the endpoint behind it cannot be
        # recognised again. The binding is only as strong as its weakest half.
        confidence = Confidence.LOW

    return Resolution(
        spec=spec, identity=identity, confidence=confidence, basis=basis
    )
