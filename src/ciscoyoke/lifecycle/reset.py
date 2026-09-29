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
the previous owner's VLANs and quietly breaks the next lab. The same goes for
configurations left in flash under other names -- a real 2950 arrived with a
`config.old` nobody had mentioned -- so those are read into the archive and
deleted too, and every deletion is proven from a fresh listing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ciscoyoke.journal import model
from ciscoyoke.lifecycle.archive import Archive, PreservationStatus
from ciscoyoke.platform_profiles import CATALYST_2950, CatalystProfile, catalyst_profile
from ciscoyoke.playbook.base import (
    Effect,
    Playbook,
    Step,
    send_line,
)
from ciscoyoke.playbook.ios_switch import after_boot_steps, flash_files
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


def _reload_steps(boot_timeout: float = 300.0) -> tuple[Step, ...]:
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
            # Only the save question. An earlier version accepted CONFIRM here
            # too and answered it with "no" -- but "Proceed with reload?
            # [confirm]" is a different transition that wants a bare return,
            # and sending "no" to it is at best ignored and at worst read as
            # cancelling the reload.
            #
            # This is the limit of `tuple[allowed states]`: it works only where
            # the same action is right for every one of them.
            require_state=(State.SAVE_CONFIG_PROMPT,),
            expect=(State.CONFIRM, State.BOOTING),
            action=send_line(b"no\r"),
            timeout=60.0,
            optional=True,
        ),
        Step(
            name="confirm_reload",
            description="answer [confirm] and wait for the device to come back",
            require_state=(State.CONFIRM, State.BOOTING),
            expect=(
                State.SETUP_DIALOG,
                State.AUTOINSTALL,
                State.PRESS_RETURN,
                State.USER_EXEC,
            ),
            action=send_line(b"\r"),
            timeout=boot_timeout,
        ),
        # The setup dialog, then on IOS 15 the autoinstall question; each only
        # if it is actually asked.
        *after_boot_steps(),
    )


def router_reset() -> Playbook:
    """Erase a router's startup configuration and reload."""
    return Playbook(
        name="reset_router",
        description="Erase startup-config and reload to a known-empty baseline",
        steps=(*_erase_steps(), *_reload_steps()),
    )


VLAN_DATABASE = "vlan.dat"

# Files `write erase` itself removes on a Catalyst: NVRAM is flash here.
_NVRAM_FILES = frozenset({"config.text", "private-config.text"})

# A previous owner's configuration left in flash under another name. Seen on a
# real 2950: `config.old` from whoever set it up, which `write erase` leaves
# alone and a reset that claims "clean" must not.
_STALE_CONFIG = re.compile(
    r"(?:private-)?config\..+|.+\.(?:cfg|conf|bak|old|ciscoyoke)",
    re.IGNORECASE,
)

_CONFIG_FILE_LINE = re.compile(r"^\s*Config file\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def stale_configs(files: frozenset[str]) -> tuple[str, ...]:
    """Configuration files in flash that `write erase` would leave behind."""
    return tuple(
        sorted(
            name
            for name in files
            if _STALE_CONFIG.fullmatch(name) and name not in _NVRAM_FILES
        )
    )


def nondefault_config_file(show_boot: str) -> str | None:
    """The ``boot config-file`` from ``show boot``, if it is not the default."""
    match = _CONFIG_FILE_LINE.search(show_boot)
    if not match:
        return None
    value = match.group(1)
    if value.split(":", 1)[-1].lstrip("/") == "config.text":
        return None
    return value


@dataclass(frozen=True, slots=True)
class FlashCleanup:
    """What a switch reset removes beyond the startup configuration.

    Read from the device before the plan is built -- the flash listing and
    ``show boot`` -- so the plan names every file it will delete.
    """

    stale: tuple[str, ...] = ()
    vlan_present: bool = True
    config_file: str | None = None

    @property
    def deleted(self) -> tuple[str, ...]:
        return ((VLAN_DATABASE,) if self.vlan_present else ()) + self.stale


def _delete_steps(
    name: str, what: str, *, tag: str | None = None, step: str | None = None
) -> tuple[Step, ...]:
    """`delete flash:<name>`: a three-part exchange, not one line.

    IOS asks "Delete filename [<name>]?", then "Delete flash:<name>? [confirm]".
    Blasting three carriage returns at it happened to work only if every prompt
    appeared in the order and timing assumed, and left no way to notice when it
    did not.
    """
    return (
        Step(
            name=step or f"delete_{name}",
            description=f"delete flash:{name} ({what})",
            require_state=(State.PRIV_EXEC,),
            expect=(State.FILENAME_PROMPT, State.CONFIRM),
            action=send_line(f"delete flash:{name}\r".encode()),
            effect=Effect.DESTRUCTIVE,
            mutation=model.delete_flash_file(f"flash:{name}", guard=ACCEPT_CONFIG_LOSS),
            timeout=30.0,
        ),
        Step(
            name=f"accept_{tag or name}_filename",
            description="accept the default filename",
            require_state=(State.FILENAME_PROMPT, State.CONFIRM),
            expect=(State.CONFIRM, State.PRIV_EXEC),
            action=send_line(b"\r"),
            timeout=30.0,
        ),
        Step(
            name=f"confirm_{tag or name}_delete",
            description="answer [confirm]",
            require_state=(State.CONFIRM, State.PRIV_EXEC),
            expect=(State.PRIV_EXEC,),
            action=send_line(b"\r"),
            timeout=30.0,
        ),
    )


def _clear_config_file_steps(current: str) -> tuple[Step, ...]:
    return (
        Step(
            name="enter_config_mode",
            description="configure terminal",
            require_state=(State.PRIV_EXEC,),
            expect=(State.CONFIG_MODE,),
            action=send_line(b"configure terminal\r"),
            timeout=30.0,
        ),
        Step(
            name="clear_boot_config_file",
            description=f"no boot config-file (was {current})",
            require_state=(State.CONFIG_MODE,),
            expect=(State.CONFIG_MODE,),
            action=send_line(b"no boot config-file\r"),
            effect=Effect.DESTRUCTIVE,
            mutation=model.boot_config_file(current),
            timeout=30.0,
        ),
        Step(
            name="leave_config_mode",
            description="end",
            require_state=(State.CONFIG_MODE,),
            expect=(State.PRIV_EXEC,),
            action=send_line(b"end\r"),
            timeout=30.0,
        ),
    )


def _all_gone(names: tuple[str, ...]) -> Callable[[str], str | None]:
    def check(output: str) -> str | None:
        remaining = sorted(set(names) & flash_files(output))
        if remaining:
            return f"still in flash after deletion: {', '.join(remaining)}"
        return None

    return check


def switch_reset(
    cleanup: FlashCleanup | None = None, profile: CatalystProfile = CATALYST_2950
) -> Playbook:
    """Erase a switch's startup configuration, VLAN database and stale configs.

    The `vlan.dat` deletion is the step people forget. Without it a switch that
    looks reset comes back carrying the previous owner's VLANs, and the next lab
    fails for reasons that have nothing to do with the lab. Stale configuration
    files are the second thing: invisible until someone renames one back.

    Every deletion is then proven from a fresh listing before the reload, rather
    than inferred from having reached the prompt again.
    """
    plan = cleanup if cleanup is not None else FlashCleanup()
    steps: list[Step] = [*_erase_steps()]
    if plan.config_file:
        steps += _clear_config_file_steps(plan.config_file)
    if plan.vlan_present:
        steps += _delete_steps(
            VLAN_DATABASE,
            "destroys the VLAN database",
            tag="vlan",
            step="delete_vlan_database",
        )
    for name in plan.stale:
        steps += _delete_steps(name, "a previous owner's configuration")
    if plan.deleted:
        steps.append(
            Step(
                name="confirm_flash_clean",
                description="dir flash: (confirm every deletion)",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"dir flash:\r"),
                verify=_all_gone(plan.deleted),
                timeout=30.0,
            )
        )
    steps += _reload_steps(profile.boot_timeout)
    return Playbook(
        name="reset_switch",
        description="Erase startup-config, vlan.dat and stale configs, then reload",
        steps=tuple(steps),
    )


class PlatformUnknownError(RuntimeError):
    """The platform could not be identified, so no reset will be guessed at."""


def for_model(
    model_name: str | None,
    *,
    platform: str | None = None,
    cleanup: FlashCleanup | None = None,
) -> Playbook:
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
            return switch_reset(cleanup, catalyst_profile(model_name))
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
        return switch_reset(cleanup, catalyst_profile(model_name))

    raise PlatformUnknownError(
        f"{model_name} is not a platform with a verified reset procedure. "
        f"State it explicitly with --platform router|catalyst if you are sure."
    )
