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

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ciscoyoke.commands import (
    CommandError,
    device_session,
    held,
    interrupted_run,
    journal_for,
)
from ciscoyoke.identify.probe import intake
from ciscoyoke.image.bootproof import prove
from ciscoyoke.image.compat import assess
from ciscoyoke.image.drivers import (
    ImageRecoveryDriver,
    NoDriverError,
    driver_for,
)
from ciscoyoke.image.fingerprint import (
    Fingerprint,
    ImageUnreadableError,
    fingerprint,
)
from ciscoyoke.image.plan import (
    FlashInventory,
    Strategy,
    make_plan,
    override_stranded,
)
from ciscoyoke.playbook import xmodem
from ciscoyoke.playbook.transfer import Method, Progress, TransferStep
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State
from ciscoyoke.transport.serial_ import DEFAULT_BAUD

#: Called with live transfer progress so a long transfer is never silent.
ProgressReporter = Callable[[Progress], None]

#: Structured detail from the last recovery, for the --json result. The
#: human string is a rendering of this, not the other way round -- so the
#: machine interface is not left with only {"confirmed": true}.
_LAST_RESULT: dict[str, object] = {}


def last_result() -> dict[str, object]:
    """Structured detail from the most recent recovery in this process."""
    return dict(_LAST_RESULT)


def read_flash(session: Session, driver: ImageRecoveryDriver) -> FlashInventory:
    """List flash using the commands this platform actually has.

    Both the command sequence and the output shape are platform-specific: a
    Catalyst needs flash mounting first and prints an indexed directory, while
    ROMMON prints size/checksum/name. Parsing one with the other's parser
    yields an empty inventory, which the planner would read as "no images
    present" -- so the driver owns both halves.
    """
    before = len(session.tracker.buffer)

    for command in driver.inventory_command():
        session.send(command)
        session.settle(timeout=60.0)

    session.drain_pager()
    return driver.parse_inventory(session.tracker.buffer.text[before:])


def do_recover_image(
    port: str,
    image_path: Path,
    *,
    baud: int = DEFAULT_BAUD,
    confirm: bool = False,
    accept_stranded_risk: bool = False,
    via: str = "xmodem",
    on_progress: ProgressReporter | None = None,
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

        try:
            driver = driver_for(found.observation.state, found.facts.model.value)
        except NoDriverError as exc:
            raise CommandError(str(exc), ExitCode.STATE_UNCERTAIN) from exc

        inventory = read_flash(session, driver)
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

        if plan.delete:
            # The planner works out what to delete; nothing executes it. Rather
            # than start a transfer into flash that was never freed -- and run
            # out of room an hour in -- the branch refuses and says so.
            raise CommandError(
                f"{compatibility.render()}\n\n{plan.render()}\n\n"
                f"BLOCKED\n\n"
                f"Making room requires deleting flash contents, and automated "
                f"deletion is not hardware-verified yet. No files were deleted "
                f"and no transfer was started.\n\n"
                f"Delete the file yourself from the device, then re-run.",
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
        _LAST_RESULT.clear()
        _LAST_RESULT.update(
            {
                "image": image.to_json(),
                "compatibility": compatibility.to_json(),
                "strategy": plan.strategy.value,
                "platform": driver.family.value,
                "flash_free_bytes": inventory.free_bytes,
                "estimated_seconds": round(step.estimate().seconds, 1),
            }
        )

        if plan.strategy is Strategy.ALREADY_PRESENT:
            # Matched on filename and size, not on content -- so this is a
            # candidate, not a confirmed identical image. Without --confirm the
            # honest answer is "probably already there", and with it the device
            # is booted and proven like any other rescue.
            if not confirm:
                return (
                    f"{rendered}\n\nA candidate image of this name and size is "
                    f"already in flash. Content identity is unknown -- the match "
                    f"is filename and size only.\n\nRe-run with --confirm to "
                    f"boot it and prove it runs.",
                    ExitCode.SUCCESS,
                )
            boot = prove(
                session,
                image,
                console_restored=True,
                boot_command=driver.boot_command(step.filename),
            )
            return (
                f"{rendered}\n\nNo transfer needed.\n\n{boot.render()}",
                ExitCode.SUCCESS if boot.proven else ExitCode.STATE_UNCERTAIN,
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

        return _transfer(
            session, port, image_path, step, rendered, image, driver, on_progress
        )


def raise_console_speed(
    session: Session, step: TransferStep, driver: ImageRecoveryDriver
) -> None:
    """Change the line speed on the device *first*, then on the host.

    The ordering is not arbitrary and getting it wrong is silent. Cisco's
    procedure is ``switch: set BAUD 115200``, after which -- in their words --
    "the screen goes blank", because the device is now talking at a rate the
    host is not listening at. The host follows.

    An earlier version of this code changed the host first, which left the
    device at 9600 receiving 115200 and every subsequent command arriving as
    line noise. The symptom is a device that appears to have hung, which is
    exactly the wrong thing to make someone believe about a box they are trying
    to rescue.
    """
    command = driver.set_baud_command(step.to_baud)
    if command is None:
        # This platform has no standalone baud command -- ROMMON carries the
        # rate as a flag on the transfer itself. Sending a Catalyst command
        # here was the original bug.
        return
    session.send(command)
    # No settle: the reply, if any, arrives at the new speed and is unreadable
    # at the old one. The reacquisition below is what confirms the change.

    setter = session.transport.set_baud  # type: ignore[attr-defined]
    setter(step.to_baud)
    reset_input = getattr(session.transport, "reset_input", None)
    if reset_input is not None:
        # Bytes captured at the previous rate are noise at this one, and
        # feeding them to the tracker would look like a device talking
        # gibberish rather than a buffer that needs emptying.
        reset_input()


@dataclass(frozen=True, slots=True)
class RestorationProof:
    """Whether the console really came back, and on what evidence."""

    target_baud: int
    bytes_observed_after_change: bool
    prompt_observed: bool
    state: State

    @property
    def restored(self) -> bool:
        """Both conditions, or it is not restored.

        Bytes alone are not enough -- a wrong baud produces bytes too, just not
        meaningful ones. A recognised prompt is what distinguishes a device
        talking to us from a device talking past us.
        """
        return self.bytes_observed_after_change and self.prompt_observed

    def render(self) -> str:
        marks = {True: "✓", False: "✗"}
        return "\n".join(
            [
                f"  {marks[self.bytes_observed_after_change]} new bytes at "
                f"{self.target_baud}",
                f"  {marks[self.prompt_observed]} prompt recognised "
                f"({self.state.value})",
            ]
        )


def restore_console_speed(
    session: Session, step: TransferStep, driver: ImageRecoveryDriver
) -> RestorationProof:
    """Put both ends back, device first again, and *prove* it took.

    The device is returned to its default with ``unset BAUD`` -- Cisco's
    documented way back, and better than naming a rate because it restores the
    platform's actual default rather than this code's assumption about it.

    The proof matters more than it looks. The previous version returned
    ``bool(session.tracker.buffer)``, and that buffer accumulates everything
    received during the whole session: after a flash inventory and an XMODEM
    setup it is never empty, so restoration was reported as verified whether or
    not the device said a single word. The journal then recorded the mutation
    as compensated on no evidence at all -- a safety-model violation in the one
    place the model exists to be trusted.

    So: mark the stream position, change both ends, nudge, and require *new*
    bytes past that mark plus a prompt we recognise.
    """
    command = driver.restore_baud_command()
    if command is not None:
        session.send(command)

    setter = getattr(session.transport, "set_baud", None)
    if setter is None:
        return RestorationProof(step.from_baud, False, False, session.state.state)

    setter(step.from_baud)
    reset_input = getattr(session.transport, "reset_input", None)
    if reset_input is not None:
        reset_input()

    # Everything before this point is old news and cannot count as evidence
    # that the device is talking at the restored rate.
    mark = len(session.tracker.buffer)

    session.send(b"\r")
    session.settle(timeout=15.0)

    fresh = len(session.tracker.buffer) > mark
    state = session.state.state
    return RestorationProof(
        target_baud=step.from_baud,
        bytes_observed_after_change=fresh,
        prompt_observed=state in _RECOGNISED_AFTER_RESTORE,
        state=state,
    )


#: States that mean the device is talking to us intelligibly again. A wrong
#: baud yields bytes but no recognisable prompt, which is exactly the case the
#: previous check could not distinguish.
_RECOGNISED_AFTER_RESTORE = (
    State.BOOTLOADER,
    State.ROMMON,
    State.PRIV_EXEC,
    State.USER_EXEC,
    State.PRESS_RETURN,
    State.SETUP_DIALOG,
    State.CONFIRM,
)


def _transfer(
    session: Session,
    port: str,
    image_path: Path,
    step: TransferStep,
    rendered: str,
    image: Fingerprint,
    driver: ImageRecoveryDriver,
    on_progress: ProgressReporter | None = None,
) -> tuple[str, ExitCode]:
    """Raise the console speed, transfer, prove the boot, and put it all back.

    The restoration runs in a ``finally``: a failed transfer that left the
    console at 115200 would make the device look dead to the next person who
    connects at 9600 -- and they would have no way of knowing why.
    """
    with journal_for(port, "recover_image") as (_connection, journal):
        raised = None
        restored = False
        restoration_note = ""
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
            if mutations and driver.can_change_baud:
                with journal.attempting(mutations[0]) as attempted:
                    raise_console_speed(session, step, driver)
                journal.observed(attempted)
                raised = attempted

            # The destination filename is required. `copy xmodem: flash:` with
            # no name is rejected by the bootloader, which is the sort of thing
            # only a real device tells you.
            session.send(driver.transfer_command(step.filename, step.to_baud))
            session.settle(timeout=30.0)

            # A real clock, or the rate and ETA machinery has no elapsed
            # time to work with and the display sits on "estimating..." for
            # the entire transfer -- which is the exact experience the
            # progress reporting exists to prevent.
            started = time.monotonic()
            progress = Progress(
                total_bytes=step.size_bytes, started_at=started, now=started
            )

            def report(sent: int, total: int) -> None:
                progress.sent_bytes = sent
                progress.total_bytes = total
                progress.now = time.monotonic()
                if on_progress is not None:
                    on_progress(progress)

            outcome = xmodem.send_file(
                image_path, transport.read, transport.write, progress=report
            )
        except xmodem.XmodemError as exc:
            journal.finish("failed")
            raise CommandError(
                f"{rendered}\n\nTransfer failed: {exc}", ExitCode.TRANSFER_FAILED
            ) from exc
        finally:
            if raised is not None:
                proof = restore_console_speed(session, step, driver)
                restored = proof.restored
                if restored:
                    # Only an observed restoration may be recorded as
                    # compensated. Claiming otherwise would let the journal
                    # assert a rollback that never happened.
                    journal.compensated(raised)
                else:
                    restoration_note = (
                        "\n\nCONSOLE NOT RESTORED\n"
                        + proof.render()
                        + f"\n  The device may still be at {step.to_baud}. "
                        f"Reconnect at that rate, or run: "
                        f"ciscoyoke resolve {port} --rollback"
                    )

        boot = prove(
            session,
            image,
            console_restored=restored or not mutations,
            # Boot the image just transferred, by name. A bare `boot` leaves
            # the choice to stale boot variables and bootloader defaults,
            # which after two hours of transferring one specific file is the
            # wrong thing to leave to chance.
            boot_command=driver.boot_command(step.filename),
        )
        journal.finish("completed" if boot.proven else "transferred_unverified")

    code = ExitCode.SUCCESS if boot.proven else ExitCode.STATE_UNCERTAIN
    _LAST_RESULT.update(
        {
            "transfer": {
                "method": step.method.value,
                "bytes": outcome.sent_bytes,
                "blocks": outcome.blocks_sent,
                "retries": outcome.retries,
                "crc": outcome.used_crc,
                "seconds": round(outcome.elapsed, 1),
            },
            "console_restored": restored,
            "boot_proof": boot.to_json(),
        }
    )
    return (
        f"{rendered}\n\nTransfer complete.\n{outcome.render()}"
        f"{restoration_note}\n\n{boot.render()}",
        code,
    )
