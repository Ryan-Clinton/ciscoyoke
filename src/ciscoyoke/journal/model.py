"""Mutations, their recovery semantics, and the write-ahead lifecycle.

Two ideas carry this module.

**Not every mutation can be undone.** An earlier draft of the specification
promised that every mutation carried a compensation. That is not achievable --
there is no compensation for ``write erase``, for deleting the last bootable
image, or for accepting config loss -- and promising it made the
strongest-sounding guarantee the least true one. Mutations therefore declare a
*reversibility class*, and irreversibility is something the tool states and
guards rather than discovers afterwards.

**A journal entry is a hypothesis, not a fact.** Between recording an intent and
observing its effect sits a physical device on the end of a cable that can be
unplugged. Write first and crash, and the journal claims something that never
happened; act first and crash, and the device changed with no record. With an
external device that uncertainty cannot be eliminated, so it is modelled:
every mutation moves through an explicit lifecycle, and an entry that never got
past ``ATTEMPTED`` is resolved by looking at the device, never by trusting the
record.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class Reversibility(StrEnum):
    """What can be done about a mutation after the fact."""

    REVERSIBLE = "reversible"
    """Undone by an exact inverse, e.g. renaming a file back."""

    COMPENSATABLE = "compensatable"
    """Not undone, but a compensating action restores the prior condition --
    setting the console baud back to what it was."""

    IRREVERSIBLE = "irreversible"
    """Nothing restores the prior state. ``write erase`` is the archetype."""


class MutationStatus(StrEnum):
    """Where a mutation got to.

    The ordering is the write-ahead protocol: record intent durably, perform the
    action, observe reality, then commit.
    """

    PREPARED = "prepared"
    """Intent recorded. Nothing has been sent to the device."""

    ATTEMPTED = "attempted"
    """Sent. Whether it took effect is unknown -- this is the uncertain state,
    and after an interruption it must be resolved by observation."""

    OBSERVED_APPLIED = "observed_applied"
    """The device was seen to be in the expected condition."""

    POSTCONDITION_VERIFIED = "postcondition_verified"
    """Everything the mutation promised has been checked."""

    COMMITTED = "committed"
    """Complete and settled."""

    COMPENSATED = "compensated"
    """Rolled back by its compensation."""

    FAILED = "failed"
    """Did not take effect, and that was established by observation."""


# Statuses whose truth is not yet established by observation. After an
# interruption these are questions to be answered by probing the device.
UNRESOLVED: frozenset[MutationStatus] = frozenset(
    {MutationStatus.PREPARED, MutationStatus.ATTEMPTED}
)

# Statuses meaning the mutation is in effect on the device right now, and so
# may need compensating during a rollback.
IN_EFFECT: frozenset[MutationStatus] = frozenset(
    {
        MutationStatus.OBSERVED_APPLIED,
        MutationStatus.POSTCONDITION_VERIFIED,
        MutationStatus.COMMITTED,
    }
)


class IrreversibleWithoutGuardError(ValueError):
    """An irreversible mutation was declared without naming its safety guard."""


class CompensationMissingError(ValueError):
    """A recoverable mutation was declared without a compensation."""


@dataclass(frozen=True, slots=True)
class Mutation:
    """One change to a device, and everything known about undoing it.

    Validated at construction: a reversible or compensatable mutation must carry
    a compensation, and an irreversible one must name the guard that authorises
    it. Making this a constructor invariant means the rule cannot be forgotten
    at a call site -- an unsafe mutation is not merely discouraged, it cannot be
    built.
    """

    kind: str
    reversibility: Reversibility
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    compensation: dict[str, Any] | None = None
    destructive_effects: tuple[str, ...] = ()
    guard: str | None = None
    status: MutationStatus = MutationStatus.PREPARED
    mutation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    recorded_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.reversibility is Reversibility.IRREVERSIBLE:
            if self.compensation is not None:
                raise ValueError(
                    f"{self.kind}: irreversible mutations cannot have a "
                    f"compensation; declaring one would be a false promise"
                )
            if not self.guard:
                raise IrreversibleWithoutGuardError(
                    f"{self.kind}: an irreversible mutation must name the safety "
                    f"guard that authorises it"
                )
        elif self.compensation is None:
            raise CompensationMissingError(
                f"{self.kind}: a {self.reversibility.value} mutation must carry a "
                f"compensation"
            )

    # -- lifecycle ---------------------------------------------------------

    def advance(self, status: MutationStatus) -> Mutation:
        """Return this mutation at a new lifecycle status."""
        return replace(self, status=status)

    @property
    def unresolved(self) -> bool:
        """Whether the device must be consulted to know what really happened."""
        return self.status in UNRESOLVED

    @property
    def in_effect(self) -> bool:
        return self.status in IN_EFFECT

    @property
    def needs_compensation(self) -> bool:
        """Whether rolling back would have to do something about this."""
        return self.in_effect and self.compensation is not None

    def describe(self) -> str:
        arrow = ""
        if self.before and self.after:
            before = ", ".join(f"{k}={v}" for k, v in self.before.items())
            after = ", ".join(f"{k}={v}" for k, v in self.after.items())
            arrow = f" {before} -> {after}"
        return f"{self.kind}{arrow} [{self.reversibility.value}, {self.status.value}]"

    def to_json(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "kind": self.kind,
            "reversibility": self.reversibility.value,
            "status": self.status.value,
            "before": self.before,
            "after": self.after,
            "compensation": self.compensation,
            "destructive_effects": list(self.destructive_effects),
            "guard": self.guard,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Mutation:
        return cls(
            kind=str(obj["kind"]),
            reversibility=Reversibility(str(obj["reversibility"])),
            before=dict(obj.get("before") or {}),
            after=dict(obj.get("after") or {}),
            compensation=obj.get("compensation"),
            destructive_effects=tuple(obj.get("destructive_effects") or ()),
            guard=obj.get("guard"),
            status=MutationStatus(str(obj["status"])),
            mutation_id=str(obj["mutation_id"]),
            recorded_at=float(obj.get("recorded_at") or 0.0),
        )


# -- constructors for the mutations this project actually performs ----------


def console_baud(before: int, after: int) -> Mutation:
    """Raise or restore the console line speed.

    The archetypal compensatable mutation, and the one that makes the journal
    necessary at all: an image transfer raises the console to 115200, and an
    interruption leaves the device there with nothing but the journal to
    suggest where to look for it.
    """
    return Mutation(
        kind="console_baud",
        reversibility=Reversibility.COMPENSATABLE,
        before={"baud": before},
        after={"baud": after},
        compensation={"action": "set_baud", "baud": before},
    )


def config_register(before: str, after: str) -> Mutation:
    return Mutation(
        kind="config_register",
        reversibility=Reversibility.COMPENSATABLE,
        before={"config_register": before},
        after={"config_register": after},
        compensation={"action": "set_config_register", "value": before},
    )


def rename_flash_file(source: str, destination: str) -> Mutation:
    return Mutation(
        kind="rename_flash_file",
        reversibility=Reversibility.REVERSIBLE,
        before={"name": source},
        after={"name": destination},
        compensation={"action": "rename", "source": destination, "destination": source},
    )


def write_erase(guard: str) -> Mutation:
    """Destroy the startup configuration. Nothing brings it back."""
    return Mutation(
        kind="write_erase",
        reversibility=Reversibility.IRREVERSIBLE,
        destructive_effects=("startup-config destroyed",),
        guard=guard,
    )


def delete_flash_file(name: str, guard: str, *, only_bootable: bool = False) -> Mutation:
    effects = [f"{name} deleted"]
    if only_bootable:
        effects.append("no rollback image remains during transfer")
    return Mutation(
        kind="delete_flash_file",
        reversibility=Reversibility.IRREVERSIBLE,
        before={"name": name, "only_bootable": only_bootable},
        destructive_effects=tuple(effects),
        guard=guard,
    )
