"""Pushing configuration to several devices, and refusing to do it by halves.

Two rules shape this.

**Resolve everything before writing anything.** A lab file naming five devices
where only four are on the bench must fail before the first line of
configuration reaches the first device -- otherwise the operator is left with a
half-applied topology and no clear record of which half. Planning is
all-or-nothing; execution is per-device only after the plan is whole.

**A weak binding is not written to.** ``resolve`` already reports how confident
a match is; a low-confidence one means the tool is about to configure whatever
happened to enumerate at a port name this boot. That is the wrong-device hazard,
and it is refused rather than confirmed away.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ciscoyoke.stream.tracker import Confidence
from ciscoyoke.topology.labfile import (
    DeviceSpec,
    LabFile,
    LabFileError,
    Resolution,
    resolve,
)
from ciscoyoke.transport.identity import PortIdentity


class ApplyRefusedError(RuntimeError):
    """The lab cannot be applied safely, and nothing has been written."""


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """Every declared device bound to a real endpoint, or the reasons why not."""

    lab: LabFile
    resolutions: tuple[Resolution, ...] = ()
    problems: tuple[str, ...] = ()
    weak: tuple[Resolution, ...] = field(default=())

    @property
    def complete(self) -> bool:
        return not self.problems and len(self.resolutions) == len(self.lab.devices)

    @property
    def safe(self) -> bool:
        return self.complete and not self.weak

    def render(self) -> str:
        lines = [f"APPLY PLAN — {self.lab.name}", ""]
        for resolution in self.resolutions:
            mark = " " if resolution.safe_to_write else "!"
            lines.append(
                f"  {mark} {resolution.spec.name:<10} {resolution.identity.device:<10} "
                f"matched on {resolution.basis} "
                f"({resolution.confidence.value} confidence)"
            )
        if self.weak:
            lines += [
                "",
                "Refusing to write to these bindings: the match is only as strong",
                "as a port name, which identifies whatever enumerated there this",
                "boot rather than a specific device.",
            ]
        if self.problems:
            lines += ["", "REFUSED:"]
            lines.extend(f"  - {problem}" for problem in self.problems)
        return "\n".join(lines)


def make_plan(
    lab: LabFile,
    candidates: dict[PortIdentity, dict[str, str | None]],
) -> ApplyPlan:
    """Bind every declared device, collecting failures rather than stopping.

    Reporting all the unresolved devices at once matters: fixing them one
    error-message-at-a-time across five reruns is how people give up and start
    configuring by hand.
    """
    resolutions: list[Resolution] = []
    problems: list[str] = []

    for spec in lab.devices:
        try:
            resolutions.append(resolve(spec, candidates))
        except LabFileError as exc:
            problems.append(str(exc))

    weak = tuple(r for r in resolutions if not r.safe_to_write)

    return ApplyPlan(
        lab=lab,
        resolutions=tuple(resolutions),
        problems=tuple(problems),
        weak=weak,
    )


#: Applies one device's configuration. Returns the lines actually sent.
Applier = Callable[[Resolution, str], int]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    device: str
    endpoint: str
    lines: int
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def load_config(spec: DeviceSpec, base: Path | None) -> str:
    """Read a device's configuration file, relative to the lab file."""
    if not spec.config:
        return ""
    path = Path(spec.config)
    if not path.is_absolute() and base is not None:
        path = base / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApplyRefusedError(f"device {spec.name!r}: {exc}") from exc


def apply(
    plan: ApplyPlan,
    applier: Applier,
    *,
    allow_weak: bool = False,
    confirm: bool = False,
) -> tuple[ApplyResult, ...]:
    """Push configuration to every resolved device.

    Refuses unless the plan is complete, refuses weak bindings unless they are
    explicitly allowed, and refuses entirely without confirmation -- writing
    configuration to four devices at once is not something to do by accident.
    """
    if not plan.complete:
        raise ApplyRefusedError(
            "; ".join(plan.problems) or "not every declared device was resolved"
        )
    if plan.weak and not allow_weak:
        names = ", ".join(r.spec.name for r in plan.weak)
        raise ApplyRefusedError(
            f"refusing to write to weakly-matched devices: {names}. "
            f"Match on a serial number or an adapter label, or allow it "
            f"explicitly."
        )
    if not confirm:
        raise ApplyRefusedError(
            f"would configure {len(plan.resolutions)} device(s). "
            f"Re-run with confirmation to proceed."
        )

    base = plan.lab.source.parent if plan.lab.source else None
    results: list[ApplyResult] = []

    for resolution in plan.resolutions:
        if resolution.confidence is Confidence.LOW and not allow_weak:
            continue
        try:
            body = load_config(resolution.spec, base)
            lines = applier(resolution, body) if body else 0
            results.append(
                ApplyResult(
                    device=resolution.spec.name,
                    endpoint=resolution.identity.device,
                    lines=lines,
                )
            )
        except (ApplyRefusedError, OSError, RuntimeError) as exc:
            # One device failing does not undo the others -- there is no
            # transaction across four physical boxes -- so the failure is
            # recorded and reported rather than pretended away.
            results.append(
                ApplyResult(
                    device=resolution.spec.name,
                    endpoint=resolution.identity.device,
                    lines=0,
                    error=str(exc),
                )
            )

    return tuple(results)
