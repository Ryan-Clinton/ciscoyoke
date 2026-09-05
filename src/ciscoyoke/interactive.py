"""Recovery that involves a person, driven end to end.

This is where :class:`~ciscoyoke.playbook.human.HumanStep` stops being a data
structure and starts being a workflow. A Catalyst password recovery cannot
happen without someone holding the Mode button while reconnecting power, so a
command that claims to perform one has to ask, wait, and -- the part that
matters -- *verify from the console* that it actually happened.

The verification is the whole point. "Press enter when you have held the button"
advances on a claim; watching for ``switch:`` advances on a fact. When somebody
releases the button half a second early, the first design walks on into a state
it does not understand and the second one says exactly what went wrong.

Credentials, where they are needed, come from a prompt or from stdin. Never from
a command line: that lands them in shell history and the process table before
the tool has done anything at all.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from ciscoyoke.commands import (
    CommandError,
    device_session,
    held,
    interrupted_run,
    journal_for,
)
from ciscoyoke.credentials import Credentials, Secret, from_stdin, prompt
from ciscoyoke.identify.probe import intake
from ciscoyoke.lifecycle import recover
from ciscoyoke.lifecycle.archive import archive
from ciscoyoke.playbook.base import PlanRefusedError, StepFailedError, execute, plan
from ciscoyoke.playbook.human import HumanActionAbandonedError, perform
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State
from ciscoyoke.transport.serial_ import DEFAULT_BAUD

Announcer = Callable[[str], None]


def _announce(text: str) -> None:
    from ciscoyoke.ui import emit

    emit(text)


def collect_credentials(
    *, from_stdin_flag: bool, need_enable: bool = True
) -> Credentials:
    """Obtain credentials without ever letting them near a command line."""
    if from_stdin_flag:
        return from_stdin()
    if not sys.stdin.isatty():
        raise CommandError(
            "credentials are needed but stdin is not a terminal; "
            "pipe them in with --password-stdin",
            ExitCode.AUTHENTICATION_REQUIRED,
        )
    return prompt(need_enable=need_enable)


def authenticate(session: Session, credentials: Credentials) -> bool:
    """Log in from wherever the session currently is.

    Each secret goes out through the session's redacting path, so the transcript
    records that a credential was sent without recording which one.
    """
    state = session.state.state

    if state is State.LOGIN_USERNAME and credentials.username:
        session.send(credentials.username.encode("latin-1") + b"\r")
        session.settle()
        state = session.state.state

    if state is State.LOGIN_PASSWORD and credentials.password:
        session.send(credentials.password.encode() + b"\r", secret=True)
        session.settle(timeout=10.0)
        state = session.state.state

    if state is State.USER_EXEC:
        session.send(b"enable\r")
        session.settle()
        if session.state.state is State.ENABLE_PASSWORD:
            secret = credentials.enable_password or credentials.password
            if secret is None:
                return False
            session.send(secret.encode() + b"\r", secret=True)
            session.settle(timeout=10.0)

    return session.state.state is State.PRIV_EXEC


def do_recover_access_interactive(
    port: str,
    *,
    baud: int = DEFAULT_BAUD,
    confirm: bool = False,
    archive_to: Path | None = None,
    announce: Announcer = _announce,
) -> tuple[str, ExitCode]:
    """Run a full access recovery, including the physical step.

    Archives first. On a locked device the archive is necessarily partial, and
    saying so before a recovery that may destroy what could not be read is the
    entire reason the preservation model exists.
    """
    blocked = interrupted_run(port)
    if blocked:
        raise CommandError(blocked, ExitCode.INTERRUPTED_RECOVERY)

    with held(port, "recover_access"), device_session(port, baud) as session:
        found = intake(session)
        bundle = archive(session, found)
        if archive_to:
            bundle.write(archive_to)

        path = recover.path_for(found.facts.model.value, session.state.state)
        if path.platform is recover.Platform.UNKNOWN:
            raise CommandError(path.note, ExitCode.STATE_UNCERTAIN)

        preview = "\n\n".join([bundle.render(), path.render()])

        if not confirm:
            return (
                f"{preview}\n\nDry run. Nothing was sent. "
                f"Re-run with --confirm to proceed.",
                ExitCode.SUCCESS,
            )

        with journal_for(port, "recover_access") as (_connection, journal):
            try:
                if path.human_step is not None:
                    perform(path.human_step, session, announce)

                checked = plan(
                    path.playbook, session.transport, session.state.state
                )
                if not checked.safe:
                    journal.finish("refused")
                    raise CommandError(
                        checked.render(), ExitCode.DESTRUCTIVE_ACTION_REFUSED
                    )

                announce(checked.render())
                execute(checked, session, journal, confirm=True)

            except HumanActionAbandonedError as exc:
                journal.finish("abandoned")
                raise CommandError(
                    f"{preview}\n\n{exc}", ExitCode.STATE_UNCERTAIN
                ) from exc
            except (PlanRefusedError, StepFailedError) as exc:
                journal.finish("failed")
                raise CommandError(
                    f"{preview}\n\n{exc}", ExitCode.DESTRUCTIVE_ACTION_REFUSED
                ) from exc

            journal.finish("completed")

    return (
        f"{preview}\n\nAccess recovery complete. Set a new password before "
        f"reloading, or the device will lock you out again.",
        ExitCode.SUCCESS,
    )


def do_authenticated_intake(
    port: str,
    *,
    baud: int = DEFAULT_BAUD,
    password_stdin: bool = False,
) -> tuple[str, ExitCode]:
    """Identify a locked device by logging into it.

    The alternative to password recovery when the credentials are simply known
    but were not tried -- which is the common case for gear coming out of a
    known environment rather than off eBay.
    """
    with device_session(port, baud) as session:
        found = intake(session)
        if not found.locked:
            return (
                f"{port} is not asking for credentials "
                f"({found.observation.state.value}).",
                ExitCode.SUCCESS,
            )

        credentials = collect_credentials(from_stdin_flag=password_stdin)
        if not authenticate(session, credentials):
            raise CommandError(
                f"authentication did not reach a privileged prompt; "
                f"device is at {session.state.state.value}",
                ExitCode.AUTHENTICATION_REQUIRED,
            )

        identified = intake(session)
        facts = identified.facts
        return (
            "\n".join(
                [
                    "",
                    f"State:        {session.state.state.value}",
                    f"Model:        {facts.model.value or 'unknown'}",
                    f"IOS version:  {facts.ios_version.value or 'unknown'}",
                    f"Serial:       {facts.serial_number.value or 'unknown'}",
                    "",
                    "Authenticated. No configuration was changed.",
                ]
            ),
            ExitCode.SUCCESS,
        )


def redacted_echo(secret: Secret) -> str:
    """What a credential looks like anywhere it is displayed."""
    return str(secret)
