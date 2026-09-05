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
from ciscoyoke.session import Session
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


def _confirm_erase(session: Session) -> None:
    """Answer the confirmation `write erase` asks for."""
    session.send(b"\r")


def router_reset() -> Playbook:
    """Erase a router's startup configuration and reload."""
    return Playbook(
        name="reset_router",
        description="Erase startup-config and reload to a known-empty baseline",
        steps=(
            Step(
                name="erase_startup_config",
                description="write erase (destroys startup-config)",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"write erase\r"),
                effect=Effect.DESTRUCTIVE,
                mutation=model.write_erase(guard=ACCEPT_CONFIG_LOSS),
                timeout=30.0,
            ),
            Step(
                name="confirm_erase",
                description="confirm the erase prompt",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=_confirm_erase,
                timeout=30.0,
            ),
            Step(
                name="reload",
                description="reload and wait for the device to come back",
                require_state=(State.PRIV_EXEC,),
                expect=(State.SETUP_DIALOG, State.PRESS_RETURN, State.USER_EXEC),
                action=send_line(b"reload\r\r"),
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
        ),
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
            Step(
                name="erase_startup_config",
                description="write erase (destroys startup-config)",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"write erase\r"),
                effect=Effect.DESTRUCTIVE,
                mutation=model.write_erase(guard=ACCEPT_CONFIG_LOSS),
                timeout=30.0,
            ),
            Step(
                name="confirm_erase",
                description="confirm the erase prompt",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=_confirm_erase,
                timeout=30.0,
            ),
            Step(
                name="delete_vlan_database",
                description="delete flash:vlan.dat (destroys the VLAN database)",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"delete flash:vlan.dat\r\r\r"),
                effect=Effect.DESTRUCTIVE,
                mutation=model.delete_flash_file(
                    "flash:vlan.dat", guard=ACCEPT_CONFIG_LOSS
                ),
                timeout=30.0,
            ),
            Step(
                name="reload",
                description="reload and wait for the device to come back",
                require_state=(State.PRIV_EXEC,),
                expect=(State.SETUP_DIALOG, State.PRESS_RETURN, State.USER_EXEC),
                action=send_line(b"reload\r\r"),
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
        ),
    )


def for_model(model_name: str | None) -> Playbook:
    """Pick the right reset for a platform.

    Defaults to the switch playbook when the model is unknown: deleting
    `vlan.dat` on a router is harmless -- the file does not exist and the delete
    is refused -- while *skipping* it on a switch leaves the device dirty in a
    way that surfaces much later. The safer default is the one whose failure
    mode is a no-op.
    """
    if model_name and model_name.upper().startswith(("CISCO1", "CISCO2", "CISCO3")):
        return router_reset()
    return switch_reset()
