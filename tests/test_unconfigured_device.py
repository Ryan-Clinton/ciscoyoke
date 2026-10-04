"""A device with no configuration is a starting point, not a dead end.

Found on the bench WS-C2950G-24-EI straight after a reset: it sat at the setup
dialog, `rescue` had nothing to suggest, and `reset` refused it for not being
at ``Switch#`` -- then printed that refusal with its dash garbled.
"""

from __future__ import annotations

import argparse
import io

import pytest

from ciscoyoke import cli, commands
from ciscoyoke.lifecycle.rescue import Recommendation, rescue
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import Direction, Record, Transcript
from ciscoyoke.transcript.scrub import scrub

SETUP_DIALOG = (
    b"\r\n         --- System Configuration Dialog ---\r\n\r\n"
    b"Would you like to enter the initial configuration dialog? [yes/no]: "
)


def session_of(*exchange: tuple[Direction, bytes], strict: bool = True) -> Session:
    records = tuple(
        Record(float(i), direction, data) for i, (direction, data) in enumerate(exchange)
    )
    device = FakeDevice(Transcript(records=records), strict=strict)
    session = Session(
        FakeTransport(device), clock=device.monotonic, sleep=device.sleeper()
    )
    session.settle()
    return session


def test_the_setup_dialog_is_declined_on_the_way_to_a_privileged_prompt() -> None:
    session = session_of(
        (Direction.RX, SETUP_DIALOG),
        (Direction.TX, b"no\r"),
        (Direction.RX, b"no\r\n\r\n\r\nPress RETURN to get started!\r\n\r\n"),
        (Direction.TX, b"\r"),
        (Direction.RX, b"\r\nSwitch>"),
        (Direction.TX, b"enable\r"),
        (Direction.RX, b"enable\r\nSwitch#"),
    )
    assert session.state.state is State.SETUP_DIALOG

    assert commands.reach_privileged(session) is True
    assert session.state.state is State.PRIV_EXEC


def test_a_privileged_prompt_is_left_alone() -> None:
    session = session_of((Direction.RX, b"\r\nSwitch#"))
    assert commands.reach_privileged(session) is False
    assert session.state.state is State.PRIV_EXEC


def test_an_enable_password_is_never_guessed() -> None:
    session = session_of(
        (Direction.RX, b"\r\nSwitch>"),
        (Direction.TX, b"enable\r"),
        (Direction.RX, b"enable\r\nPassword: "),
    )
    with pytest.raises(commands.CommandError) as refused:
        commands.reach_privileged(session)
    assert refused.value.code is ExitCode.AUTHENTICATION_REQUIRED
    assert "recover access" in str(refused.value)


def test_rescue_at_the_setup_dialog_has_a_next_step() -> None:
    report = rescue(session_of((Direction.RX, SETUP_DIALOG), strict=False), "COM4")
    assert report.recommendation is Recommendation.RESET
    assert "no startup configuration" in report.reason
    assert report.next_command == "ciscoyoke reset COM4"


def test_a_netmask_is_not_mistaken_for_an_address() -> None:
    result = scrub(
        Transcript(
            records=(
                Record(0.0, Direction.RX, b" ip address 192.0.2.10 255.255.255.0\r\n"),
                Record(1.0, Direction.RX, b" network 192.0.2.0 0.0.0.255 area 0\r\n"),
            )
        )
    )
    text = "".join(r.data.decode("latin-1") for r in result.transcript)
    assert "192.0.2." not in text
    assert "255.255.255.0" in text
    assert "0.0.0.255" in text


def test_a_refusal_survives_a_console_that_cannot_encode_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr("sys.stderr", io.TextIOWrapper(raw, encoding="ascii"))

    def refuse() -> int:
        raise commands.CommandError(
            "PLAN — reset_switch", ExitCode.DESTRUCTIVE_ACTION_REFUSED
        )

    code = cli._run(refuse, argparse.Namespace(json=False), "reset")

    assert code == int(ExitCode.DESTRUCTIVE_ACTION_REFUSED)
    shown = raw.getvalue().decode("utf-8")
    assert "error: PLAN" in shown
    assert "reset_switch" in shown
    assert "�" not in shown and "?" not in shown
