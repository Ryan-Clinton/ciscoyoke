"""Human steps performed end to end, and credentials that never leak."""

from __future__ import annotations

import pytest

from ciscoyoke.credentials import Credentials, Secret
from ciscoyoke.interactive import authenticate
from ciscoyoke.playbook.human import (
    HumanActionAbandonedError,
    catalyst_mode_button,
    perform,
    power_cycle,
)
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import Direction, Record, Transcript

SENTINEL = "Tr0ub4dor-SENTINEL-3xample"


def session_over(records: tuple[Record, ...]) -> tuple[Session, FakeDevice]:
    device = FakeDevice(Transcript(records=records), strict=False)
    session = Session(
        FakeTransport(device), clock=device.monotonic, sleep=device.sleeper()
    )
    return session, device


def session_at(text: bytes) -> tuple[Session, FakeDevice]:
    device = FakeDevice(Transcript(records=()), strict=False)
    tracker = StateTracker()
    tracker.feed(text)
    session = Session(
        FakeTransport(device),
        tracker=tracker,
        clock=device.monotonic,
        sleep=device.sleeper(),
    )
    return session, device


# -- human steps -----------------------------------------------------------


def test_a_human_step_advances_on_console_evidence_not_a_claim() -> None:
    """Watching for `switch:` advances on a fact.

    "Press enter when you have held the button" would advance on a claim, and
    walk on into a state it does not understand when someone releases early.
    """
    session, _ = session_over(
        (Record(0.0, Direction.RX, b"\r\nswitch: "),)
    )
    announced: list[str] = []

    observed = perform(catalyst_mode_button(), session, announced.append)

    assert observed is State.BOOTLOADER
    assert any("MODE button" in text for text in announced)
    assert any("observed" in text for text in announced)


def test_releasing_the_button_early_is_diagnosed_specifically() -> None:
    """'Expected the bootloader, saw a normal boot' tells the operator what to
    do differently. 'Timed out' does not."""
    session, _ = session_over(
        (Record(0.0, Direction.RX, b"\r\nPress RETURN to get started!\r\n"),)
    )

    step = catalyst_mode_button()
    with pytest.raises(HumanActionAbandonedError) as excinfo:
        perform(step, session, lambda _t: None)

    message = str(excinfo.value)
    assert "expected the console to show bootloader" in message
    assert "observed press_return" in message
    # The state-specific hint, not the generic one: seeing a completed boot
    # says something more useful than "the button was not held".
    assert "completed a normal boot" in message
    assert "holding MODE for longer" in message


def test_recovery_disabled_during_a_human_step_is_called_out() -> None:
    session, _ = session_over(
        (
            Record(
                0.0,
                Direction.RX,
                b"WARNING: PASSWORD RECOVERY FUNCTIONALITY IS DISABLED\r\n",
            ),
        )
    )

    with pytest.raises(HumanActionAbandonedError) as excinfo:
        perform(catalyst_mode_button(), session, lambda _t: None)

    assert "destroy its startup configuration" in str(excinfo.value)


def test_a_human_step_must_declare_observable_evidence() -> None:
    from ciscoyoke.playbook.human import HumanStep

    with pytest.raises(ValueError, match="observable evidence"):
        HumanStep(
            name="hopeful",
            instruction="do something",
            reason="because",
            evidence_states=(),
        )


def test_the_instruction_states_why_it_is_necessary() -> None:
    rendered = catalyst_mode_button().render(3, 8)

    assert "Step 3/8" in rendered
    assert "Why:" in rendered
    assert "no software path to it" in rendered


def test_a_power_cycle_accepts_any_sign_of_life() -> None:
    session, _ = session_over(
        (Record(0.0, Direction.RX, b"\r\nrommon 1 > "),)
    )
    assert perform(power_cycle(), session, lambda _t: None) is State.ROMMON


# -- authentication --------------------------------------------------------


def test_authenticating_reaches_a_privileged_prompt() -> None:
    session, _ = session_over(
        (
            Record(0.0, Direction.RX, b"\r\nUsername: "),
            Record(0.1, Direction.RX, b"\r\nPassword: "),
            Record(0.2, Direction.RX, b"\r\nRouter>"),
            Record(0.3, Direction.RX, b"\r\nRouter#"),
        )
    )
    session.observe_passively(listen=0.1)

    ok = authenticate(
        session,
        Credentials(
            username="admin",
            password=Secret(SENTINEL),
            enable_password=Secret(SENTINEL),
        ),
    )

    assert ok is True


def test_the_credential_never_reaches_the_transcript() -> None:
    """Sent through the session's redacting path, so the transcript records
    that a credential was sent without recording which one."""
    session, _ = session_over(
        (
            Record(0.0, Direction.RX, b"\r\nPassword: "),
            Record(0.1, Direction.RX, b"\r\nRouter#"),
        )
    )
    session.observe_passively(listen=0.1)

    authenticate(session, Credentials(password=Secret(SENTINEL)))

    rendered = "".join(
        str(record.to_json()) for record in session.recorder.transcript()
    )
    assert SENTINEL not in rendered
    assert any(record.secret for record in session.recorder.transcript())


def test_authentication_reports_failure_rather_than_assuming() -> None:
    session, _ = session_at(b"\r\nPassword: ")

    assert authenticate(session, Credentials(password=Secret("wrong"))) is False


def test_a_device_needing_no_credentials_is_left_alone() -> None:
    session, device = session_at(b"\r\nRouter#")

    assert authenticate(session, Credentials()) is True
    assert device.sent == ()
