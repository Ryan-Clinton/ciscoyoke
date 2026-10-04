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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from ciscoyoke.diagnose import failure_path, record_failure
from ciscoyoke.identify import memory
from ciscoyoke.identify.facts import DeviceFacts
from ciscoyoke.identify.probe import intake
from ciscoyoke.journal.lease import Lease, TargetBusyError
from ciscoyoke.journal.paths import transcripts_dir
from ciscoyoke.journal.store import Journal, connect, find_interrupted
from ciscoyoke.lifecycle import recover
from ciscoyoke.lifecycle import reset as reset_module
from ciscoyoke.lifecycle.archive import (
    Archive,
    archive,
    capture_boot_environment,
    capture_files,
)
from ciscoyoke.lifecycle.health import assess
from ciscoyoke.lifecycle.rescue import RescueReport, rescue
from ciscoyoke.platform_profiles import (
    CatalystProfile,
    catalyst_profile,
    unverified_refusal,
)
from ciscoyoke.playbook import ios_switch
from ciscoyoke.playbook.base import PlanRefusedError, StepFailedError, execute, plan
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.session import Session, SessionTimeoutError
from ciscoyoke.stream.tracker import Basis, State
from ciscoyoke.transcript.schema import write as write_transcript
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


# -- shared by the destructive commands -------------------------------------


@contextmanager
def recorded(session: Session, operation: str) -> Iterator[Path]:
    """Save the session's raw transcript when the block exits, however it exits.

    A run that fails part-way is exactly the run whose recording matters most,
    so this writes in ``finally`` -- and yields the path up front so a failure
    message can say where to look.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = transcripts_dir() / f"{stamp}-{operation}.ytx"
    try:
        yield path
    finally:
        write_transcript(path, session.recorder.transcript())


def save_failure(
    port: str,
    operation: str,
    error: BaseException,
    session: Session,
    transcript: Path,
    *,
    profile: CatalystProfile | None = None,
    model: str | None = None,
) -> str:
    """Write what a failed run knew, and say where it went and what to do next.

    Returns text to append to the failure message. Never raises: a failure to
    write diagnostics must not replace the failure being diagnosed.
    """
    try:
        record = record_failure(
            operation=operation,
            error=error,
            tracker=session.tracker,
            identity=identity_for(port),
            profile=profile.to_json() if profile is not None else None,
            model=model,
            transcript=transcript,
        )
        path = record.write(failure_path(transcript))
    except Exception as exc:
        return f"\n\n(diagnostics could not be saved: {exc})"
    return (
        f"\n\n{record.summary()}\n\n"
        f"Diagnostics saved beside the recording:\n  {path}\n"
        f"To send a report that lets this be fixed without the hardware:\n"
        f"  ciscoyoke report"
    )


def note_identity(port: str, facts: DeviceFacts, source: str) -> None:
    """Remember an observed model against this adapter, for a later locked run."""
    finding = facts.model
    if (
        finding.value
        and finding.basis is Basis.OBSERVED
        and recover.classify(finding.value) is not recover.Platform.UNKNOWN
    ):
        memory.remember(identity_for(port).key, finding.value, source)


def known_model(port: str, observed: str | None) -> tuple[str | None, str]:
    """The observed model, else the one last seen on this adapter.

    Returns the model and, when it came from memory, a sentence saying so --
    remembered identity is inferred, and the output must not hide that.
    """
    if observed:
        return observed, ""
    remembered = memory.recall(identity_for(port).key)
    if remembered is None:
        return None, ""
    return remembered.model, remembered.describe()


def _switch_cleanup(
    session: Session, bundle: Archive
) -> tuple[Archive, reset_module.FlashCleanup | None]:
    """Read what a switch reset must remove, preserving each file first.

    Only possible from a privileged prompt, where the flash listing was
    captured; otherwise the plan falls back to the fixed switch reset.
    """
    listing = next(
        (a for a in bundle.artifacts if a.name == "flash_listing" and a.content), None
    )
    if listing is None or listing.content is None:
        return bundle, None

    files = ios_switch.flash_files(listing.content)
    stale = reset_module.stale_configs(files)
    boot = capture_boot_environment(session)
    preserved = capture_files(session, stale)
    cleanup = reset_module.FlashCleanup(
        stale=stale,
        vlan_present=reset_module.VLAN_DATABASE in files,
        config_file=reset_module.nondefault_config_file(boot.content or ""),
    )
    return replace(bundle, artifacts=(*bundle.artifacts, boot, *preserved)), cleanup


#: An unconfigured device that has simply not been taken to a privileged prompt
#: yet: fresh from the box, or just reset.
_BEFORE_PRIVILEGED = (
    State.SETUP_DIALOG,
    State.AUTOINSTALL,
    State.PRESS_RETURN,
    State.USER_EXEC,
)


def reach_privileged(session: Session) -> bool:
    """Take an open, unlocked console to the privileged prompt.

    A real 2950 sat at the setup dialog after a reset, and `reset` refused it
    for not being at ``Switch#`` -- although its own plan ends by answering that
    same dialog. Nothing here changes the device: declining the dialog writes no
    configuration, and ``enable`` lasts only as long as the session.

    Returns whether anything was sent. A device that asks for a password is
    left to `recover access`: this never guesses a credential.
    """
    if session.state.state not in _BEFORE_PRIVILEGED:
        return False

    def answer(line: bytes, wanted: tuple[State, ...]) -> None:
        offset = len(session.tracker.buffer)
        session.send(line)
        try:
            session.wait_for(wanted, timeout=60.0, after_offset=offset)
        except SessionTimeoutError as exc:
            raise CommandError(
                f"could not reach a privileged prompt: {exc}", ExitCode.STATE_UNCERTAIN
            ) from exc

    locked = (State.LOGIN_USERNAME, State.LOGIN_PASSWORD, State.ENABLE_PASSWORD)
    prompt = (State.USER_EXEC, State.PRIV_EXEC, *locked)
    if session.state.state is State.SETUP_DIALOG:
        answer(b"no\r", (State.AUTOINSTALL, State.PRESS_RETURN, *prompt))
    if session.state.state is State.AUTOINSTALL:
        answer(b"yes\r", (State.PRESS_RETURN, *prompt))
    if session.state.state is State.PRESS_RETURN:
        answer(b"\r", prompt)
    if session.state.state is State.USER_EXEC:
        answer(b"enable\r", (State.PRIV_EXEC, *locked))
    session.settle()

    if session.state.state in locked:
        raise CommandError(
            "the device asks for a password before its privileged prompt. "
            "Recover access first (ciscoyoke recover access PORT --no-restore), "
            "then reset.",
            ExitCode.AUTHENTICATION_REQUIRED,
        )
    return True


# -- destructive commands --------------------------------------------------


def do_reset(
    port: str,
    *,
    baud: int = DEFAULT_BAUD,
    confirm: bool = False,
    accept_config_loss: bool = False,
    archive_to: Path | None = None,
    platform: str | None = None,
    accept_unverified: bool = False,
) -> tuple[str, ExitCode]:
    """Reset a device to a known-empty baseline.

    Archives first, always. The archive is what turns the confirmation from
    "are you sure?" into a statement of exactly what is about to be lost, and
    on a locked device it is the only preservation that will ever be possible.
    """
    blocked = interrupted_run(port)
    if blocked:
        raise CommandError(blocked, ExitCode.INTERRUPTED_RECOVERY)

    with (
        held(port, "reset"),
        device_session(port, baud) as session,
        recorded(session, "reset") as transcript,
    ):
        found = intake(session)
        if reach_privileged(session):
            # Read again: the first look was from a prompt that cannot show a
            # configuration or list flash.
            found = intake(session)
        note_identity(port, found.facts, "intake")
        bundle = archive(session, found)

        model_name, recalled = known_model(port, found.facts.model.value)
        cleanup = None
        profile = None
        if recover.path_platform(model_name, platform) is recover.Platform.SWITCH:
            profile = catalyst_profile(model_name)
            bundle, cleanup = _switch_cleanup(session, bundle)

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

        try:
            book = reset_module.for_model(
                model_name, platform=platform, cleanup=cleanup
            )
        except reset_module.PlatformUnknownError as exc:
            # A refusal, not a crash. This is the safety path: letting it
            # escape as a traceback would be the worst possible ending for the
            # one branch that exists to stop a wrong guess.
            raise CommandError(str(exc), ExitCode.STATE_UNCERTAIN) from exc

        checked = plan(book, session.transport, session.state.state)
        if not checked.safe:
            raise CommandError(
                checked.render(), ExitCode.DESTRUCTIVE_ACTION_REFUSED
            )

        rendered = "\n\n".join(
            [
                bundle.render(),
                *([f"Platform from memory: {recalled}"] if recalled else []),
                *([profile.describe()] if profile is not None else []),
                decision.render(),
                checked.render(),
                f"Session recorded to {transcript}",
            ]
        )
        gate = (
            unverified_refusal(profile, "reset")
            if profile is not None and confirm and not accept_unverified
            else None
        )
        if gate:
            raise CommandError(f"{rendered}\n\n{gate}", ExitCode.STATE_UNCERTAIN)
        if not confirm:
            return (
                f"{rendered}\n\nDry run. Nothing destructive was sent. "
                f"Re-run with --confirm to proceed.",
                ExitCode.SUCCESS,
            )

        with journal_for(port, "reset") as (_connection, journal):
            try:
                execute(checked, session, journal, confirm=True)
            except (PlanRefusedError, StepFailedError) as exc:
                journal.finish("failed")
                raise CommandError(
                    str(exc)
                    + save_failure(
                        port,
                        "reset",
                        exc,
                        session,
                        transcript,
                        profile=profile,
                        model=model_name,
                    ),
                    ExitCode.DESTRUCTIVE_ACTION_REFUSED,
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
