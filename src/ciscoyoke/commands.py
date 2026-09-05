"""Command implementations, kept separate from argument parsing.

``cli.py`` is about turning a command line into a call; this is about doing the
work. Splitting them means every command is callable and testable without going
through ``argparse``, which is what lets the CLI contract -- exit codes, JSON
shape, refusal messages -- be asserted directly.

Every command that can change a device takes a lease first and journals what it
does. The read-only ones do not, because holding a lock to look at something
would block a second terminal from doing the same harmlessly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ciscoyoke.identify.probe import intake
from ciscoyoke.journal.lease import Lease, TargetBusyError
from ciscoyoke.journal.store import Journal, connect, find_interrupted
from ciscoyoke.lifecycle import recover
from ciscoyoke.lifecycle import reset as reset_module
from ciscoyoke.lifecycle.archive import archive
from ciscoyoke.lifecycle.health import assess
from ciscoyoke.lifecycle.rescue import RescueReport, rescue
from ciscoyoke.playbook.base import PlanRefusedError, StepFailedError, execute, plan
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.session import Session
from ciscoyoke.transport.identity import PortIdentity, enumerate_ports
from ciscoyoke.transport.serial_ import (
    DEFAULT_BAUD,
    SerialTransport,
    SerialUnavailableError,
)


class CommandError(RuntimeError):
    """A command failed in a way that maps to an exit code."""

    def __init__(self, message: str, code: ExitCode) -> None:
        self.code = code
        super().__init__(message)


def identity_for(port: str) -> PortIdentity:
    """The USB identity behind a port name, or a name-only fallback.

    A lease keys on this, so a port that cannot be identified still gets a
    lease -- just a weaker one, keyed on the name, which is honest about what it
    can guarantee.
    """
    for identity in enumerate_ports():
        if identity.device == port:
            return identity
    return PortIdentity(device=port)


@contextmanager
def device_session(port: str, baud: int = DEFAULT_BAUD) -> Iterator[Session]:
    """Open a console session and close it afterwards."""
    try:
        transport = SerialTransport(port, baud=baud).open()
    except SerialUnavailableError as exc:
        raise CommandError(str(exc), ExitCode.DEVICE_NOT_FOUND) from exc
    try:
        yield Session(transport)
    finally:
        transport.close()


@contextmanager
def held(port: str, operation: str) -> Iterator[Lease]:
    """Take the transport lease, or fail with the holder's details."""
    identity = identity_for(port)
    lease = Lease(identity.key, target_name=port, operation=operation)
    try:
        lease.acquire()
    except TargetBusyError as exc:
        raise CommandError(str(exc), ExitCode.TARGET_LEASE_HELD) from exc
    try:
        yield lease
    finally:
        lease.release()


@contextmanager
def journal_for(port: str, operation: str) -> Iterator[tuple[sqlite3.Connection, Journal]]:
    identity = identity_for(port)
    connection = connect()
    try:
        journal = Journal.begin(
            connection,
            target_key=identity.key,
            target_name=port,
            operation=operation,
        )
        yield connection, journal
    finally:
        connection.close()


def interrupted_run(port: str) -> str | None:
    """Whether a previous run against this device never finished.

    Checked before any new operation. Starting a fresh recovery on top of an
    unresolved one is how a device ends up at a baud rate nobody recorded.
    """
    identity = identity_for(port)
    connection = connect()
    try:
        previous = find_interrupted(connection, identity.key)
        if previous is None:
            return None
        unresolved = previous.unresolved()
        if not unresolved:
            return None
        details = "; ".join(m.describe() for m in unresolved)
        return (
            f"a previous recovery on this device did not finish: {details}. "
            f"Resolve it before starting another operation."
        )
    finally:
        connection.close()


# -- read-only commands ----------------------------------------------------


def do_rescue(port: str, baud: int = DEFAULT_BAUD) -> RescueReport:
    """Diagnose, preserve, recommend. Takes no lease: nothing is changed."""
    with device_session(port, baud) as session:
        return rescue(session, port)


def do_health(port: str, baud: int = DEFAULT_BAUD) -> tuple[dict[str, object], ExitCode]:
    with device_session(port, baud) as session:
        found = intake(session)
        report = assess(session.tracker.buffer.text, found.facts)
    code = ExitCode.SUCCESS if not report.faults else ExitCode.STATE_UNCERTAIN
    return report.to_json(), code


@dataclass(frozen=True, slots=True)
class ArchiveOutcome:
    status: str
    gaps: tuple[str, ...]
    directory: Path | None
    rendered: str


def do_archive(
    port: str, output: Path | None, baud: int = DEFAULT_BAUD
) -> ArchiveOutcome:
    """Preserve what the device will disclose. Read-only, so no lease."""
    with device_session(port, baud) as session:
        found = intake(session)
        bundle = archive(session, found)

    directory = bundle.write(output) if output else None
    return ArchiveOutcome(
        status=bundle.status.value,
        gaps=tuple(a.name for a in bundle.at_risk),
        directory=directory,
        rendered=bundle.render(),
    )


# -- destructive commands --------------------------------------------------


def do_reset(
    port: str,
    *,
    baud: int = DEFAULT_BAUD,
    confirm: bool = False,
    accept_config_loss: bool = False,
    archive_to: Path | None = None,
) -> tuple[str, ExitCode]:
    """Reset a device to a known-empty baseline.

    Archives first, always. The archive is what turns the confirmation from
    "are you sure?" into a statement of exactly what is about to be lost, and
    on a locked device it is the only preservation that will ever be possible.
    """
    blocked = interrupted_run(port)
    if blocked:
        raise CommandError(blocked, ExitCode.INTERRUPTED_RECOVERY)

    with held(port, "reset"), device_session(port, baud) as session:
        found = intake(session)
        bundle = archive(session, found)
        if archive_to:
            bundle.write(archive_to)

        decision = reset_module.decide(
            bundle, accept_config_loss=accept_config_loss
        )
        if not decision.allowed:
            raise CommandError(
                f"{bundle.render()}\n\n{decision.render()}",
                ExitCode.DESTRUCTIVE_ACTION_REFUSED,
            )

        book = reset_module.for_model(found.facts.model.value)
        checked = plan(book, session.transport, session.state.state)
        if not checked.safe:
            raise CommandError(
                checked.render(), ExitCode.DESTRUCTIVE_ACTION_REFUSED
            )

        rendered = "\n\n".join(
            [bundle.render(), decision.render(), checked.render()]
        )
        if not confirm:
            return (
                f"{rendered}\n\nDry run. Nothing was sent. "
                f"Re-run with --confirm to proceed.",
                ExitCode.SUCCESS,
            )

        with journal_for(port, "reset") as (_connection, journal):
            try:
                execute(checked, session, journal, confirm=True)
            except (PlanRefusedError, StepFailedError) as exc:
                journal.finish("failed")
                raise CommandError(
                    str(exc), ExitCode.DESTRUCTIVE_ACTION_REFUSED
                ) from exc
            journal.finish("completed")

        return f"{rendered}\n\nReset complete.", ExitCode.SUCCESS


def do_recover_access(
    port: str, *, baud: int = DEFAULT_BAUD, confirm: bool = False
) -> tuple[str, ExitCode]:
    """Describe -- and with confirmation, run -- the access recovery path.

    Without ``--confirm`` this only reports what recovery would involve,
    including whether someone has to be physically at the device. That is worth
    knowing before starting: a Catalyst recovery cannot be completed from
    another room.
    """
    blocked = interrupted_run(port)
    if blocked:
        raise CommandError(blocked, ExitCode.INTERRUPTED_RECOVERY)

    with device_session(port, baud) as session:
        found = intake(session)
        path = recover.path_for(found.facts.model.value, session.state.state)

        if path.platform is recover.Platform.UNKNOWN:
            raise CommandError(path.note, ExitCode.STATE_UNCERTAIN)

        checked = plan(path.playbook, session.transport, session.state.state)
        rendered = f"{path.render()}\n\n{checked.render()}"

        if not confirm:
            return (
                f"{rendered}\n\nDry run. Nothing was sent. "
                f"Re-run with --confirm to proceed.",
                ExitCode.SUCCESS,
            )

        if not checked.safe:
            raise CommandError(rendered, ExitCode.DESTRUCTIVE_ACTION_REFUSED)

        # The physical prerequisite is not something this command can perform,
        # and pretending otherwise is the failure mode the HumanStep design
        # exists to prevent.
        if path.needs_a_person:
            raise CommandError(
                f"{rendered}\n\nThis recovery needs someone at the device:\n"
                f"  {path.human_step.instruction if path.human_step else ''}\n\n"
                f"Interactive recovery is not yet wired to the CLI.",
                ExitCode.STATE_UNCERTAIN,
            )

        with held(port, "recover_access"), journal_for(port, "recover_access") as (
            _connection,
            journal,
        ):
            try:
                execute(checked, session, journal, confirm=True)
            except (PlanRefusedError, StepFailedError) as exc:
                journal.finish("failed")
                raise CommandError(str(exc), ExitCode.TRANSFER_FAILED) from exc
            journal.finish("completed")

        return f"{rendered}\n\nAccess recovery complete.", ExitCode.SUCCESS
