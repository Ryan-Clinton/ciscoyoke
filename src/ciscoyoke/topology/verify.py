"""Declared cabling versus discovered cabling.

With more than three devices on a bench, "is it patched the way I meant?" stops
being answerable by looking. CDP already knows: it operates at layer 2 and needs
no addressing at all, so a bench of freshly-erased devices with no IP addresses
can still be checked -- which is exactly when a lab is most likely to be
miscabled and least able to tell you.

Four outcomes rather than pass/fail, because "wrong" is not actionable and each
of these implies a different fix:

    ✓ correct          the cable is where it was declared
    ✗ misconnected     something is plugged in, but not the declared neighbour
    ? missing          nothing is plugged in where a cable was declared
    ! self-looped      both ends land on the same device

Anything this enables to perform the check is journalled and restored
afterwards. A verification tool that leaves the device different from how it
found it is a misconfiguration tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ciscoyoke.topology.labfile import Endpoint, LabFile, Link

# `show cdp neighbors detail` records, which span several lines each.
_NEIGHBOUR = re.compile(
    r"Device ID:\s*(?P<device>\S+).*?"
    r"Interface:\s*(?P<local>[\w./-]+),\s*"
    r"Port ID \(outgoing port\):\s*(?P<remote>[\w./-]+)",
    re.DOTALL | re.IGNORECASE,
)

# Cisco abbreviates interface names inconsistently between commands, so both
# sides are normalised before comparison.
_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    ("tengigabitethernet", "te"),
    ("gigabitethernet", "gi"),
    ("fastethernet", "fa"),
    ("ethernet", "et"),
    ("serial", "se"),
    ("loopback", "lo"),
    ("vlan", "vl"),
)


def normalise_interface(name: str) -> str:
    """Reduce an interface name to a comparable form.

    ``FastEthernet0/1``, ``Fa0/1`` and ``fastethernet 0/1`` are the same port.
    Without this, a correct cable reads as a mismatch purely because two Cisco
    commands disagree about how to spell it.
    """
    text = name.strip().lower().replace(" ", "")
    for long_form, short_form in _ABBREVIATIONS:
        if text.startswith(long_form):
            return short_form + text[len(long_form) :]
        if text.startswith(short_form):
            return text
    return text


def normalise_device(name: str) -> str:
    """Strip the domain suffix CDP often appends to a hostname."""
    return name.strip().split(".")[0].lower()


class Outcome(StrEnum):
    CORRECT = "correct"
    MISCONNECTED = "misconnected"
    MISSING = "missing"
    SELF_LOOPED = "self_looped"
    UNDISCOVERABLE = "undiscoverable"


@dataclass(frozen=True, slots=True)
class Neighbour:
    """One adjacency the device reported."""

    local_device: str
    local_interface: str
    remote_device: str
    remote_interface: str

    @property
    def key(self) -> tuple[str, str]:
        return (normalise_device(self.local_device), normalise_interface(self.local_interface))


def parse_cdp(output: str, local_device: str) -> tuple[Neighbour, ...]:
    """Read ``show cdp neighbors detail`` output from one device."""
    return tuple(
        Neighbour(
            local_device=local_device,
            local_interface=match.group("local"),
            remote_device=match.group("device"),
            remote_interface=match.group("remote"),
        )
        for match in _NEIGHBOUR.finditer(output)
    )


@dataclass(frozen=True, slots=True)
class Finding:
    """One declared cable, checked."""

    link: Link
    outcome: Outcome
    observed: Neighbour | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.CORRECT

    def render(self) -> str:
        marks = {
            Outcome.CORRECT: "✓",
            Outcome.MISCONNECTED: "✗",
            Outcome.MISSING: "?",
            Outcome.SELF_LOOPED: "!",
            Outcome.UNDISCOVERABLE: "-",
        }
        left = f"{self.link.a}"
        right = f"{self.link.b}"
        line = f"{marks[self.outcome]} {left:<20} ── {right:<20}"
        if self.detail:
            line += f"  {self.detail}"
        return line

    def to_json(self) -> dict[str, object]:
        return {
            "declared": [str(self.link.a), str(self.link.b)],
            "outcome": self.outcome.value,
            "detail": self.detail,
            "observed": (
                f"{self.observed.remote_device}:{self.observed.remote_interface}"
                if self.observed
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    findings: tuple[Finding, ...]

    @property
    def correct(self) -> bool:
        return all(finding.ok for finding in self.findings)

    @property
    def problems(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if not finding.ok)

    def render(self) -> str:
        if not self.findings:
            return "No cabling declared."
        lines = [finding.render() for finding in self.findings]
        lines.append("")
        if self.correct:
            lines.append(f"All {len(self.findings)} declared links verified.")
        else:
            lines.append(
                f"{len(self.problems)} of {len(self.findings)} declared links "
                f"do not match the topology."
            )
        return "\n".join(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "correct": self.correct,
            "findings": [finding.to_json() for finding in self.findings],
        }


def verify(
    lab: LabFile,
    discovered: dict[str, tuple[Neighbour, ...]],
) -> VerificationReport:
    """Diff what CDP found against what the lab file declared.

    ``discovered`` maps each device name to the adjacencies it reported. A
    device absent from the mapping is not treated as a fault in the cabling --
    it means the device could not be reached, which is a different problem and
    is reported as ``undiscoverable`` rather than blamed on a cable.
    """
    index: dict[tuple[str, str], Neighbour] = {}
    for neighbours in discovered.values():
        for neighbour in neighbours:
            index[neighbour.key] = neighbour

    findings: list[Finding] = []
    for link in lab.cabling:
        findings.append(_check(link, index, discovered))
    return VerificationReport(tuple(findings))


def _check(
    link: Link,
    index: dict[tuple[str, str], Neighbour],
    discovered: dict[str, tuple[Neighbour, ...]],
) -> Finding:
    if link.a.device not in discovered:
        return Finding(
            link,
            Outcome.UNDISCOVERABLE,
            detail=f"{link.a.device} was not reachable, so this cable was not checked",
        )

    key = (normalise_device(link.a.device), normalise_interface(link.a.interface))
    observed = index.get(key)

    if observed is None:
        return Finding(link, Outcome.MISSING, detail="(no neighbour)")

    remote_device = normalise_device(observed.remote_device)
    remote_interface = normalise_interface(observed.remote_interface)

    if remote_device == normalise_device(link.a.device):
        return Finding(
            link,
            Outcome.SELF_LOOPED,
            observed,
            detail=(
                f"loop: both ends on {link.a.device} "
                f"({observed.local_interface} → {observed.remote_interface})"
            ),
        )

    if (
        remote_device == normalise_device(link.b.device)
        and remote_interface == normalise_interface(link.b.interface)
    ):
        return Finding(link, Outcome.CORRECT, observed)

    return Finding(
        link,
        Outcome.MISCONNECTED,
        observed,
        detail=(
            f"found {observed.remote_device}:{observed.remote_interface}, "
            f"expected {link.b}"
        ),
    )


def undeclared_links(
    lab: LabFile, discovered: dict[str, tuple[Neighbour, ...]]
) -> tuple[Neighbour, ...]:
    """Adjacencies present on the bench but absent from the lab file.

    Not an error -- a management cable is a perfectly good reason -- but worth
    surfacing, because an unexpected link is how a lab quietly stops testing
    what it claims to test.
    """
    declared: set[tuple[str, str]] = set()
    for link in lab.cabling:
        for endpoint in (link.a, link.b):
            declared.add(
                (normalise_device(endpoint.device), normalise_interface(endpoint.interface))
            )

    extra: list[Neighbour] = []
    for neighbours in discovered.values():
        for neighbour in neighbours:
            if neighbour.key not in declared:
                extra.append(neighbour)
    return tuple(extra)


def endpoint_of(neighbour: Neighbour) -> Endpoint:
    return Endpoint(neighbour.local_device, neighbour.local_interface)
