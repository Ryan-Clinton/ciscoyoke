"""Physical actions a person has to perform, modelled as verifiable steps.

Catalyst fixed-configuration password recovery requires holding the Mode button
while reconnecting power. No amount of software makes that automatic, and a tool
whose ``unlock`` verb pretends otherwise is lying about physics.

The alternative is not to reduce scope but to model it properly. A
:class:`HumanStep` carries the instruction, the reason it is necessary, and --
critically -- the **machine-observable evidence** that proves it was actually
done. The tool never takes the operator's word for it. It watches the console
and continues when the device itself confirms.

That distinction matters more than it looks. "Press enter when you've held the
button" advances on a claim; watching for ``switch:`` advances on a fact. When
someone releases the button half a second early, the first design proceeds into
a state it does not understand and the second says so.

The same abstraction covers manual power cycling and cable moves, and any future
platform with front-panel interactions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ciscoyoke.session import Session, SessionTimeoutError
from ciscoyoke.stream.tracker import State

#: Shows an instruction to the operator. Returns nothing.
Announcer = Callable[[str], None]


class HumanActionAbandonedError(RuntimeError):
    """The physical action was not completed, or not completed correctly."""

    def __init__(self, step: HumanStep, observed: State, hint: str = "") -> None:
        self.step = step
        self.observed = observed
        expected = ", ".join(s.value for s in step.evidence_states)
        message = (
            f"{step.name}: expected the console to show {expected}, "
            f"observed {observed.value}"
        )
        if hint:
            message += f"\n  {hint}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HumanStep:
    """An instruction to a person, and the proof the tool will accept."""

    name: str
    instruction: str
    reason: str
    evidence_states: tuple[State, ...]
    timeout: float = 120.0
    recovery_hint: str = ""
    wrong_state_hints: tuple[tuple[State, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence_states:
            raise ValueError(
                f"{self.name}: a human step must declare observable evidence; "
                f"without it the tool would be advancing on a claim rather than "
                f"on a fact"
            )

    def render(self, index: int | None = None, total: int | None = None) -> str:
        header = "HUMAN ACTION REQUIRED"
        if index is not None and total is not None:
            header = f"Step {index}/{total}   {header}"

        expected = ", ".join(state.value for state in self.evidence_states)
        return "\n".join(
            [
                "",
                header,
                "",
                f"  {self.instruction}",
                "",
                f"  Why: {self.reason}",
                "",
                f"  Waiting for the console to show: {expected}",
                f"  (up to {int(self.timeout)}s; Ctrl-C to abandon)",
                "",
            ]
        )

    def hint_for(self, observed: State) -> str:
        """Advice for a state that means the action went wrong in a known way."""
        for state, hint in self.wrong_state_hints:
            if state is observed:
                return hint
        return self.recovery_hint


def perform(
    step: HumanStep,
    session: Session,
    announce: Announcer,
) -> State:
    """Show the instruction, then watch the console for proof.

    Returns the state actually observed. Raises
    :class:`HumanActionAbandonedError` if the evidence never appears, carrying
    what was seen instead -- "expected the bootloader, observed a normal boot"
    tells the operator they released the button too early, where "timed out"
    tells them nothing.
    """
    announce(step.render())

    try:
        observation = session.wait_for(step.evidence_states, timeout=step.timeout)
    except SessionTimeoutError as exc:
        observed = session.state.state
        raise HumanActionAbandonedError(step, observed, step.hint_for(observed)) from exc

    announce(f"  ✓ observed: {observation.state.value}\n\n  Continuing automatically.\n")
    return observation.state


# -- the physical actions this project actually needs -----------------------


def catalyst_mode_button() -> HumanStep:
    """Hold Mode while reconnecting power, to reach the bootloader.

    The archetypal case: the only route to ``switch:`` on a fixed-configuration
    Catalyst, and entirely outside software's control.
    """
    return HumanStep(
        name="catalyst_mode_button",
        instruction=(
            "Unplug the switch. Hold the MODE button down, plug the power back "
            "in, and keep holding until the console responds."
        ),
        reason=(
            "the bootloader is only reachable through this sequence on this "
            "platform; there is no software path to it"
        ),
        evidence_states=(State.BOOTLOADER,),
        timeout=180.0,
        recovery_hint=(
            "If the switch booted normally, the button was released too early. "
            "Power off and try again, holding until the console reacts."
        ),
        wrong_state_hints=(
            (
                State.PRESS_RETURN,
                "The switch completed a normal boot, so the bootloader was "
                "missed. Power off and repeat, holding MODE for longer.",
            ),
            (
                State.SETUP_DIALOG,
                "The switch booted with no configuration and is offering the "
                "setup dialog. The bootloader was missed; power off and repeat.",
            ),
            (
                State.DESTRUCTIVE_RECOVERY_GUARD,
                "This switch has password recovery disabled. Continuing would "
                "destroy its startup configuration. Stopping here.",
            ),
        ),
    )


def power_cycle() -> HumanStep:
    """Ask for a power cycle and wait for the device to start talking."""
    return HumanStep(
        name="power_cycle",
        instruction="Power the device off, wait five seconds, and power it on.",
        reason="the device must be restarted to reach the requested state",
        evidence_states=(State.BOOTING, State.ROMMON, State.BOOTLOADER),
        timeout=120.0,
        recovery_hint=(
            "If nothing appears, check the console cable and that the port is "
            "at the right baud rate: ciscoyoke doctor"
        ),
    )


def router_break_window() -> HumanStep:
    """Power-cycle a router so a break can be sent during the boot window.

    ROMMON is reachable only in the first seconds after power-up, so the human
    action and the automated break have to be coordinated. Modelling the human
    part explicitly is what makes that coordination possible rather than a race.
    """
    return HumanStep(
        name="router_break_window",
        instruction=(
            "Power the router off, then on. Do not touch the console -- a break "
            "will be sent automatically during the boot window."
        ),
        reason=(
            "ROMMON can only be entered within roughly sixty seconds of "
            "power-up, so the break has to be timed against a restart"
        ),
        evidence_states=(State.BOOTING,),
        timeout=120.0,
        recovery_hint=(
            "If the router reached a prompt instead, the break did not take. "
            "Some USB adapters do not drive a true break; ciscoyoke doctor "
            "reports what this one supports."
        ),
    )
