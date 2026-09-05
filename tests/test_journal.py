"""Journal semantics: reversibility, write-ahead, convergence and rollback."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ciscoyoke.journal import model
from ciscoyoke.journal.converge import (
    Resolution,
    reconcile,
    resume_point,
    roll_back,
)
from ciscoyoke.journal.model import (
    CompensationMissingError,
    IrreversibleWithoutGuardError,
    Mutation,
    MutationStatus,
    Reversibility,
)
from ciscoyoke.journal.store import Journal, connect, find_interrupted


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "state.db")
    try:
        yield connection
    finally:
        connection.close()


def new_journal(connection: sqlite3.Connection, target: str = "usb:0403:6001:A1") -> Journal:
    return Journal.begin(
        connection,
        target_key=target,
        target_name="bench-left",
        operation="recover_image",
        starting_state="bootloader",
    )


# -- reversibility ---------------------------------------------------------


def test_recoverable_mutation_must_carry_a_compensation() -> None:
    """Declared recoverable but with nothing to recover with is a lie.

    Enforced in the constructor, so an unsafe mutation cannot be built at all
    rather than merely being discouraged at review time.
    """
    with pytest.raises(CompensationMissingError):
        Mutation(kind="console_baud", reversibility=Reversibility.COMPENSATABLE)


def test_irreversible_mutation_must_name_its_guard() -> None:
    with pytest.raises(IrreversibleWithoutGuardError):
        Mutation(kind="write_erase", reversibility=Reversibility.IRREVERSIBLE)


def test_irreversible_mutation_cannot_claim_a_compensation() -> None:
    """The false promise the earlier spec made, now impossible to express."""
    with pytest.raises(ValueError, match="false promise"):
        Mutation(
            kind="write_erase",
            reversibility=Reversibility.IRREVERSIBLE,
            guard="accept_config_loss",
            compensation={"action": "restore"},
        )


def test_write_erase_declares_what_it_destroys() -> None:
    mutation = model.write_erase(guard="accept_config_loss")
    assert mutation.reversibility is Reversibility.IRREVERSIBLE
    assert mutation.compensation is None
    assert "startup-config destroyed" in mutation.destructive_effects
    assert mutation.needs_compensation is False


def test_deleting_the_last_image_says_so() -> None:
    mutation = model.delete_flash_file(
        "flash:c2950-old.bin", guard="accept_stranded_risk", only_bootable=True
    )
    assert "no rollback image remains during transfer" in mutation.destructive_effects


def test_baud_change_compensates_to_the_original() -> None:
    mutation = model.console_baud(9600, 115200)
    assert mutation.reversibility is Reversibility.COMPENSATABLE
    assert mutation.compensation == {"action": "set_baud", "baud": 9600}


# -- write-ahead protocol --------------------------------------------------


def test_attempting_leaves_the_mutation_unresolved(db: sqlite3.Connection) -> None:
    """The heart of the protocol.

    Leaving the block does *not* mean the mutation applied -- only that it was
    sent. Claiming otherwise because the sending code returned would be exactly
    the false certainty the journal exists to prevent.
    """
    journal = new_journal(db)

    with journal.attempting(model.console_baud(9600, 115200)) as mutation:
        assert mutation.status is MutationStatus.ATTEMPTED

    stored = journal.mutations()[0]
    assert stored.status is MutationStatus.ATTEMPTED
    assert stored.unresolved is True


def test_a_crash_mid_mutation_leaves_an_honest_record(db: sqlite3.Connection) -> None:
    """Simulate the process dying inside the attempt block."""
    journal = new_journal(db)

    with pytest.raises(KeyboardInterrupt), journal.attempting(
        model.console_baud(9600, 115200)
    ):
        raise KeyboardInterrupt

    survivors = journal.unresolved()
    assert len(survivors) == 1
    assert survivors[0].status is MutationStatus.ATTEMPTED


def test_intent_is_durable_across_connections(tmp_path: Path) -> None:
    """A separate process must be able to read what the dead one recorded."""
    path = tmp_path / "state.db"

    first = connect(path)
    journal = new_journal(first)
    with journal.attempting(model.console_baud(9600, 115200)):
        pass
    first.close()

    second = connect(path)
    try:
        recovered = find_interrupted(second, "usb:0403:6001:A1")
        assert recovered is not None
        assert recovered.run_id == journal.run_id
        assert len(recovered.unresolved()) == 1
    finally:
        second.close()


def test_finished_runs_are_not_offered_for_resume(db: sqlite3.Connection) -> None:
    journal = new_journal(db)
    with journal.attempting(model.console_baud(9600, 115200)) as mutation:
        pass
    journal.committed(mutation)
    journal.finish("completed")

    assert find_interrupted(db, "usb:0403:6001:A1") is None


# -- convergence -----------------------------------------------------------


def test_observation_promotes_an_attempted_mutation(db: sqlite3.Connection) -> None:
    journal = new_journal(db)
    with journal.attempting(model.console_baud(9600, 115200)):
        pass

    report = reconcile(
        journal, lambda _m: (Resolution.APPLIED, "device responded at 115200")
    )

    assert report.resumable is True
    assert journal.mutations()[0].status is MutationStatus.OBSERVED_APPLIED
    assert "device responded at 115200" in report.render()


def test_observation_demotes_a_mutation_that_never_landed(db: sqlite3.Connection) -> None:
    """The journal said it was sent; the device says otherwise. Reality wins."""
    journal = new_journal(db)
    with journal.attempting(model.console_baud(9600, 115200)):
        pass

    reconcile(journal, lambda _m: (Resolution.NOT_APPLIED, "still at 9600"))

    assert journal.mutations()[0].status is MutationStatus.FAILED
    assert journal.unresolved() == ()


def test_indeterminate_blocks_automatic_resume(db: sqlite3.Connection) -> None:
    """Not knowing is a legitimate answer, and it must stop the tool.

    Continuing automatically through a mutation whose effect could not be
    established is exactly the recklessness this design refuses.
    """
    journal = new_journal(db)
    with journal.attempting(model.console_baud(9600, 115200)):
        pass

    report = reconcile(journal, lambda _m: (Resolution.INDETERMINATE, "no response"))

    assert report.resumable is False
    assert len(report.blocked_by) == 1
    assert journal.unresolved() != ()
    assert "cannot resume automatically" in report.render()


def test_reconciliation_is_idempotent(db: sqlite3.Connection) -> None:
    """Resuming twice must equal resuming once."""
    journal = new_journal(db)
    with journal.attempting(model.console_baud(9600, 115200)):
        pass

    first = reconcile(journal, lambda _m: (Resolution.APPLIED, "at 115200"))
    statuses_after_first = [m.status for m in journal.mutations()]

    second = reconcile(journal, lambda _m: (Resolution.APPLIED, "at 115200"))

    assert [m.status for m in journal.mutations()] == statuses_after_first
    assert first.resumable is second.resumable
    # Nothing left unresolved, so the second pass had nothing to do.
    assert second.reconciliations == ()


# -- rollback --------------------------------------------------------------


def test_rollback_runs_newest_first(db: sqlite3.Connection) -> None:
    """Order matters.

    A baud change made after a config-register change has to be undone first,
    or the register cannot be read back at the original speed -- the rollback
    would strand itself.
    """
    journal = new_journal(db)

    for mutation in (
        model.config_register("0x2102", "0x2142"),
        model.console_baud(9600, 115200),
    ):
        with journal.attempting(mutation) as attempted:
            journal.observed(attempted)

    order: list[str] = []

    def compensate(mutation: Mutation) -> bool:
        order.append(mutation.kind)
        return True

    roll_back(journal, compensate)
    assert order == ["console_baud", "config_register"]


def test_rollback_reports_what_it_cannot_undo(db: sqlite3.Connection) -> None:
    """An irreversible mutation must not be silently omitted.

    Omitting it would imply the device was restored when part of it cannot be.
    """
    journal = new_journal(db)

    with journal.attempting(model.console_baud(9600, 115200)) as baud:
        journal.observed(baud)
    with journal.attempting(model.write_erase(guard="accept_config_loss")) as erase:
        journal.observed(erase)

    stranded = roll_back(journal, lambda _m: True)

    assert [m.kind for m in stranded] == ["write_erase"]


def test_failed_compensation_leaves_the_mutation_in_effect(db: sqlite3.Connection) -> None:
    """If the device did not confirm the rollback, do not claim it happened."""
    journal = new_journal(db)
    with journal.attempting(model.console_baud(9600, 115200)) as mutation:
        journal.observed(mutation)

    roll_back(journal, lambda _m: False)

    assert journal.mutations()[0].status is MutationStatus.OBSERVED_APPLIED


def test_resume_point_is_after_the_last_settled_step(db: sqlite3.Connection) -> None:
    journal = new_journal(db)

    with journal.attempting(model.console_baud(9600, 115200)) as first:
        journal.committed(first)
    with journal.attempting(model.config_register("0x2102", "0x2142")):
        pass

    assert resume_point(journal) == 1
