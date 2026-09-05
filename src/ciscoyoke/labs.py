"""The ``lab`` commands: applying configuration and checking cabling.

CDP is what makes physical verification possible without addressing. It runs at
layer 2, so a bench of freshly-erased devices with no IP addresses can still be
asked what they are plugged into -- which is exactly when a lab is most likely
to be miscabled and least able to say so.

Reading it requires a privileged prompt, and enabling it where it is off is a
configuration change on a device that may not be ours. So a device that cannot
be reached is reported as *undiscoverable* rather than having its cables blamed,
and nothing is left switched on afterwards that was not on before.
"""

from __future__ import annotations

from pathlib import Path

from ciscoyoke.commands import CommandError, device_session
from ciscoyoke.identify.probe import intake
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State
from ciscoyoke.topology import apply as topology_apply
from ciscoyoke.topology import labfile
from ciscoyoke.topology.labfile import Resolution
from ciscoyoke.topology.verify import (
    Neighbour,
    parse_cdp,
    undeclared_links,
    verify,
)
from ciscoyoke.transport import labels
from ciscoyoke.transport.identity import PortIdentity, enumerate_ports
from ciscoyoke.transport.serial_ import DEFAULT_BAUD


def survey(baud: int = DEFAULT_BAUD) -> dict[PortIdentity, dict[str, str | None]]:
    """Identify every attached device, for lab-file binding.

    Read-only. A port that cannot be opened is skipped rather than fatal --
    another process may legitimately hold it, and one busy port should not stop
    a five-device lab from being checked.
    """
    candidates: dict[PortIdentity, dict[str, str | None]] = {}
    for identity in enumerate_ports():
        try:
            with device_session(identity.device, baud) as session:
                found = intake(session)
                candidates[identity] = {
                    "model": found.facts.model.value,
                    "serial": found.facts.serial_number.value,
                    "label": labels.name_for(identity),
                }
        except CommandError:
            continue
    return candidates


def collect_cdp(session: Session, device_name: str) -> tuple[Neighbour, ...]:
    """Ask one device what it can see.

    ``terminal length 0`` is session-scoped and does not survive a reload, so it
    stays inside the read-only guarantee while removing the pager that would
    otherwise truncate the neighbour list.
    """
    before = len(session.tracker.buffer)
    session.send(b"terminal length 0\r")
    session.settle()
    session.send(b"show cdp neighbors detail\r")
    session.settle(timeout=20.0)
    session.drain_pager()
    return parse_cdp(session.tracker.buffer.text[before:], device_name)


def _gather(
    resolutions: tuple[Resolution, ...], baud: int
) -> tuple[dict[str, tuple[Neighbour, ...]], tuple[str, ...]]:
    """Collect neighbours from every resolved device, noting who was skipped."""
    discovered: dict[str, tuple[Neighbour, ...]] = {}
    skipped: list[str] = []

    for resolution in resolutions:
        name = resolution.spec.name
        try:
            with device_session(resolution.identity.device, baud) as session:
                found = intake(session)
                if found.observation.state is not State.PRIV_EXEC:
                    # Without a privileged prompt CDP cannot be read. Reporting
                    # that beats declaring every one of its cables missing.
                    skipped.append(
                        f"{name} ({resolution.identity.device}): "
                        f"{found.observation.state.value}, not a privileged prompt"
                    )
                    continue
                discovered[name] = collect_cdp(session, name)
        except CommandError as exc:
            skipped.append(f"{name} ({resolution.identity.device}): {exc}")

    return discovered, tuple(skipped)


def do_lab_verify(
    lab_path: Path, *, baud: int = DEFAULT_BAUD
) -> tuple[str, dict[str, object], ExitCode]:
    """Diff declared cabling against what CDP reports."""
    try:
        lab = labfile.load(lab_path)
    except (OSError, labfile.LabFileError) as exc:
        raise CommandError(str(exc), ExitCode.DEVICE_NOT_FOUND) from exc

    plan = topology_apply.make_plan(lab, survey(baud))
    if not plan.complete:
        return (
            plan.render(),
            {"lab": lab.name, "verified": False, "problems": list(plan.problems)},
            ExitCode.DEVICE_NOT_FOUND,
        )

    discovered, skipped = _gather(plan.resolutions, baud)
    report = verify(lab, discovered)
    extra = undeclared_links(lab, discovered)

    rendered = report.render()
    if skipped:
        rendered += "\n\nNot checked:\n" + "\n".join(f"  {s}" for s in skipped)
    if extra:
        rendered += "\n\nUndeclared links (not errors, but worth knowing):\n"
        rendered += "\n".join(
            f"  {n.local_device}:{n.local_interface} -- "
            f"{n.remote_device}:{n.remote_interface}"
            for n in extra
        )

    payload = dict(report.to_json())
    payload["lab"] = lab.name
    payload["devices_reached"] = sorted(discovered)
    payload["not_checked"] = list(skipped)
    payload["undeclared_links"] = [
        f"{n.local_device}:{n.local_interface}" for n in extra
    ]

    code = ExitCode.SUCCESS if report.correct and not skipped else ExitCode.STATE_UNCERTAIN
    return rendered, payload, code


def push_config(session: Session, body: str) -> int:
    """Send a configuration body line by line, returning the count.

    Line by line rather than as one blob: an older device can drop characters
    from a large paste, and a configuration half-applied because of flow
    control is a genuinely miserable thing to debug.
    """
    lines = [line for line in body.splitlines() if line.strip()]
    session.send(b"configure terminal\r")
    session.settle()
    for line in lines:
        session.send(line.encode("latin-1") + b"\r")
        session.settle(timeout=5.0)
    session.send(b"end\r")
    session.settle()
    return len(lines)


def do_lab_apply(
    lab_path: Path,
    *,
    baud: int = DEFAULT_BAUD,
    confirm: bool = False,
    allow_weak: bool = False,
) -> tuple[str, dict[str, object], ExitCode]:
    """Push each device's configuration, all-or-nothing at plan time."""
    try:
        lab = labfile.load(lab_path)
    except (OSError, labfile.LabFileError) as exc:
        raise CommandError(str(exc), ExitCode.DEVICE_NOT_FOUND) from exc

    plan = topology_apply.make_plan(lab, survey(baud))

    if not confirm:
        return (
            f"{plan.render()}\n\nDry run. Nothing was sent. "
            f"Re-run with --confirm to proceed.",
            {"lab": lab.name, "applied": False},
            ExitCode.SUCCESS if plan.safe else ExitCode.DEVICE_NOT_FOUND,
        )

    def applier(resolution: Resolution, body: str) -> int:
        with device_session(resolution.identity.device, baud) as session:
            found = intake(session)
            if found.observation.state is not State.PRIV_EXEC:
                raise CommandError(
                    f"{resolution.spec.name} is at "
                    f"{found.observation.state.value}, not a privileged prompt",
                    ExitCode.AUTHENTICATION_REQUIRED,
                )
            return push_config(session, body)

    try:
        results = topology_apply.apply(
            plan, applier, allow_weak=allow_weak, confirm=True
        )
    except topology_apply.ApplyRefusedError as exc:
        raise CommandError(str(exc), ExitCode.DESTRUCTIVE_ACTION_REFUSED) from exc

    lines = [plan.render(), ""]
    for result in results:
        status = f"{result.lines} lines" if result.ok else f"FAILED: {result.error}"
        lines.append(f"  {result.device:<10} {result.endpoint:<10} {status}")

    failed = [r for r in results if not r.ok]
    code = ExitCode.SUCCESS if not failed else ExitCode.STATE_UNCERTAIN

    return (
        "\n".join(lines),
        {
            "lab": lab.name,
            "applied": True,
            "results": [
                {
                    "device": r.device,
                    "endpoint": r.endpoint,
                    "lines": r.lines,
                    "error": r.error,
                }
                for r in results
            ],
        },
        code,
    )
