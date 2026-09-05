"""Declarative, guarded steps with destructive-effect metadata.

A playbook is data before it is behaviour. Each step declares what state it
expects to start from, what it will send, what states it expects to reach, what
transport capabilities it needs, and what -- if anything -- it destroys. Because
all of that is declared rather than buried in control flow, a plan can be
*checked* before a single byte reaches the device.

That is the point. Discovering halfway through a recovery that the adapter
cannot send a break, or that the device announced its configuration is about to
be destroyed, leaves the hardware in an intermediate state with the operator
holding a half-finished procedure. Every check that can happen at plan time
happens at plan time, while the device is still untouched.

Three rules hold over every playbook, and each is a test rather than a comment:

* every step is guarded -- none may fire into an unverified state;
* no step with destructive effect runs while the tracker reports
  ``DESTRUCTIVE_RECOVERY_GUARD``;
* a playbook whose capability requirements exceed the transport's fails at plan
  time, never mid-execution.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ciscoyoke.journal.model import Mutation
from ciscoyoke.journal.store import Journal
from ciscoyoke.session import Session, SessionTimeoutError
from ciscoyoke.stream.tracker import State
from ciscoyoke.transport.base import CapabilityUnavailableError, Transport


class PlanRefusedError(RuntimeError):
    """The plan cannot safely run. Raised before anything is sent."""


class StepFailedError(RuntimeError):
    """A step did not reach its expected state."""

    def __init__(self, step: Step, observed: State, detail: str = "") -> None:
        self.step = step
        self.observed = observed
        expected = ", ".join(s.value for s in step.expect)
        super().__init__(
            f"step {step.name!r}: expected {expected}, observed {observed.value}"
            + (f" ({detail})" if detail else "")
        )


class Effect(StrEnum):
    """What a step does to the device."""

    READ_ONLY = "read_only"
    """Changes nothing. Safe against hardware in unknown condition."""

    SESSION_ONLY = "session_only"
    """Changes the session but not the device -- ``terminal length 0``, a baud
    change. Does not survive a reload."""

    DESTRUCTIVE = "destructive"
    """Changes or destroys device state. Requires a mutation and a guard."""


#: Runs a step's action. Returns nothing; failures raise.
Action = Callable[[Session], None]


@dataclass(frozen=True, slots=True)
class Step:
    """One guarded action.

    ``require_state`` is what makes a step refuse to fire blind: the session
    must already be somewhere the step understands. ``expect`` is what it must
    reach afterwards. A step with neither would be a bare command sent into the
    dark, which is what this design exists to prevent.
    """

    name: str
    require_state: tuple[State, ...]
    expect: tuple[State, ...]
    action: Action
    effect: Effect = Effect.READ_ONLY
    mutation: Mutation | None = None
    requires_capabilities: tuple[str, ...] = ()
    timeout: float = 30.0
    description: str = ""

    def __post_init__(self) -> None:
        if not self.expect:
            raise ValueError(
                f"{self.name}: every step must declare the states it expects to "
                f"reach; a step with no expectation cannot be verified"
            )
        if not self.require_state:
            raise ValueError(
                f"{self.name}: every step must declare the states it may start "
                f"from; a step with no precondition can fire into anything"
            )
        if self.effect is Effect.DESTRUCTIVE and self.mutation is None:
            raise ValueError(
                f"{self.name}: a destructive step must carry the mutation "
                f"describing what it destroys and whether it can be undone"
            )
        if self.mutation is not None and self.effect is not Effect.DESTRUCTIVE:
            raise ValueError(
                f"{self.name}: a step carrying a mutation must declare itself "
                f"destructive"
            )

    @property
    def destructive(self) -> bool:
        return self.effect is Effect.DESTRUCTIVE

    def describe(self) -> str:
        return self.description or self.name


@dataclass(frozen=True, slots=True)
class Playbook:
    """A named sequence of steps."""

    name: str
    steps: tuple[Step, ...]
    description: str = ""

    @property
    def destructive_steps(self) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.destructive)

    @property
    def destructive_effects(self) -> tuple[str, ...]:
        effects: list[str] = []
        for step in self.destructive_steps:
            if step.mutation is not None:
                effects.extend(step.mutation.destructive_effects)
        return tuple(effects)

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for step in self.steps:
            for capability in step.requires_capabilities:
                seen[capability] = None
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class Plan:
    """A playbook checked against a transport and a starting state."""

    playbook: Playbook
    transport_name: str
    starting_state: State
    refusals: tuple[str, ...] = field(default=())

    @property
    def safe(self) -> bool:
        return not self.refusals

    @property
    def destructive(self) -> bool:
        return bool(self.playbook.destructive_steps)

    def render(self) -> str:
        lines = [
            f"PLAN — {self.playbook.name}",
            "",
            f"  Transport        {self.transport_name}",
            f"  Starting state   {self.starting_state.value}",
            f"  Steps            {len(self.playbook.steps)}",
        ]
        effects = self.playbook.destructive_effects
        if effects:
            lines.append("  Destroys         " + "; ".join(effects))
        else:
            lines.append("  Destroys         nothing")

        lines.append("")
        for index, step in enumerate(self.playbook.steps, start=1):
            marker = "!" if step.destructive else " "
            lines.append(f"  {marker} {index}. {step.describe()}")

        if self.refusals:
            lines += ["", "REFUSED:"]
            lines.extend(f"  - {reason}" for reason in self.refusals)
        return "\n".join(lines)


def plan(
    playbook: Playbook,
    transport: Transport,
    starting_state: State,
) -> Plan:
    """Check a playbook without running it.

    Everything knowable in advance is decided here: capabilities, the guard, and
    whether the first step can even start from where the device currently is.
    """
    refusals: list[str] = []

    missing = transport.capabilities.missing(playbook.required_capabilities)
    if missing:
        refusals.append(
            f"{transport.name} cannot provide: {', '.join(missing)}"
        )

    if starting_state is State.DESTRUCTIVE_RECOVERY_GUARD and playbook.destructive_steps:
        refusals.append(
            "device reports password recovery is disabled; continuing would "
            "destroy the startup configuration"
        )

    first = playbook.steps[0] if playbook.steps else None
    if first is not None and starting_state not in first.require_state:
        allowed = ", ".join(s.value for s in first.require_state)
        refusals.append(
            f"first step {first.name!r} requires {allowed}, "
            f"device is at {starting_state.value}"
        )

    return Plan(
        playbook=playbook,
        transport_name=transport.name,
        starting_state=starting_state,
        refusals=tuple(refusals),
    )


def execute(
    checked: Plan,
    session: Session,
    journal: Journal,
    *,
    confirm: bool = False,
) -> tuple[Step, ...]:
    """Run a checked plan, journalling every mutation.

    Refuses unless the plan is safe, and refuses a destructive plan without
    ``confirm``. Returns the steps that completed.

    The destructive guard is re-checked *before each step*, not only at plan
    time: a device can announce that recovery is disabled part-way through a
    boot, after the plan was made and before the damaging step is reached.
    """
    if not checked.safe:
        raise PlanRefusedError("; ".join(checked.refusals))
    if checked.destructive and not confirm:
        raise PlanRefusedError(
            f"{checked.playbook.name} would destroy: "
            f"{'; '.join(checked.playbook.destructive_effects)}. "
            f"Re-run with confirmation to proceed."
        )

    completed: list[Step] = []

    for step in checked.playbook.steps:
        current = session.state.state

        if step.destructive and current is State.DESTRUCTIVE_RECOVERY_GUARD:
            raise PlanRefusedError(
                f"step {step.name!r} refused: device now reports password "
                f"recovery is disabled"
            )

        if current not in step.require_state:
            raise StepFailedError(step, current, "precondition not met")

        if step.mutation is None:
            _run(step, session)
            completed.append(step)
            continue

        with journal.attempting(step.mutation) as attempted:
            _run(step, session)
        # The step returned, which means the bytes were sent and the expected
        # state was observed -- that observation is what promotes the mutation.
        journal.observed(attempted)
        journal.verified(attempted)
        journal.committed(attempted)
        completed.append(step)

    return tuple(completed)


def _run(step: Step, session: Session) -> None:
    step.action(session)
    try:
        session.wait_for(step.expect, timeout=step.timeout)
    except SessionTimeoutError as exc:
        raise StepFailedError(step, session.state.state, str(exc)) from exc


def send_line(text: bytes) -> Action:
    """An action that transmits one line."""

    def action(session: Session) -> None:
        session.send(text)

    return action


def require_capabilities(
    transport: Transport, capabilities: Sequence[str]
) -> None:
    """Raise unless the transport offers everything in ``capabilities``."""
    missing = transport.capabilities.missing(tuple(capabilities))
    if missing:
        raise CapabilityUnavailableError(missing, transport.name)
