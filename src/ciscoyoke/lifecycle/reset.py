"""Reset a device to a known-empty lab baseline.

The first genuinely destructive operation, and the one every safety mechanism
built so far exists to protect.

What reset **is**: erasing the startup configuration and VLAN database so the
device boots as though it came out of a box, ready to learn on.

What reset is **not**: sanitisation. It makes no claim about residual data in
flash, NVRAM, or anywhere it does not know about. Media sanitisation has a real
standard and this tool does not clear it, so it does not imply that it does --
see the specification's §10.2. Conflating the two would let someone hand on a
device believing it clean.

Erasing a VLAN database matters more than it looks on a switch: `write erase`
alone leaves `vlan.dat` in flash, so a switch that appears reset comes back with
the previous owner's VLANs and quietly breaks the next lab.
"""

from __future__ import annotations

from dataclasses import dataclass

from ciscoyoke.journal import model
from ciscoyoke.lifecycle.archive import Archive, PreservationStatus
from ciscoyoke.playbook.base import (
    Effect,
    Playbook,
    Step,
    send_line,
)
from ciscoyoke.stream.tracker import State

#: The named guard that authorises destroying a configuration.
ACCEPT_CONFIG_LOSS = "accept_config_loss"


class ArchiveRequiredError(RuntimeError):
    """Refused: nothing was preserved before a destructive operation."""


@dataclass(frozen=True, slots=True)
class ResetDecision:
    """Whether a reset may proceed, and what it would cost."""

    allowed: bool
    reason: str
    at_risk: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [self.reason]
        if self.at_risk:
            lines += [
                "",
                "Never captured, and about to be destroyed:",
                *(f"  - {name}" for name in self.at_risk),
            ]
        return "\n".join(lines)


def decide(archive: Archive | None, *, accept_config_loss: bool = False) -> ResetDecision:
    """Gate a reset on what preservation actually achieved.

    A `PARTIAL` archive does not block: on a locked device it is the *only*
    possible outcome, and refusing would make the tool useless for its main
    case. What it does is force the cost into the confirmation, so the operator
    is told what was never read before they destroy it.

    No archive at all does block, unless the loss is accepted explicitly.
    """
    if archive is None:
        if not accept_config_loss:
            return ResetDecision(
                allowed=False,
                reason=(
                    "refusing to reset a device that has not been archived; "
                    "run archive first, or accept the loss explicitly"
                ),
            )
        return ResetDecision(
            allowed=True,
            reason="proceeding without an archive: loss accepted explicitly",
        )

    at_risk = tuple(artifact.name for artifact in archive.at_risk)

    if archive.status is PreservationStatus.NONE:
        if not accept_config_loss:
            return ResetDecision(
                allowed=False,
                reason=(
                    "archive captured nothing; resetting now would destroy a "
                    "configuration that was never read"
                ),
                at_risk=at_risk,
            )
        return ResetDecision(
            allowed=True,
            reason="archive captured nothing; loss accepted explicitly",
            at_risk=at_risk,
        )

    if archive.status is PreservationStatus.PARTIAL:
        return ResetDecision(
            allowed=True,
            reason=(
                "archive is PARTIAL: some artifacts could not be read and will "
                "be destroyed"
            ),
            at_risk=at_risk,
        )

    return ResetDecision(allowed=True, reason="archive is complete")


def _erase_steps() -> tuple[Step, ...]:
    """`write erase`, and the confirmation IOS actually asks for.

    The previous shape of this was a deadlock. It sent ``write erase`` and
    waited for ``PRIV_EXEC``, with the confirmation as a *later* step -- but
    the device answers "Erasing the nvram filesystem will remove all files!
    Continue? [confirm]" and will not return to the prompt until that is
    answered. So the tool waited for a prompt the device was waiting to be
    released to produce, and both sides sat there until the timeout.

    Modelling the confirmation as a state the playbook expects and then leaves
    is the fix, and it is why ``CONFIRM`` exists as a first-class state rather
    than as a stray carriage return sent hopefully.
    """
    return (
        Step(
            name="erase_startup_config",
            description="write erase (destroys startup-config)",
            require_state=(State.PRIV_EXEC,),
            expect=(State.CONFIRM,),
            action=send_line(b"write erase\r"),
            effect=Effect.DESTRUCTIVE,
            mutation=model.write_erase(guard=ACCEPT_CONFIG_LOSS),
            timeout=30.0,
        ),
        Step(
            name="confirm_erase",
            description="answer [confirm]",
            require_state=(State.CONFIRM,),
            expect=(State.PRIV_EXEC,),
            action=send_line(b"\r"),
            timeout=60.0,
        ),
    )


def _reload_steps() -> tuple[Step, ...]:
    """`reload`, its save question, and its confirmation.

    Two prompts, in order: "System configuration has been modified. Save?
    [yes/no]:" -- answered *no*, because saving would defeat the erase that
    just happened -- and then "Proceed with reload? [confirm]".

    The save prompt only appears when the running configuration differs from
    the startup one, which after an erase it does. Both are accepted as valid
    next states so the playbook works either way rather than depending on that.
    """
    return (
        Step(
            name="reload",
            description="reload the device",
            require_state=(State.PRIV_EXEC,),
            expect=(State.SAVE_CONFIG_PROMPT, State.CONFIRM),
            action=send_line(b"reload\r"),
            timeout=60.0,
        ),
        Step(
            name="decline_save",
            description="decline saving (it would undo the erase)",
            require_state=(State.SAVE_CONFIG_PROMPT, State.CONFIRM),
            expect=(State.CONFIRM, State.BOOTING),
            action=send_line(b"no\r"),
            timeout=60.0,
        ),
        Step(
            name="confirm_reload",
            description="answer [confirm] and wait for the device to come back",
            require_state=(State.CONFIRM, State.BOOTING),
            expect=(State.SETUP_DIALOG, State.PRESS_RETURN, State.USER_EXEC),
            action=send_line(b"\r"),
            timeout=300.0,
        ),
        Step(
            name="decline_setup_dialog",
            description="decline the initial configuration dialog",
            require_state=(State.SETUP_DIALOG, State.PRESS_RETURN, State.USER_EXEC),
            expect=(State.PRESS_RETURN, State.USER_EXEC),
            action=send_line(b"no\r"),
            timeout=60.0,
        ),
    )


def router_reset() -> Playbook:
    """Erase a router's startup configuration and reload."""
    return Playbook(
        name="reset_router",
        description="Erase startup-config and reload to a known-empty baseline",
        steps=(*_erase_steps(), *_reload_steps()),
    )


def switch_reset() -> Playbook:
    """Erase a switch's startup configuration *and* its VLAN database.

    The `vlan.dat` deletion is the step people forget. Without it a switch that
    looks reset comes back carrying the previous owner's VLANs, and the next lab
    fails for reasons that have nothing to do with the lab.
    """
    return Playbook(
        name="reset_switch",
        description="Erase startup-config and vlan.dat, then reload",
        steps=(
            *_erase_steps(),
            # `delete flash:vlan.dat` is a three-part exchange, not one line:
            # IOS echoes "Delete filename [vlan.dat]?", then
            # "Delete flash:vlan.dat? [confirm]". Blasting three carriage
            # returns at it happened to work only if every prompt appeared in
            # the order and timing assumed, and left no way to notice when it
            # did not.
            Step(
                name="delete_vlan_database",
                description="delete flash:vlan.dat (destroys the VLAN database)",
                require_state=(State.PRIV_EXEC,),
                expect=(State.FILENAME_PROMPT, State.CONFIRM),
                action=send_line(b"delete flash:vlan.dat\r"),
                effect=Effect.DESTRUCTIVE,
                mutation=model.delete_flash_file(
                    "flash:vlan.dat", guard=ACCEPT_CONFIG_LOSS
                ),
                timeout=30.0,
            ),
            Step(
                name="accept_vlan_filename",
                description="accept the default filename",
                require_state=(State.FILENAME_PROMPT, State.CONFIRM),
                expect=(State.CONFIRM, State.PRIV_EXEC),
                action=send_line(b"\r"),
                timeout=30.0,
            ),
            Step(
                name="confirm_vlan_delete",
                description="answer [confirm]",
                require_state=(State.CONFIRM, State.PRIV_EXEC),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"\r"),
                timeout=30.0,
            ),
            *_reload_steps(),
        ),
    )


class PlatformUnknownError(RuntimeError):
    """The platform could not be identified, so no reset will be guessed at."""


def for_model(model_name: str | None, *, platform: str | None = None) -> Playbook:
    """Pick the right reset, or refuse.

    An earlier version defaulted to the switch playbook on an unknown model,
    reasoning that deleting a nonexistent ``vlan.dat`` on a router is a no-op.
    That is true, and it was still the wrong call: it guesses on the one path
    in this project that cannot be undone.

    Worse, the failure is not symmetric. If the guess is wrong and a
    switch-specific step fails, it fails *after* ``write erase`` has already
    destroyed the configuration -- so the cost of guessing is paid before the
    guess is discovered to be wrong.

    ``platform`` lets the operator supply the evidence the device would not,
    which is a different thing from the tool inventing it.
    """
    if platform:
        chosen = platform.strip().lower()
        if chosen in ("router", "ios", "rommon"):
            return router_reset()
        if chosen in ("switch", "catalyst"):
            return switch_reset()
        raise PlatformUnknownError(
            f"unknown platform {platform!r}; expected 'router' or 'catalyst'"
        )

    if not model_name:
        raise PlatformUnknownError(
            "the device did not identify itself, and reset is irreversible. "
            "Identify it first (ciscoyoke intake), or state the platform "
            "explicitly with --platform router|catalyst -- which supplies the "
            "evidence rather than guessing at it."
        )

    upper = model_name.upper()
    if upper.startswith(("CISCO1", "CISCO2", "CISCO3", "C1700", "C2600", "C2800")):
        return router_reset()
    if upper.startswith(("WS-C", "CAT")):
        return switch_reset()

    raise PlatformUnknownError(
        f"{model_name} is not a platform with a verified reset procedure. "
        f"State it explicitly with --platform router|catalyst if you are sure."
    )
