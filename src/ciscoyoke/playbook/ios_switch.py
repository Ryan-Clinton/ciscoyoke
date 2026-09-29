"""Access recovery for fixed-configuration Catalyst switches.

Different from a router in a way that matters: there is no break sequence and no
configuration register. The bootloader is reached by **holding the Mode button
while reconnecting power**, which no software can do, and the configuration is
bypassed by *renaming a file* rather than by setting a register.

The rename is the good news. The configuration moved aside is entirely
reversible -- the file is still there, and putting the name back restores the
previous owner's configuration exactly. The recovery is therefore genuinely
non-destructive when it goes to plan, which is worth preserving carefully rather
than skipping in favour of an erase.

*Which* file to move is read from the device, not assumed. The bootloader's
``CONFIG_FILE`` variable can point IOS at something other than ``config.text``
-- managed estates do this -- and renaming ``config.text`` on such a switch
changes a file IOS never reads, then boots straight back into the login the
recovery was meant to bypass. So the procedure runs in two phases: a read-only
look at the boot environment and the flash listing, then a rename chosen from
what that look found (:func:`decide_stash`).

``load_helper`` before ``flash_init`` is not optional on the older platforms this
targets, and neither is doing both before touching flash at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ciscoyoke.journal import model
from ciscoyoke.playbook.base import (
    Effect,
    Playbook,
    Step,
    send_line,
)
from ciscoyoke.stream.tracker import State

DEFAULT_CONFIG = "config.text"
DEFAULT_STASH = "config.old"

#: Guard authorising moving the configuration aside.
ACCEPT_CONFIG_RENAME = "accept_config_rename"

_STILL_CONFIGURED = (
    "the switch booted into a login, so a configuration was still loaded; "
    "the file moved aside was not the one IOS reads"
)
_LOGIN_MEANS_CONFIGURED = (
    (State.LOGIN_USERNAME, _STILL_CONFIGURED),
    (State.LOGIN_PASSWORD, _STILL_CONFIGURED),
)


# -- reading the device ------------------------------------------------------


_ENV_LINE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)=(.*?)\s*$", re.MULTILINE)
# `dir flash:` on a Catalyst bootloader:
#     2  -rwx  1674   <date>  config.text
#     4  drwx  192    <date>  c2950-i6q4l2-mz.121-9.EA1
_DIR_LINE = re.compile(r"^\s*\d+\s+[-d][-rwx]{3,}\s+\d+\s+.*?(\S+)\s*$", re.MULTILINE)


def boot_environment(text: str) -> dict[str, str]:
    """Parse the output of the bootloader's ``set`` command."""
    return dict(_ENV_LINE.findall(text))


def flash_files(text: str) -> frozenset[str]:
    """File and directory names from a ``dir flash:`` listing."""
    return frozenset(_DIR_LINE.findall(text))


def _basename(path: str) -> str:
    """``flash:/config.text``, ``flash:config.text`` -> ``config.text``."""
    return path.split(":", 1)[-1].lstrip("/")


@dataclass(frozen=True, slots=True)
class StashDecision:
    """Which file to move aside, and to where -- or that none needs moving."""

    source: str | None
    stash: str | None
    reason: str

    @property
    def renames(self) -> bool:
        return self.source is not None and self.stash is not None


class StashRefusedError(RuntimeError):
    """What flash and the boot environment show does not add up; nothing moved."""


def decide_stash(environment: dict[str, str], files: frozenset[str]) -> StashDecision:
    """Choose the rename from what the device actually reported.

    Refuses rather than guesses when the evidence is inconsistent, because a
    rename of the wrong file is the failure this function exists to prevent:
    it succeeds, the switch reboots, and the login is still there.
    """
    configured = environment.get("CONFIG_FILE", "").strip()
    source = _basename(configured) if configured else DEFAULT_CONFIG
    origin = f"CONFIG_FILE={configured}" if configured else "the default"

    if source in files:
        candidates = (
            DEFAULT_STASH if source == DEFAULT_CONFIG else f"{source}.old",
            f"{source}.ciscoyoke",
        )
        free = [name for name in candidates if name not in files]
        if not free:
            raise StashRefusedError(
                f"{source} is loaded ({origin}), but {' and '.join(candidates)} "
                f"already exist in flash; nothing was renamed"
            )
        stash = free[0]
        return StashDecision(
            source=source,
            stash=stash,
            reason=f"IOS loads {source} ({origin}); it will be renamed to {stash}",
        )

    if configured:
        raise StashRefusedError(
            f"CONFIG_FILE points at {configured}, which is not in flash "
            f"(found: {', '.join(sorted(files)) or 'nothing'}); nothing was renamed"
        )

    if DEFAULT_STASH in files:
        return StashDecision(
            source=None,
            stash=None,
            reason=(
                f"{DEFAULT_CONFIG} is absent and {DEFAULT_STASH} exists: an earlier "
                f"recovery already moved it aside, so there is nothing to rename"
            ),
        )

    return StashDecision(
        source=None,
        stash=None,
        reason=f"no {DEFAULT_CONFIG} in flash; the switch has no startup configuration",
    )


# -- playbooks ---------------------------------------------------------------


def prepare_flash() -> Playbook:
    """Initialise flash and read what the recovery needs to decide. Read-only.

    ``flash_init`` and ``load_helper`` mount and prepare; ``dir`` and ``set``
    only report. Nothing here changes the device, so a recovery that stops
    after this phase has cost nothing.
    """
    return Playbook(
        name="prepare_flash",
        description="Initialise flash and read the boot environment",
        steps=(
            Step(
                name="flash_init",
                description="flash_init (mount the flash filesystem)",
                require_state=(State.BOOTLOADER,),
                expect=(State.BOOTLOADER,),
                action=send_line(b"flash_init\r"),
                timeout=60.0,
            ),
            Step(
                name="load_helper",
                description="load_helper (required on older platforms)",
                require_state=(State.BOOTLOADER,),
                expect=(State.BOOTLOADER,),
                action=send_line(b"load_helper\r"),
                timeout=30.0,
            ),
            Step(
                name="list_flash",
                description="dir flash: (list contents before changing anything)",
                require_state=(State.BOOTLOADER,),
                expect=(State.BOOTLOADER,),
                action=send_line(b"dir flash:\r"),
                timeout=30.0,
            ),
            Step(
                name="read_boot_environment",
                description="set (read CONFIG_FILE: which file IOS actually loads)",
                require_state=(State.BOOTLOADER,),
                expect=(State.BOOTLOADER,),
                action=send_line(b"set\r"),
                timeout=30.0,
            ),
        ),
    )


def _moved(source: str, stash: str) -> Callable[[str], str | None]:
    """Verify a rename from a fresh listing: stash present, source gone."""

    def check(output: str) -> str | None:
        files = flash_files(output)
        if stash not in files:
            return f"{stash} is not in flash after the rename"
        if source in files:
            return f"{source} is still in flash after the rename"
        return None

    return check


def bypass_configuration(decision: StashDecision) -> Playbook:
    """Move the loaded configuration aside, prove it moved, and boot without it.

    Reversible by construction: the file is renamed, never deleted, so the
    previous owner's configuration survives the recovery intact and
    :func:`restore_configuration` can put it back byte for byte.
    """
    steps: list[Step] = []
    if decision.renames:
        assert decision.source is not None and decision.stash is not None
        source, stash = f"flash:{decision.source}", f"flash:{decision.stash}"
        steps += [
            Step(
                name="stash_configuration",
                description=f"rename {source} -> {stash}",
                require_state=(State.BOOTLOADER,),
                expect=(State.BOOTLOADER,),
                action=send_line(f"rename {source} {stash}\r".encode()),
                effect=Effect.DESTRUCTIVE,
                mutation=model.rename_flash_file(source, stash),
                timeout=30.0,
            ),
            Step(
                name="confirm_stash",
                description=f"dir flash: (confirm {decision.source} moved)",
                require_state=(State.BOOTLOADER,),
                expect=(State.BOOTLOADER,),
                action=send_line(b"dir flash:\r"),
                verify=_moved(decision.source, decision.stash),
                timeout=30.0,
            ),
        ]
    steps += [
        Step(
            name="boot",
            description="boot without a configuration",
            require_state=(State.BOOTLOADER,),
            expect=(State.SETUP_DIALOG, State.PRESS_RETURN, State.USER_EXEC),
            action=send_line(b"boot\r"),
            refuse=_LOGIN_MEANS_CONFIGURED,
            timeout=300.0,
        ),
        Step(
            name="decline_setup_dialog",
            description="decline the initial configuration dialog",
            require_state=(State.SETUP_DIALOG,),
            expect=(State.PRESS_RETURN, State.USER_EXEC),
            action=send_line(b"no\r"),
            refuse=_LOGIN_MEANS_CONFIGURED,
            optional=True,
            timeout=60.0,
        ),
        Step(
            name="enable",
            description="enter privileged mode (no password without a config)",
            require_state=(State.PRESS_RETURN, State.USER_EXEC),
            expect=(State.PRIV_EXEC,),
            action=send_line(b"\renable\r"),
            refuse=_LOGIN_MEANS_CONFIGURED,
            timeout=30.0,
        ),
    ]
    return Playbook(
        name="bypass_configuration",
        description="Move the loaded configuration aside and boot without it",
        steps=tuple(steps),
    )


def restore_configuration(decision: StashDecision) -> Playbook:
    """Put the configuration back and load it.

    The rename is undone first so the file is where IOS expects it, then copied
    into the running configuration. Copying to *running* rather than *startup*
    is deliberate: it takes effect immediately and can still be abandoned by a
    reload if it turns out to lock the operator out again.

    Empty when nothing was moved aside: there is no configuration of ours to
    put back.
    """
    if not decision.renames:
        return Playbook(name="restore_switch_configuration", steps=())
    assert decision.source is not None and decision.stash is not None
    source, stash = f"flash:{decision.source}", f"flash:{decision.stash}"
    return Playbook(
        name="restore_switch_configuration",
        description="Restore the configuration file and load it",
        steps=(
            Step(
                name="unstash_configuration",
                description=f"rename {stash} -> {source}",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                # IOS asks "Destination filename [config.text]?" and the second
                # CR accepts it. Sent together because on a real 2950 a syslog
                # line landed after the question, the prompt was no longer at
                # the end of the stream, and the step waited on a question it
                # could no longer see.
                action=send_line(f"rename {stash} {source}\r\r".encode()),
                effect=Effect.DESTRUCTIVE,
                mutation=model.rename_flash_file(stash, source),
                timeout=30.0,
            ),
            Step(
                name="load_configuration",
                description=f"copy {source} system:running-config",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(f"copy {source} system:running-config\r\r".encode()),
                timeout=60.0,
            ),
        ),
    )


#: What the preview shows before the device has been read.
PRESUMED = StashDecision(
    source=DEFAULT_CONFIG,
    stash=DEFAULT_STASH,
    reason="presumed until the boot environment has been read",
)


def after_prepare(decision: StashDecision) -> Playbook:
    """Everything after the read-only phase, for the file actually loaded."""
    return Playbook(
        name="switch_access_recovery",
        description=decision.reason,
        steps=(
            *bypass_configuration(decision).steps,
            *restore_configuration(decision).steps,
        ),
    )


def full_access_recovery(decision: StashDecision = PRESUMED) -> Playbook:
    """The whole procedure from the bootloader onwards.

    For previewing. A real run executes :func:`prepare_flash` first and builds
    the rest with :func:`after_prepare` from what it read, because the file to
    move is not known until then.

    Does **not** include the Mode-button sequence: that is a physical action and
    belongs to :func:`ciscoyoke.playbook.human.catalyst_mode_button`, which can
    prove it happened. Folding it in here would mean pretending software could
    perform it.
    """
    return Playbook(
        name="switch_access_recovery",
        description=(
            "From the bootloader: initialise flash, read which configuration "
            "IOS loads, move it aside, boot, then restore and load it"
        ),
        steps=(*prepare_flash().steps, *after_prepare(decision).steps),
    )
