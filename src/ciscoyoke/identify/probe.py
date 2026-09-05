"""Passive-first device identification.

The ordering is the whole design. Against hardware in unknown condition, the
sequence is:

1. **Listen.** Change nothing. A booting device narrates itself, and a device
   mid-transfer must not be interrupted.
2. **Nudge**, only if silent. A bare carriage return is the least destructive
   thing a console accepts.
3. **Ask**, only if already at a privileged prompt. ``show version`` is
   read-only, but it still requires a login the tool may not have.

At no point does this module change device configuration. ``intake`` is safe to
run against anything, which is what makes it the natural first command and the
one that can be recommended without qualification.
"""

from __future__ import annotations

from dataclasses import dataclass

from ciscoyoke.identify import facts as facts_module
from ciscoyoke.identify.facts import DeviceFacts
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import Observation, State

# States from which `show version` can be issued without authenticating.
_INTERROGABLE = (State.PRIV_EXEC, State.USER_EXEC)

# States that mean a human or a recovery is required before identity is known.
_LOCKED = (State.LOGIN_USERNAME, State.LOGIN_PASSWORD, State.ENABLE_PASSWORD)


@dataclass(frozen=True, slots=True)
class Intake:
    """What one passive-first pass learned."""

    observation: Observation
    facts: DeviceFacts
    interrogated: bool
    note: str

    @property
    def locked(self) -> bool:
        return self.observation.state in _LOCKED

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.observation.state.value,
            "basis": self.observation.basis.value,
            "confidence": self.observation.confidence.value,
            "evidence": [
                {
                    "signal": signal.kind.value,
                    "value": signal.value,
                    "context": signal.context,
                }
                for signal in self.observation.evidence
            ],
            "facts": self.facts.to_json(),
            "interrogated": self.interrogated,
            "note": self.note,
        }


def intake(session: Session, *, interrogate: bool = True) -> Intake:
    """Identify a device without changing it.

    ``interrogate=False`` restricts this to pure listening, for a caller that
    wants to be certain not one byte was transmitted.
    """
    observation = session.observe_passively()

    if observation.state is State.UNKNOWN and session.is_silent():
        # Nothing at all. Either the device is genuinely quiet, or the baud is
        # wrong, or the cable is not what it appears to be. A nudge tells us
        # which without risking anything.
        observation = session.wake()

    banner_facts = facts_module.from_boot_banner(session.tracker.buffer.text)

    if observation.state in _LOCKED:
        return Intake(
            observation=observation,
            facts=banner_facts,
            interrogated=False,
            note="identity requires boot evidence or credentials",
        )

    if observation.state is State.DESTRUCTIVE_RECOVERY_GUARD:
        return Intake(
            observation=observation,
            facts=banner_facts,
            interrogated=False,
            note=(
                "password recovery is disabled on this device; interrupting the "
                "boot may destroy the startup configuration"
            ),
        )

    if not interrogate or observation.state not in _INTERROGABLE:
        return Intake(
            observation=observation,
            facts=banner_facts,
            interrogated=False,
            note=_note_for(observation.state),
        )

    return Intake(
        observation=observation,
        facts=_interrogate(session),
        interrogated=True,
        note="identified from show version",
    )


def _interrogate(session: Session) -> DeviceFacts:
    """Run ``show version`` and parse it.

    ``terminal length 0`` is sent first. It is a session-scoped display setting
    that does not touch the configuration and does not survive a reload, so it
    stays inside the read-only guarantee -- but it is the difference between
    parsing one page and paging through five.
    """
    session.send(b"terminal length 0\r")
    session.settle()

    before = len(session.tracker.buffer)
    session.send(b"show version\r")
    session.settle(timeout=10.0)
    session.drain_pager()

    output = session.tracker.buffer.text[before:]
    return facts_module.from_show_version(output)


def _note_for(state: State) -> str:
    return {
        State.ROMMON: "device is in ROMMON; no IOS booted",
        State.BOOTLOADER: "bootloader reached; no IOS booted",
        State.BOOTING: "device is still booting",
        State.SETUP_DIALOG: "device is offering the initial configuration dialog",
        State.PRESS_RETURN: "device is booted and waiting for a carriage return",
        State.PAGER: "device is holding a pager prompt",
        State.UNKNOWN: "no signal at this baud rate; check cable, power and speed",
        State.SILENT: "no signal at this baud rate; check cable, power and speed",
    }.get(state, f"device is at {state.value}")
