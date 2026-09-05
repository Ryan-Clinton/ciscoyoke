"""Access recovery for IOS routers, via ROMMON.

The documented procedure: interrupt the boot with a break, set the configuration
register to ``0x2142`` so the startup configuration is skipped, reset, then --
crucially -- *copy the startup config back into running config* rather than
starting from nothing, set a new password, and restore ``0x2102``.

That copy-back step is the difference between recovery and destruction. Booting
with ``0x2142`` does not erase anything; it bypasses. A procedure that stops at
"you now have a blank router" has thrown away the previous owner's configuration
for no reason, which on a second-hand device is the very thing worth keeping.

The break window is the awkward part. ROMMON is reachable only in roughly the
first sixty seconds after power-up, so the human power-cycle and the automated
break have to be coordinated -- which is why the power cycle is a
:class:`~ciscoyoke.playbook.human.HumanStep` with observable evidence rather
than a hopeful ``sleep``.
"""

from __future__ import annotations

from ciscoyoke.journal import model
from ciscoyoke.playbook.base import (
    Effect,
    Playbook,
    Step,
    send_line,
)
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State

#: Boots ignoring the startup configuration. Does not erase it.
BYPASS_REGISTER = "0x2142"

#: The normal setting: boot from flash, load the startup configuration.
NORMAL_REGISTER = "0x2102"

#: Guard authorising a register change during access recovery.
ACCEPT_REGISTER_CHANGE = "accept_config_register_change"


def _send_break(session: Session) -> None:
    """Assert a break to drop the booting router into ROMMON.

    Whether the adapter actually drives a break is a transport capability, not
    an assumption -- the step declares ``send_break`` so a transport that cannot
    do it fails at plan time rather than after the device has been power-cycled.
    """
    breaker = getattr(session.transport, "send_break", None)
    if breaker is None:
        raise RuntimeError("transport cannot send a break")
    breaker()


def enter_rommon() -> Playbook:
    """Get a booting router to the ROMMON prompt.

    Assumes the caller has already had the device power-cycled -- see
    :func:`ciscoyoke.playbook.human.router_break_window`, which supplies the
    observable evidence that a restart actually happened.
    """
    return Playbook(
        name="enter_rommon",
        description="Break into ROMMON during the boot window",
        steps=(
            Step(
                name="send_break",
                description="send a break during the boot window",
                require_state=(State.BOOTING,),
                expect=(State.ROMMON,),
                action=_send_break,
                requires_capabilities=("send_break",),
                timeout=90.0,
            ),
        ),
    )


def bypass_startup_config() -> Playbook:
    """Set ``0x2142`` and reset, so the router boots without its config.

    The register change is compensatable: whatever happens next, the journal
    knows how to put it back. That matters because a router left at ``0x2142``
    silently ignores its configuration on every subsequent boot -- a fault that
    surfaces days later and looks like something else entirely.
    """
    return Playbook(
        name="bypass_startup_config",
        description="Boot ignoring the startup configuration (does not erase it)",
        steps=(
            Step(
                name="set_bypass_register",
                description=f"confreg {BYPASS_REGISTER} (bypass startup-config)",
                require_state=(State.ROMMON,),
                expect=(State.ROMMON,),
                action=send_line(f"confreg {BYPASS_REGISTER}\r".encode()),
                effect=Effect.DESTRUCTIVE,
                mutation=model.config_register(NORMAL_REGISTER, BYPASS_REGISTER),
                timeout=30.0,
            ),
            Step(
                name="reset",
                description="reset and wait for the router to boot",
                require_state=(State.ROMMON,),
                expect=(State.SETUP_DIALOG, State.PRESS_RETURN, State.USER_EXEC),
                action=send_line(b"reset\r"),
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
                description="enter privileged mode (no password after bypass)",
                require_state=(State.PRESS_RETURN, State.USER_EXEC),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"\renable\r"),
                timeout=30.0,
            ),
        ),
    )


def restore_configuration() -> Playbook:
    """Recover the previous configuration and put the register back.

    ``copy startup-config running-config`` is what makes this recovery rather
    than a wipe. Skipping it leaves a blank router and discards exactly what a
    second-hand device was worth reading.
    """
    return Playbook(
        name="restore_configuration",
        description="Reload the saved configuration and restore normal booting",
        steps=(
            Step(
                name="copy_startup_to_running",
                description="copy startup-config running-config (recovers the config)",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"copy startup-config running-config\r\r"),
                timeout=60.0,
            ),
            Step(
                name="restore_register",
                description=f"config-register {NORMAL_REGISTER} (normal booting)",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC, State.CONFIG_MODE),
                action=send_line(
                    f"configure terminal\rconfig-register {NORMAL_REGISTER}\rend\r".encode()
                ),
                effect=Effect.DESTRUCTIVE,
                mutation=model.config_register(BYPASS_REGISTER, NORMAL_REGISTER),
                timeout=30.0,
            ),
        ),
    )


def full_access_recovery() -> Playbook:
    """The whole documented procedure, as one plan.

    Composed from the parts so each can be run alone -- a router already sitting
    in ROMMON does not need the break -- while the combined form is what
    ``recover access`` presents to someone who just wants the box back.
    """
    steps = (
        *enter_rommon().steps,
        *bypass_startup_config().steps,
        *restore_configuration().steps,
    )
    return Playbook(
        name="router_access_recovery",
        description=(
            "Break into ROMMON, boot bypassing the startup configuration, "
            "recover it, and restore normal booting"
        ),
        steps=steps,
    )
