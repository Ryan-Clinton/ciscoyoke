"""Catalyst access recovery: reading which file to move, and proving it moved.

Written after a real WS-C2950G-24-EI: the recovery renamed ``config.text``,
booted, and landed on ``Username:`` anyway. Everything here is about never
declaring that kind of run a success, and about naming the cause when it
happens.
"""

from __future__ import annotations

import pytest

from ciscoyoke.playbook import ios_switch
from ciscoyoke.playbook.base import Step, StepFailedError, _run, send_line
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import Direction, Record, Transcript

# The layout of a Catalyst bootloader `dir flash:`, with the LF-then-CR line
# endings a real 2950 bootloader sends.
DIR_WITH_CONFIG = (
    "dir flash:\n\r"
    "Directory of flash:/\n\r\n\r"
    "2    -rwx  1674      <date>               c2950-i6q4l2-mz.121-9.EA1.bin\n\r"
    "4    drwx  192       <date>               html\n\r"
    "66   -rwx  1296      <date>               config.text\n\r"
    "68   -rwx  5         <date>               private-config.text\n\r\n\r"
    "3563520 bytes available (4177920 bytes used)\n\r"
)

SET_DEFAULT = "set\n\rBOOT=flash:c2950-i6q4l2-mz.121-9.EA1.bin\n\rMAC_ADDR=00:1B:2C:3D:4E:5F\n\r"
SET_REDIRECTED = SET_DEFAULT + "CONFIG_FILE=flash:/switch.cfg\n\r"


def files(*names: str) -> frozenset[str]:
    return frozenset(names)


# -- parsing ----------------------------------------------------------------


def test_the_flash_listing_yields_file_names_only() -> None:
    assert ios_switch.flash_files(DIR_WITH_CONFIG) == {
        "c2950-i6q4l2-mz.121-9.EA1.bin",
        "html",
        "config.text",
        "private-config.text",
    }


def test_the_boot_environment_is_read_from_set() -> None:
    env = ios_switch.boot_environment(SET_REDIRECTED)
    assert env["CONFIG_FILE"] == "flash:/switch.cfg"
    assert env["BOOT"] == "flash:c2950-i6q4l2-mz.121-9.EA1.bin"


# -- deciding ---------------------------------------------------------------


def test_the_default_config_is_moved_to_config_old() -> None:
    decision = ios_switch.decide_stash({}, files("config.text"))
    assert (decision.source, decision.stash) == ("config.text", "config.old")


def test_config_file_redirects_the_rename_to_the_file_ios_loads() -> None:
    """The 2950 case: config.text renamed, login still there."""
    decision = ios_switch.decide_stash(
        {"CONFIG_FILE": "flash:/switch.cfg"}, files("config.text", "switch.cfg")
    )
    assert (decision.source, decision.stash) == ("switch.cfg", "switch.cfg.old")
    assert "CONFIG_FILE=flash:/switch.cfg" in decision.reason


def test_a_config_file_that_is_not_in_flash_is_refused() -> None:
    with pytest.raises(ios_switch.StashRefusedError, match="not in flash"):
        ios_switch.decide_stash({"CONFIG_FILE": "flash:gone.cfg"}, files("config.text"))


def test_an_existing_stash_is_never_overwritten() -> None:
    decision = ios_switch.decide_stash({}, files("config.text", "config.old"))
    assert decision.stash == "config.text.ciscoyoke"


def test_no_free_stash_name_is_refused() -> None:
    with pytest.raises(ios_switch.StashRefusedError, match="already exist"):
        ios_switch.decide_stash(
            {}, files("config.text", "config.old", "config.text.ciscoyoke")
        )


def test_an_earlier_stash_means_nothing_to_rename() -> None:
    decision = ios_switch.decide_stash({}, files("config.old"))
    assert not decision.renames
    assert "earlier recovery" in decision.reason
    assert ios_switch.restore_configuration(decision).steps == ()


def test_the_real_run_moves_the_decided_file_and_proves_it() -> None:
    decision = ios_switch.decide_stash(
        {"CONFIG_FILE": "flash:switch.cfg"}, files("switch.cfg")
    )
    names = [step.name for step in ios_switch.after_prepare(decision).steps]
    assert names[:3] == ["stash_configuration", "confirm_stash", "boot"]
    rename = ios_switch.after_prepare(decision).steps[0]
    assert rename.mutation is not None
    assert "switch.cfg" in rename.describe()


def test_the_ios_rename_answers_its_destination_question() -> None:
    """``Destination filename [config.text]?`` must be answered in the same
    breath; a log line landing after it hides the prompt from the tracker."""
    sent: list[bytes] = []

    class Capture:
        def send(self, data: bytes) -> None:
            sent.append(data)

    decision = ios_switch.decide_stash({}, files("config.text", "config.old"))
    unstash = ios_switch.restore_configuration(decision).steps[0]
    unstash.action(Capture())  # type: ignore[arg-type]

    assert sent == [b"rename flash:config.text.ciscoyoke flash:config.text\r\r"]


def test_prepare_reads_the_boot_environment_before_anything_changes() -> None:
    steps = ios_switch.prepare_flash().steps
    assert [step.name for step in steps][-1] == "read_boot_environment"
    assert not any(step.destructive for step in steps)


# -- verifying ---------------------------------------------------------------


def replying(initial: bytes, reply: bytes) -> Session:
    device = FakeDevice(
        Transcript(records=(Record(0.0, Direction.RX, reply),)), strict=False
    )
    tracker = StateTracker()
    tracker.feed(initial)
    return Session(
        FakeTransport(device),
        tracker=tracker,
        clock=device.monotonic,
        sleep=device.sleeper(),
    )


def confirm_step(source: str, stash: str) -> Step:
    decision = ios_switch.StashDecision(source=source, stash=stash, reason="")
    return ios_switch.bypass_configuration(decision).steps[1]


def test_a_rename_that_did_not_happen_fails_at_the_prompt_it_reached() -> None:
    """The bootloader answers a failed rename with the same ``switch:``."""
    session = replying(b"\n\rswitch: ", DIR_WITH_CONFIG.encode() + b"\n\rswitch: ")

    with pytest.raises(StepFailedError, match=r"config\.old is not in flash"):
        _run(confirm_step("config.text", "config.old"), session)


def test_a_rename_that_happened_passes() -> None:
    moved = DIR_WITH_CONFIG.replace("config.text", "config.old")
    session = replying(b"\n\rswitch: ", moved.encode() + b"\n\rswitch: ")

    _run(confirm_step("config.text", "config.old"), session)


def test_booting_into_a_login_is_named_not_timed_out() -> None:
    enable = next(
        step
        for step in ios_switch.bypass_configuration(ios_switch.PRESUMED).steps
        if step.name == "enable"
    )
    session = replying(b"\r\nPress RETURN to get started!\r\n", b"\r\nUsername: ")

    with pytest.raises(StepFailedError, match="configuration was still loaded"):
        _run(enable, session)


def test_verify_sees_only_what_arrived_after_the_action() -> None:
    seen: list[str] = []

    def record(output: str) -> None:
        seen.append(output)

    step = Step(
        name="probe",
        require_state=(State.BOOTLOADER,),
        expect=(State.BOOTLOADER,),
        action=send_line(b"dir flash:\r"),
        verify=record,
    )
    session = replying(b"old output\n\rswitch: ", b"new output\n\rswitch: ")

    _run(step, session)

    assert seen == ["new output\n\rswitch: "]
