"""Support bundles, interactive console, and resolving an interrupted run.

Three things that only matter when something has gone wrong, which is when a
tool is judged.

A bug report against hardware nobody else owns is unactionable without the
device's actual behaviour -- so the bundle exists, and it refuses to include a
raw transcript, because a raw transcript may carry a previous owner's
credentials into a public issue tracker.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from ciscoyoke import __version__, doctor
from ciscoyoke.commands import CommandError, device_session, identity_for
from ciscoyoke.journal.converge import Resolution, reconcile, roll_back
from ciscoyoke.journal.model import Mutation
from ciscoyoke.journal.store import connect, find_interrupted
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.transport import labels
from ciscoyoke.transport.identity import enumerate_ports
from ciscoyoke.transport.serial_ import CANDIDATE_BAUDS

BUNDLE_NOTES = """ciscoyoke support bundle

Contains: host diagnostics, environment, port inventory, and any unfinished
journal for the named device.

It deliberately does NOT contain a session transcript. Attach one yourself,
and scrub it first:

    ciscoyoke transcript scrub session.ytx

Automated scrubbing cannot prove a transcript contains no sensitive data.
Read it before sending it to anyone.
"""


def do_support_bundle(output: Path, port: str | None = None) -> tuple[str, Path]:
    """Collect what a maintainer needs, minus what they must not receive."""
    output.mkdir(parents=True, exist_ok=True)

    diagnosis = doctor.run()
    (output / "doctor.json").write_text(
        json.dumps(diagnosis.to_json(), indent=2), encoding="utf-8"
    )

    environment = {
        "ciscoyoke_version": __version__,
        "python": sys.version,
        "platform": sys.platform,
        "ports": [
            {
                "device": identity.device,
                "adapter": identity.adapter,
                "identity_strength": identity.strength.value,
                "label": labels.name_for(identity),
            }
            for identity in enumerate_ports()
        ],
    }
    (output / "environment.json").write_text(
        json.dumps(environment, indent=2), encoding="utf-8"
    )

    journals: list[dict[str, object]] = []
    if port:
        identity = identity_for(port)
        connection = connect()
        try:
            previous = find_interrupted(connection, identity.key)
            if previous is not None:
                journals.append(
                    {
                        "run_id": previous.run_id,
                        "target": port,
                        "mutations": [m.to_json() for m in previous.mutations()],
                    }
                )
        finally:
            connection.close()
    (output / "journal.json").write_text(
        json.dumps(journals, indent=2), encoding="utf-8"
    )

    (output / "README.txt").write_text(BUNDLE_NOTES, encoding="utf-8")

    summary = (
        f"Support bundle written to {output}\n\n"
        f"  doctor.json       host diagnostics\n"
        f"  environment.json  versions and port inventory\n"
        f"  journal.json      {len(journals)} unfinished run(s)\n"
        f"  README.txt        what to attach and how to scrub it\n\n"
        f"No transcript is included. Scrub one and attach it yourself."
    )
    return summary, output


# -- resolving an interrupted run -------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    rendered: str
    code: ExitCode


def _probe_baud(port: str, mutation: Mutation) -> tuple[Resolution, str]:
    """Decide whether a recorded baud change actually took effect.

    Probes the journalled speed as well as the default -- which is the entire
    reason the journal records it. A device left at 115200 by an interrupted
    transfer looks dead to anything that only tries 9600.
    """
    expected = mutation.after.get("baud")
    original = mutation.before.get("baud")

    for candidate, verdict in (
        (expected, Resolution.APPLIED),
        (original, Resolution.NOT_APPLIED),
    ):
        if candidate is None:
            continue
        try:
            with device_session(port, int(candidate)) as session:
                session.observe_passively(listen=1.0)
                if session.tracker.buffer:
                    return verdict, f"device responded at {candidate}"
        except CommandError:
            continue

    return (
        Resolution.INDETERMINATE,
        "no response at either the journalled or the original speed",
    )


def do_resolve(
    port: str, *, rollback: bool = False
) -> ResolveOutcome:
    """Reconcile an interrupted run against the device, then resume or roll back.

    Nothing here trusts the journal. Every unresolved mutation is put to the
    device, and an entry that cannot be settled leaves the run blocked rather
    than being assumed one way -- continuing past an indeterminate mutation is
    exactly the recklessness the design refuses.
    """
    identity = identity_for(port)
    connection = connect()
    try:
        journal = find_interrupted(connection, identity.key)
        if journal is None:
            return ResolveOutcome(
                f"No interrupted run recorded for {port}.", ExitCode.SUCCESS
            )

        def probe(mutation: Mutation) -> tuple[Resolution, str]:
            if mutation.kind == "console_baud":
                return _probe_baud(port, mutation)
            return (
                Resolution.INDETERMINATE,
                f"no automatic check exists for {mutation.kind}",
            )

        report = reconcile(journal, probe)
        rendered = report.render()

        if not report.resumable:
            return ResolveOutcome(rendered, ExitCode.INTERRUPTED_RECOVERY)

        if rollback:
            stranded = roll_back(journal, lambda _m: _compensate(port, _m))
            journal.finish("rolled_back")
            rendered += "\n\nRolled back."
            if stranded:
                rendered += (
                    "\n\nThese could not be undone, and nothing can undo them:\n"
                    + "\n".join(f"  {m.describe()}" for m in stranded)
                )
            return ResolveOutcome(rendered, ExitCode.SUCCESS)

        journal.finish("resolved")
        return ResolveOutcome(
            rendered + "\n\nMarked resolved. The device is free for a new operation.",
            ExitCode.SUCCESS,
        )
    finally:
        connection.close()


def _compensate(port: str, mutation: Mutation) -> bool:
    """Apply one compensation, confirming it took effect."""
    if mutation.kind != "console_baud" or mutation.compensation is None:
        return False
    target = mutation.compensation.get("baud")
    if target is None:
        return False
    try:
        with device_session(port, int(target)) as session:
            session.observe_passively(listen=1.0)
            return bool(session.tracker.buffer)
    except CommandError:
        return False


def sweep_bauds() -> tuple[int, ...]:
    """The candidate line speeds, in the order worth trying."""
    return CANDIDATE_BAUDS
