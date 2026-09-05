"""Reconciling a journal against the device in front of you.

The rule this module implements, stated once and applied everywhere:

    **Write intent durably, perform the action, observe reality, then commit
    the result. After interruption, observed reality always resolves incomplete
    journal entries.**

An ``ATTEMPTED`` mutation is a question, not a fact. The journal says a baud
change was sent; whether the device heard it is knowable only by looking. So on
resume the device is probed -- including at the *journalled* speed, which is the
whole reason the record exists -- and what is found decides what the journal
should have said.

Where the two disagree, reality wins and the journal is corrected. The journal
is a hypothesis, not an authority. That is commitment 1 turned on the tool's own
memory rather than on the device.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ciscoyoke.journal.model import Mutation, MutationStatus
from ciscoyoke.journal.store import Journal


class Resolution(StrEnum):
    """What observation concluded about an unresolved mutation."""

    APPLIED = "applied"
    """The device is in the expected condition; the mutation took effect."""

    NOT_APPLIED = "not_applied"
    """The device is in its prior condition; the mutation never landed."""

    INDETERMINATE = "indeterminate"
    """Neither could be established. Reported, never guessed."""


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """One unresolved mutation, and what looking at the device revealed."""

    mutation: Mutation
    resolution: Resolution
    evidence: str

    def describe(self) -> str:
        return (
            f"mutation {self.mutation.mutation_id}  {self.mutation.kind} "
            f"status {self.mutation.status.value} -> {self.resolution.value} "
            f"({self.evidence})"
        )


#: Given a mutation, determine whether the device shows its effect.
#: Returning INDETERMINATE is a legitimate answer and must stay available: a
#: prober that always commits to applied-or-not would manufacture the certainty
#: this whole mechanism exists to avoid.
Prober = Callable[[Mutation], tuple[Resolution, str]]


@dataclass(frozen=True, slots=True)
class ConvergenceReport:
    """Everything learned about an interrupted run."""

    reconciliations: tuple[Reconciliation, ...]
    resumable: bool
    blocked_by: tuple[Mutation, ...] = ()

    @property
    def applied(self) -> tuple[Mutation, ...]:
        return tuple(
            r.mutation for r in self.reconciliations if r.resolution is Resolution.APPLIED
        )

    @property
    def indeterminate(self) -> tuple[Mutation, ...]:
        return tuple(
            r.mutation
            for r in self.reconciliations
            if r.resolution is Resolution.INDETERMINATE
        )

    def render(self) -> str:
        if not self.reconciliations:
            return "No interrupted recovery to resolve."

        lines = ["Previous interrupted recovery detected.", ""]
        for reconciliation in self.reconciliations:
            mutation = reconciliation.mutation
            mark = {
                Resolution.APPLIED: "applied",
                Resolution.NOT_APPLIED: "never landed",
                Resolution.INDETERMINATE: "UNRESOLVED",
            }[reconciliation.resolution]
            lines.append(f"  {mutation.describe()}")
            lines.append(f"      -> {mark}: {reconciliation.evidence}")

        lines.append("")
        if self.resumable:
            lines.append("Recovery can safely resume.")
        else:
            lines.append(
                "Recovery cannot resume automatically: the effect of "
                f"{len(self.blocked_by)} mutation(s) could not be established. "
                "Inspect the device before continuing."
            )
        return "\n".join(lines)


def reconcile(journal: Journal, probe: Prober) -> ConvergenceReport:
    """Resolve every unresolved mutation by observing the device.

    Nothing here trusts the journal. Each unresolved entry is put to the prober,
    and the journal is then corrected to match what was found -- promoted to
    ``OBSERVED_APPLIED`` or demoted to ``FAILED``. Entries that cannot be
    settled are left unresolved and reported, because an indeterminate mutation
    is precisely the situation in which continuing automatically would be
    reckless.
    """
    reconciliations: list[Reconciliation] = []
    blocked: list[Mutation] = []

    for mutation in journal.unresolved():
        resolution, evidence = probe(mutation)

        if resolution is Resolution.APPLIED:
            journal.observed(mutation)
        elif resolution is Resolution.NOT_APPLIED:
            journal.failed(mutation)
        else:
            blocked.append(mutation)

        reconciliations.append(Reconciliation(mutation, resolution, evidence))

    return ConvergenceReport(
        reconciliations=tuple(reconciliations),
        resumable=not blocked,
        blocked_by=tuple(blocked),
    )


#: Apply a compensation. Returns True if the device confirmed the rollback.
Compensator = Callable[[Mutation], bool]


def roll_back(journal: Journal, compensate: Compensator) -> tuple[Mutation, ...]:
    """Compensate every in-effect mutation, newest first.

    Reverse order matters: a baud change made after a config-register change
    must be undone before the register can be read back at the original speed.
    Undoing in application order would strand the very rollback that is trying
    to run.

    Irreversible mutations are skipped -- there is nothing to apply -- and
    returned to the caller so they can be reported. A rollback that silently
    omitted them would imply the device was restored when part of it cannot be.
    """
    irreversible: list[Mutation] = []

    for mutation in journal.in_effect():
        if mutation.compensation is None:
            irreversible.append(mutation)
            continue
        if compensate(mutation):
            journal.compensated(mutation)

    return tuple(irreversible)


def resume_point(journal: Journal) -> int:
    """How many mutations are settled, i.e. where a resumed run should restart.

    Counts from the beginning and stops at the first entry that is not
    committed or compensated, so a run resumes after its last *settled* step
    rather than after its last *recorded* one.
    """
    settled = (MutationStatus.COMMITTED, MutationStatus.COMPENSATED)
    for index, mutation in enumerate(journal.mutations()):
        if mutation.status not in settled:
            return index
    return len(journal.mutations())
