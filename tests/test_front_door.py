"""The front door gives advice a newcomer can type and have work."""

from __future__ import annotations

import shlex

import pytest

from ciscoyoke.cli import build_parser
from ciscoyoke.lifecycle.rescue import _ADVICE, Recommendation, next_step
from ciscoyoke.stream.tracker import State


@pytest.mark.parametrize("recommendation", list(Recommendation))
def test_every_recommended_command_is_one_the_cli_accepts(
    recommendation: Recommendation,
) -> None:
    """`rescue` once told a silent device's owner to run `doctor --port`, an
    option that never existed. Every advised command must parse."""
    advice = _ADVICE[recommendation].format(target="COM4").replace("<ios.bin>", "x.bin")
    parser = build_parser()
    for command in advice.split("then:"):
        words = shlex.split(command.strip())
        if words and words[0] == "ciscoyoke":  # otherwise prose, not a command
            parser.parse_args(words[1:])  # SystemExit on anything it rejects


def test_every_state_has_a_next_step() -> None:
    for state in State:
        assert next_step(state)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (State.LOGIN_USERNAME, "recover access"),
        (State.PRIV_EXEC, "ready"),
        (State.BOOTLOADER, "rescue"),
        (State.SILENT, "sweep"),
        (State.DESTRUCTIVE_RECOVERY_GUARD, "stop: recovery disabled"),
    ],
)
def test_scan_says_what_each_device_needs(state: State, expected: str) -> None:
    assert next_step(state) == expected
