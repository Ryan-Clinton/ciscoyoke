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
from ciscoyoke.identify.probe import intake
from ciscoyoke.journal.converge import Resolution, reconcile, roll_back
from ciscoyoke.journal.model import Mutation
from ciscoyoke.journal.store import connect, find_interrupted
from ciscoyoke.playbook import ios_switch
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State
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


_FLASH_KINDS = ("rename_flash_file", "delete_flash_file")


def _base(name: object) -> str:
    return str(name).split(":", 1)[-1].lstrip("/")


def _flash_listing(session: Session) -> frozenset[str] | None:
    """List flash from wherever the device is, or ``None`` if it cannot be read.

    Both a privileged IOS prompt and the Catalyst bootloader answer
    ``dir flash:`` with the same layout; nothing else can be asked.
    """
    if session.state.state not in (State.PRIV_EXEC, State.BOOTLOADER):
        return None
    before = len(session.tracker.buffer)
    if session.state.state is State.PRIV_EXEC:
        session.send(b"terminal length 0\r")
        session.settle()
        before = len(session.tracker.buffer)
    session.send(b"dir flash:\r")
    session.settle(timeout=20.0)
    session.drain_pager()
    listing = session.tracker.buffer.text[before:]
    return ios_switch.flash_files(listing) if "Directory of" in listing else None


def _probe_flash(port: str, mutation: Mutation) -> tuple[Resolution, str]:
    """Settle a rename or delete by listing flash. Names only, never contents."""
    try:
        with device_session(port) as session:
            intake(session, interrogate=False)
            files = _flash_listing(session)
    except CommandError as exc:
        return Resolution.INDETERMINATE, str(exc)
    if files is None:
        return (
            Resolution.INDETERMINATE,
            "flash can only be listed from a privileged prompt or the bootloader",
        )

    if mutation.kind == "delete_flash_file":
        name = _base(mutation.before.get("name"))
        if name in files:
            return Resolution.NOT_APPLIED, f"{name} is still in flash"
        return Resolution.APPLIED, f"{name} is not in flash"

    source = _base(mutation.before.get("name"))
    destination = _base(mutation.after.get("name"))
    if destination in files and source not in files:
        return Resolution.APPLIED, f"{destination} present, {source} absent"
    if source in files and destination not in files:
        return Resolution.NOT_APPLIED, f"{source} present, {destination} absent"
    return (
        Resolution.INDETERMINATE,
        f"{source} {'present' if source in files else 'absent'}, "
        f"{destination} {'present' if destination in files else 'absent'}: "
        f"neither the before nor the after state",
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
            if mutation.kind in _FLASH_KINDS:
                return _probe_flash(port, mutation)
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


def _compensate_rename(port: str, mutation: Mutation) -> bool:
    """Rename a file back, then confirm it from a fresh listing."""
    source = _base(mutation.before.get("name"))
    destination = _base(mutation.after.get("name"))
    try:
        with device_session(port) as session:
            intake(session, interrogate=False)
            state = session.state.state
            if state not in (State.PRIV_EXEC, State.BOOTLOADER):
                return False
            # IOS asks "Destination filename [..]?"; the bootloader does not.
            answer = b"\r" if state is State.PRIV_EXEC else b""
            session.send(
                f"rename flash:{destination} flash:{source}\r".encode() + answer
            )
            session.settle(timeout=20.0)
            files = _flash_listing(session)
    except CommandError:
        return False
    return files is not None and source in files and destination not in files


def _compensate(port: str, mutation: Mutation) -> bool:
    """Apply one compensation, confirming it took effect."""
    if mutation.kind == "rename_flash_file" and mutation.compensation is not None:
        return _compensate_rename(port, mutation)
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
