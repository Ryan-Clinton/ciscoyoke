"""Access recovery for fixed-configuration Catalyst switches.

Different from a router in a way that matters: there is no break sequence and no
configuration register. The bootloader is reached by **holding the Mode button
while reconnecting power**, which no software can do, and the configuration is
bypassed by *renaming a file* rather than by setting a register.

The rename is the good news. ``config.text`` moved aside is entirely reversible
-- the file is still there, and putting the name back restores the previous
owner's configuration exactly. The recovery is therefore genuinely
non-destructive when it goes to plan, which is worth preserving carefully rather
than skipping in favour of an erase.

``load_helper`` before ``flash_init`` is not optional on the older platforms this
targets, and neither is doing both before touching flash at all.
"""

from __future__ import annotations

from ciscoyoke.journal import model
from ciscoyoke.playbook.base import (
    Effect,
    Playbook,
    Step,
    send_line,
)
from ciscoyoke.stream.tracker import State

CONFIG_FILE = "flash:config.text"
STASHED_FILE = "flash:config.old"

#: Guard authorising moving the configuration aside.
ACCEPT_CONFIG_RENAME = "accept_config_rename"


def prepare_flash() -> Playbook:
    """Initialise the flash filesystem from the bootloader prompt.

    Read-only: ``flash_init`` and ``load_helper`` mount and prepare, they do not
    modify. Separated from the rename so a caller can inspect flash -- and find
    out whether ``config.text`` even exists -- before changing anything.
    """
    return Playbook(
        name="prepare_flash",
        description="Initialise flash so its contents can be listed",
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
        ),
    )


def bypass_configuration() -> Playbook:
    """Move ``config.text`` aside and boot without it.

    Reversible by construction: the file is renamed, never deleted, so the
    previous owner's configuration survives the recovery intact and
    :func:`restore_configuration` can put it back byte for byte.
    """
    return Playbook(
        name="bypass_configuration",
        description="Rename config.text aside and boot without a configuration",
        steps=(
            Step(
                name="stash_configuration",
                description=f"rename {CONFIG_FILE} -> {STASHED_FILE}",
                require_state=(State.BOOTLOADER,),
                expect=(State.BOOTLOADER,),
                action=send_line(f"rename {CONFIG_FILE} {STASHED_FILE}\r".encode()),
                effect=Effect.DESTRUCTIVE,
                mutation=model.rename_flash_file(CONFIG_FILE, STASHED_FILE),
                timeout=30.0,
            ),
            Step(
                name="boot",
                description="boot without a configuration",
                require_state=(State.BOOTLOADER,),
                expect=(State.SETUP_DIALOG, State.PRESS_RETURN, State.USER_EXEC),
                action=send_line(b"boot\r"),
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
            Step(
                name="enable",
                description="enter privileged mode (no password without a config)",
                require_state=(State.PRESS_RETURN, State.USER_EXEC),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"\renable\r"),
                timeout=30.0,
            ),
        ),
    )


def restore_configuration() -> Playbook:
    """Put the configuration back and load it.

    The rename is undone first so the file is where IOS expects it, then copied
    into the running configuration. Copying to *running* rather than *startup*
    is deliberate: it takes effect immediately and can still be abandoned by a
    reload if it turns out to lock the operator out again.
    """
    return Playbook(
        name="restore_switch_configuration",
        description="Restore config.text and load the recovered configuration",
        steps=(
            Step(
                name="unstash_configuration",
                description=f"rename {STASHED_FILE} -> {CONFIG_FILE}",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(f"rename {STASHED_FILE} {CONFIG_FILE}\r".encode()),
                effect=Effect.DESTRUCTIVE,
                mutation=model.rename_flash_file(STASHED_FILE, CONFIG_FILE),
                timeout=30.0,
            ),
            Step(
                name="load_configuration",
                description="copy flash:config.text system:running-config",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"copy flash:config.text system:running-config\r\r"),
                timeout=60.0,
            ),
        ),
    )


def full_access_recovery() -> Playbook:
    """The whole procedure from the bootloader onwards.

    Does **not** include the Mode-button sequence: that is a physical action and
    belongs to :func:`ciscoyoke.playbook.human.catalyst_mode_button`, which can
    prove it happened. Folding it in here would mean pretending software could
    perform it.
    """
    steps = (
        *prepare_flash().steps,
        *bypass_configuration().steps,
        *restore_configuration().steps,
    )
    return Playbook(
        name="switch_access_recovery",
        description=(
            "From the bootloader: initialise flash, move the configuration "
            "aside, boot, then restore and load it"
        ),
        steps=steps,
    )
