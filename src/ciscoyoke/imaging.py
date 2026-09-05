"""The ``recover image`` command: getting a supplied image onto a dead device.

Everything checkable is checked before a byte is sent -- fingerprint,
compatibility, flash inventory, the stranding rule -- because the alternative is
discovering the problem an hour into a transfer on a device whose only image was
just deleted.

The console speed increase is journalled rather than treated as an
implementation detail. An interrupted transfer leaves the device at 115200, and
the journal is the only thing that knows to look for it there.
"""

from __future__ import annotations

from pathlib import Path

from ciscoyoke.commands import (
    CommandError,
    device_session,
    held,
    interrupted_run,
    journal_for,
)
from ciscoyoke.identify.probe import intake
from ciscoyoke.image.compat import assess
from ciscoyoke.image.fingerprint import ImageUnreadableError, fingerprint
from ciscoyoke.image.plan import (
    FlashInventory,
    Strategy,
    make_plan,
    override_stranded,
    parse_flash,
)
from ciscoyoke.playbook import xmodem
from ciscoyoke.playbook.transfer import Method, Progress, TransferStep
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State
from ciscoyoke.transport.serial_ import DEFAULT_BAUD


def read_flash(session: Session, state: State) -> FlashInventory:
    """List flash from wherever the device currently is.

    The bootloader needs flash initialising first; a booted device does not.
    Getting this wrong yields an empty inventory, which the planner would read
    as "no images present" and then act on -- so the state drives the command.
    """
    before = len(session.tracker.buffer)

    if state is State.BOOTLOADER:
        session.send(b"flash_init\r")
        session.settle(timeout=60.0)
        session.send(b"dir flash:\r")
    elif state is State.ROMMON:
        session.send(b"dir flash:\r")
    elif state is State.PRIV_EXEC:
        session.send(b"show flash:\r")
    else:
        return FlashInventory()

    session.settle(timeout=30.0)
    session.drain_pager()
    return parse_flash(session.tracker.buffer.text[before:])


def do_recover_image(
    port: str,
    image_path: Path,
    *,
    baud: int = DEFAULT_BAUD,
    confirm: bool = False,
    accept_stranded_risk: bool = False,
    via: str = "xmodem",
    on_progress: object = None,
) -> tuple[str, ExitCode]:
    """Plan and, with confirmation, perform an image rescue."""
    blocked = interrupted_run(port)
    if blocked:
        raise CommandError(blocked, ExitCode.INTERRUPTED_RECOVERY)

    try:
        image = fingerprint(image_path)
    except ImageUnreadableError as exc:
        raise CommandError(str(exc), ExitCode.DEVICE_NOT_FOUND) from exc

    with held(port, "recover_image"), device_session(port, baud) as session:
        found = intake(session)
        inventory = read_flash(session, found.observation.state)
        compatibility = assess(
            image, found.facts, flash_free_bytes=inventory.free_bytes
        )
        plan = make_plan(image, inventory, compatibility)

        if plan.strategy is Strategy.BLOCKED_WOULD_STRAND:
            if not accept_stranded_risk:
                raise CommandError(
                    f"{compatibility.render()}\n\n{plan.render()}",
                    ExitCode.DESTRUCTIVE_ACTION_REFUSED,
                )
            plan = override_stranded(plan)

        if plan.blocked:
            raise CommandError(
                f"{compatibility.render()}\n\n{plan.render()}",
                ExitCode.DESTRUCTIVE_ACTION_REFUSED,
            )

        method = Method.TFTP if via == "tftp" else Method.XMODEM
        step = TransferStep(
            method=method,
            size_bytes=image.size,
            filename=image.name,
            from_baud=baud,
        )
        rendered = "\n\n".join(
            [compatibility.render(), plan.render(), step.render()]
        )

        if plan.strategy is Strategy.ALREADY_PRESENT:
            return (
                f"{rendered}\n\nNo transfer needed: boot the existing image.",
                ExitCode.SUCCESS,
            )

        if not confirm:
            return (
                f"{rendered}\n\nDry run. Nothing was sent. "
                f"Re-run with --confirm to proceed.",
                ExitCode.SUCCESS,
            )

        if method is Method.TFTP:
            # The ROMMON variables need addressing this command does not
            # collect. Saying so beats a half-configured ROMMON.
            raise CommandError(
                f"{rendered}\n\nTFTP delivery needs ROMMON addressing "
                f"(IP_ADDRESS, IP_SUBNET_MASK, TFTP_SERVER, TFTP_FILE) that "
                f"this command does not collect yet. Use --via xmodem.",
                ExitCode.TRANSPORT_CAPABILITY_UNAVAILABLE,
            )

        return _transfer(session, port, image_path, step, rendered)


def _transfer(
    session: Session,
    port: str,
    image_path: Path,
    step: TransferStep,
    rendered: str,
) -> tuple[str, ExitCode]:
    """Raise the console speed, transfer, and always put it back.

    The compensation runs in a ``finally``: a failed transfer that left the
    console at 115200 would make the device look dead to the next person who
    connects at 9600.
    """
    with journal_for(port, "recover_image") as (_connection, journal):
        raised = None
        transport = session.transport
        setter = getattr(transport, "set_baud", None)
        mutations = step.mutations()

        if mutations and setter is None:
            journal.finish("failed")
            raise CommandError(
                "this transport cannot change line speed, and XMODEM at 9600 "
                "would take hours. Use a local serial port or rfc2217://.",
                ExitCode.TRANSPORT_CAPABILITY_UNAVAILABLE,
            )

        try:
            if mutations and setter is not None:
                with journal.attempting(mutations[0]) as attempted:
                    setter(step.to_baud)
                    reset_input = getattr(transport, "reset_input", None)
                    if reset_input is not None:
                        reset_input()
                journal.observed(attempted)
                raised = attempted

            session.send(b"copy xmodem: flash:\r")
            session.settle(timeout=30.0)

            progress = Progress(total_bytes=step.size_bytes)
            outcome = xmodem.send_file(
                image_path,
                transport.read,
                transport.write,
                progress=lambda sent, _total: setattr(progress, "sent_bytes", sent),
            )
        except xmodem.XmodemError as exc:
            journal.finish("failed")
            raise CommandError(
                f"{rendered}\n\nTransfer failed: {exc}", ExitCode.TRANSFER_FAILED
            ) from exc
        finally:
            if raised is not None and setter is not None:
                setter(step.from_baud)
                journal.compensated(raised)

        journal.finish("completed")

    return (
        f"{rendered}\n\nTransfer complete.\n{outcome.render()}\n\n"
        f"Boot the image and check the running version before relying on it: "
        f"a completed transfer proves the bytes arrived, not that they boot.",
        ExitCode.SUCCESS,
    )
