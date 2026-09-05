"""Playbook safety: plan-time refusal, guards, and journalled execution."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ciscoyoke.journal import model
from ciscoyoke.journal.store import Journal, connect
from ciscoyoke.lifecycle import reset
from ciscoyoke.playbook.base import (
    Effect,
    PlanRefusedError,
    Playbook,
    Step,
    StepFailedError,
    execute,
    plan,
    send_line,
)
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import Transcript
from ciscoyoke.transport.base import TransportCapabilities


class StubTransport:
    def __init__(self, name: str, capabilities: TransportCapabilities) -> None:
        self._name = name
        self._capabilities = capabilities

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> TransportCapabilities:
        return self._capabilities

    def read(self) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "state.db")
    try:
        yield connection
    finally:
        connection.close()


def journal_for(connection: sqlite3.Connection) -> Journal:
    return Journal.begin(
        connection,
        target_key="usb:test",
        target_name="bench-left",
        operation="reset",
    )


def responding_session(
    initial: bytes, reply: bytes
) -> tuple[Session, FakeDevice]:
    """A session already at ``initial`` whose device answers with ``reply``.

    Needed now that steps require evidence arriving *after* the action: a fake
    device that says nothing cannot prove the device did anything.
    """
    from ciscoyoke.transcript.schema import Direction, Record

    device = FakeDevice(
        Transcript(records=(Record(0.0, Direction.RX, reply),)), strict=False
    )
    tracker = StateTracker()
    tracker.feed(initial)
    session = Session(
        FakeTransport(device),
        tracker=tracker,
        clock=device.monotonic,
        sleep=device.sleeper(),
    )
    return session, device


def session_at(state_text: bytes) -> tuple[Session, FakeDevice]:
    """A session whose tracker has already reached a given state."""
    device = FakeDevice(Transcript(records=()), strict=False)
    tracker = StateTracker()
    tracker.feed(state_text)
    session = Session(
        FakeTransport(device),
        tracker=tracker,
        clock=device.monotonic,
        sleep=device.sleeper(),
    )
    return session, device


# -- step declaration ------------------------------------------------------


def test_a_step_must_declare_what_it_expects() -> None:
    """An unverifiable step is a command sent into the dark."""
    with pytest.raises(ValueError, match="expects to reach"):
        Step(
            name="blind",
            require_state=(State.PRIV_EXEC,),
            expect=(),
            action=send_line(b"\r"),
        )


def test_a_step_must_declare_where_it_may_start() -> None:
    with pytest.raises(ValueError, match="may start from"):
        Step(
            name="unguarded",
            require_state=(),
            expect=(State.PRIV_EXEC,),
            action=send_line(b"\r"),
        )


def test_a_destructive_step_must_carry_its_mutation() -> None:
    with pytest.raises(ValueError, match="must carry the mutation"):
        Step(
            name="erase",
            require_state=(State.PRIV_EXEC,),
            expect=(State.PRIV_EXEC,),
            action=send_line(b"write erase\r"),
            effect=Effect.DESTRUCTIVE,
        )


def test_a_mutation_step_must_declare_itself_destructive() -> None:
    with pytest.raises(ValueError, match="declare itself destructive"):
        Step(
            name="sneaky",
            require_state=(State.PRIV_EXEC,),
            expect=(State.PRIV_EXEC,),
            action=send_line(b"write erase\r"),
            mutation=model.write_erase(guard="accept_config_loss"),
        )


# -- plan-time refusal -----------------------------------------------------


def test_missing_capability_refuses_at_plan_time() -> None:
    """Never mid-device.

    Discovering the adapter cannot send a break halfway through leaves the
    hardware in an intermediate state the operator has to unpick by hand.
    """
    book = Playbook(
        name="needs_break",
        steps=(
            Step(
                name="break_in",
                require_state=(State.BOOTING,),
                expect=(State.ROMMON,),
                action=send_line(b""),
                requires_capabilities=("send_break",),
            ),
        ),
    )
    transport = StubTransport("socket://host:7001", TransportCapabilities.raw_socket())

    checked = plan(book, transport, State.BOOTING)

    assert checked.safe is False
    assert "send_break" in checked.refusals[0]
    assert "REFUSED" in checked.render()


def test_recovery_disabled_refuses_a_destructive_plan() -> None:
    transport = StubTransport("COM3", TransportCapabilities.local_serial())
    checked = plan(reset.switch_reset(), transport, State.DESTRUCTIVE_RECOVERY_GUARD)

    assert checked.safe is False
    assert "recovery is disabled" in " ".join(checked.refusals)


def test_wrong_starting_state_refuses() -> None:
    transport = StubTransport("COM3", TransportCapabilities.local_serial())
    checked = plan(reset.router_reset(), transport, State.BOOTLOADER)

    assert checked.safe is False
    assert "requires priv_exec" in " ".join(checked.refusals)


def test_a_plan_states_what_it_destroys() -> None:
    transport = StubTransport("COM3", TransportCapabilities.local_serial())
    rendered = plan(reset.switch_reset(), transport, State.PRIV_EXEC).render()

    assert "startup-config destroyed" in rendered
    assert "flash:vlan.dat deleted" in rendered


# -- execution -------------------------------------------------------------


def test_a_destructive_plan_refuses_without_confirmation(db: sqlite3.Connection) -> None:
    session, _ = session_at(b"\r\nRouter#")
    transport = StubTransport("COM3", TransportCapabilities.local_serial())
    checked = plan(reset.router_reset(), transport, State.PRIV_EXEC)

    with pytest.raises(PlanRefusedError, match="would destroy"):
        execute(checked, session, journal_for(db))


def test_an_unsafe_plan_cannot_be_executed(db: sqlite3.Connection) -> None:
    session, _ = session_at(b"\r\nswitch: ")
    transport = StubTransport("COM3", TransportCapabilities.local_serial())
    checked = plan(reset.router_reset(), transport, State.BOOTLOADER)

    with pytest.raises(PlanRefusedError):
        execute(checked, session, journal_for(db), confirm=True)


def test_the_guard_is_rechecked_before_every_step(db: sqlite3.Connection) -> None:
    """A device can announce recovery is disabled *after* the plan was made.

    Checking only at plan time would let the damaging step through in exactly
    the case the guard exists for.
    """
    session, _ = session_at(b"\r\nRouter#")
    transport = StubTransport("COM3", TransportCapabilities.local_serial())
    checked = plan(reset.router_reset(), transport, State.PRIV_EXEC)

    # The banner arrives between planning and execution.
    session.tracker.feed(b"\r\nPASSWORD RECOVERY FUNCTIONALITY IS DISABLED\r\n")

    with pytest.raises(PlanRefusedError, match="now reports"):
        execute(checked, session, journal_for(db), confirm=True)


def test_a_step_whose_precondition_fails_stops_the_run(db: sqlite3.Connection) -> None:
    session, _ = session_at(b"\r\nRouter>")
    transport = StubTransport("COM3", TransportCapabilities.local_serial())
    checked = plan(reset.router_reset(), transport, State.PRIV_EXEC)

    with pytest.raises(StepFailedError, match="precondition not met"):
        execute(checked, session, journal_for(db), confirm=True)


def test_a_step_must_prove_the_device_reacted(db: sqlite3.Connection) -> None:
    """Starting in a state that is also expected proves nothing.

    wait_for used to check the current state before reading, so a step could
    return immediately on the state it began in. That is the general form of
    the stale-evidence bug found in baud restoration.
    """
    session, _ = session_at(b"\r\nRouter#")
    transport = StubTransport("COM3", TransportCapabilities.local_serial())

    book = Playbook(
        name="silent_device",
        steps=(
            Step(
                name="erase",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"write erase\r"),
                effect=Effect.DESTRUCTIVE,
                mutation=model.write_erase(guard=reset.ACCEPT_CONFIG_LOSS),
                timeout=0.2,
            ),
        ),
    )
    checked = plan(book, transport, State.PRIV_EXEC)

    with pytest.raises(StepFailedError):
        execute(checked, session, journal_for(db), confirm=True)


def test_a_successful_step_journals_its_mutation(db: sqlite3.Connection) -> None:
    """Execution and the journal are not two bookkeeping systems."""
    session, _ = responding_session(b"\r\nRouter#", b"\r\nRouter#")
    transport = StubTransport("COM3", TransportCapabilities.local_serial())

    book = Playbook(
        name="erase_only",
        steps=(
            Step(
                name="erase",
                require_state=(State.PRIV_EXEC,),
                expect=(State.PRIV_EXEC,),
                action=send_line(b"write erase\r"),
                effect=Effect.DESTRUCTIVE,
                mutation=model.write_erase(guard=reset.ACCEPT_CONFIG_LOSS),
                timeout=1.0,
            ),
        ),
    )
    journal = journal_for(db)
    checked = plan(book, transport, State.PRIV_EXEC)

    completed = execute(checked, session, journal, confirm=True)

    assert len(completed) == 1
    recorded = journal.mutations()
    assert len(recorded) == 1
    assert recorded[0].kind == "write_erase"
    assert recorded[0].status is model.MutationStatus.COMMITTED


def test_a_failed_step_leaves_its_mutation_unresolved(db: sqlite3.Connection) -> None:
    """The honest record when a device stops responding mid-erase."""
    session, _ = session_at(b"\r\nRouter#")
    transport = StubTransport("COM3", TransportCapabilities.local_serial())

    book = Playbook(
        name="erase_then_impossible",
        steps=(
            Step(
                name="erase",
                require_state=(State.PRIV_EXEC,),
                expect=(State.ROMMON,),  # never reached
                action=send_line(b"write erase\r"),
                effect=Effect.DESTRUCTIVE,
                mutation=model.write_erase(guard=reset.ACCEPT_CONFIG_LOSS),
                timeout=0.05,
            ),
        ),
    )
    journal = journal_for(db)
    checked = plan(book, transport, State.PRIV_EXEC)

    with pytest.raises(StepFailedError):
        execute(checked, session, journal, confirm=True)

    unresolved = journal.unresolved()
    assert len(unresolved) == 1
    assert unresolved[0].status is model.MutationStatus.ATTEMPTED


# -- reset playbooks -------------------------------------------------------


def test_switch_reset_deletes_the_vlan_database() -> None:
    """The step people forget.

    `write erase` leaves vlan.dat, so a switch that looks reset comes back with
    the previous owner's VLANs and breaks the next lab for unrelated-looking
    reasons.
    """
    names = [step.name for step in reset.switch_reset().steps]
    assert "delete_vlan_database" in names


def test_router_reset_does_not_delete_a_vlan_database() -> None:
    names = [step.name for step in reset.router_reset().steps]
    assert "delete_vlan_database" not in names


def test_an_unknown_model_is_refused_rather_than_guessed_at() -> None:
    """Reset is irreversible, so it does not guess.

    An earlier version defaulted to the switch playbook, reasoning that
    deleting a nonexistent vlan.dat on a router is harmless. True -- but the
    failure is not symmetric: a wrong guess fails *after* write erase has
    already destroyed the configuration, so the cost is paid before the guess
    is discovered to be wrong.
    """
    with pytest.raises(reset.PlatformUnknownError, match="irreversible"):
        reset.for_model(None)


def test_the_operator_may_supply_the_missing_evidence() -> None:
    """Stating the platform is different from the tool inventing it."""
    router = [s.name for s in reset.for_model(None, platform="router").steps]
    catalyst = [s.name for s in reset.for_model(None, platform="catalyst").steps]

    assert "delete_vlan_database" not in router
    assert "delete_vlan_database" in catalyst


def test_an_unrecognised_model_is_also_refused() -> None:
    with pytest.raises(reset.PlatformUnknownError, match="verified reset"):
        reset.for_model("NETGEAR-GS108")


def test_a_known_router_gets_the_router_playbook() -> None:
    names = [step.name for step in reset.for_model("CISCO1760").steps]
    assert "delete_vlan_database" not in names


def test_every_reset_step_is_guarded() -> None:
    """The blanket invariant, asserted over the real playbooks."""
    for book in (reset.router_reset(), reset.switch_reset()):
        for step in book.steps:
            assert step.require_state, step.name
            assert step.expect, step.name


def test_every_destructive_step_names_its_guard() -> None:
    for book in (reset.router_reset(), reset.switch_reset()):
        for step in book.destructive_steps:
            assert step.mutation is not None
            assert step.mutation.guard == reset.ACCEPT_CONFIG_LOSS
