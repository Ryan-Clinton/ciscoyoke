"""Working first time on hardware nobody here owns, and failing usefully if not.

Covers the platform profiles, the 2960 differences taken from Cisco's
documentation, the failure record, the shareable report and the replay view.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from ciscoyoke import doctor
from ciscoyoke.diagnose import failure_path, record_failure, unrecognised_prompt
from ciscoyoke.journal.paths import STATE_DIR_ENV
from ciscoyoke.lifecycle import recover
from ciscoyoke.platform_profiles import (
    CATALYST_2950,
    CATALYST_2960,
    CATALYST_2960_STACKABLE,
    CATALYST_3560,
    GENERIC_CATALYST,
    Evidence,
    catalyst_profile,
    unverified_refusal,
)
from ciscoyoke.playbook import human, ios_switch
from ciscoyoke.playbook.base import StepFailedError, _run
from ciscoyoke.report import do_report, elide_config_output
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.replay import replay
from ciscoyoke.transcript.schema import Direction, Record, Transcript, read, write
from ciscoyoke.transport.identity import PortIdentity

FIXTURES = Path(__file__).parent / "fixtures"


# -- profiles -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("WS-C2950G-24-EI", CATALYST_2950),
        ("WS-C2960-24TT-L", CATALYST_2960),
        ("WS-C2960G-48TC-L", CATALYST_2960),
        # Longest prefix wins: these must not fall into the plain 2960 profile.
        ("WS-C2960X-24TS-L", CATALYST_2960_STACKABLE),
        ("WS-C2960S-48LPS-L", CATALYST_2960_STACKABLE),
        ("WS-C2960+24TC-L", CATALYST_2960_STACKABLE),
        ("WS-C3560-24PS-S", CATALYST_3560),
        (None, GENERIC_CATALYST),
    ],
)
def test_models_map_to_the_right_profile(model: str | None, expected: object) -> None:
    assert catalyst_profile(model) is expected


def test_only_the_2950_claims_hardware_evidence() -> None:
    assert CATALYST_2950.evidence is Evidence.HARDWARE
    assert CATALYST_2960.evidence is Evidence.DOCUMENTATION
    assert CATALYST_3560.evidence is Evidence.DOCUMENTATION
    assert (FIXTURES.parent.parent / CATALYST_2950.source).exists()


def test_the_3560_uses_its_documented_mode_release_and_helper() -> None:
    step = human.catalyst_mode_button(CATALYST_3560)
    assert "System LED" in step.instruction
    assert "amber" in step.instruction
    assert "solid green" in step.instruction
    assert CATALYST_3560.load_helper is True
    assert "cisco.com" in CATALYST_3560.source


def test_the_2960_is_told_to_watch_syst_not_stat() -> None:
    """A 2960 released 'when STAT goes out' misses the bootloader."""
    step = human.catalyst_mode_button(CATALYST_2960)
    assert "SYST" in step.instruction
    assert "amber" in step.instruction
    assert "STAT" not in step.instruction
    assert "STAT LED goes out" in human.catalyst_mode_button(CATALYST_2950).instruction


def test_load_helper_is_only_sent_where_it_exists() -> None:
    def names(profile: object) -> list[str]:
        return [s.name for s in ios_switch.prepare_flash(profile).steps]  # type: ignore[arg-type]

    assert "load_helper" in names(CATALYST_2950)
    assert "load_helper" not in names(CATALYST_2960_STACKABLE)


def test_the_recovery_path_says_how_well_the_family_is_known() -> None:
    path = recover.path_for("WS-C2960-24TT-L", State.LOGIN_USERNAME)
    assert path.profile is CATALYST_2960
    rendered = path.render()
    assert "not yet run on hardware" in rendered
    assert "cisco.com" in rendered
    assert path.human_step is not None
    assert "SYST" in path.human_step.instruction


def test_an_unknown_family_needs_explicit_acknowledgement() -> None:
    assert unverified_refusal(CATALYST_2960, "reset") is None
    refusal = unverified_refusal(GENERIC_CATALYST, "reset")
    assert refusal is not None and "--accept-unverified" in refusal


def test_an_unknown_family_gets_longer_waits() -> None:
    assert GENERIC_CATALYST.human_timeout > CATALYST_2950.human_timeout
    assert GENERIC_CATALYST.boot_timeout > CATALYST_2950.boot_timeout


# -- the 2960's prompts ---------------------------------------------------------


def state_of(text: bytes) -> State:
    tracker = StateTracker()
    tracker.feed(text)
    return tracker.current.state


def test_the_2960_setup_wording_is_the_setup_dialog() -> None:
    assert state_of(b"\r\nContinue with the configuration dialog? [yes/no]: ") is State.SETUP_DIALOG
    assert (
        state_of(b"Would you like to enter the initial configuration dialog? [yes/no]: ")
        is State.SETUP_DIALOG
    )


def test_ios15_autoinstall_is_its_own_prompt() -> None:
    assert state_of(b"\r\nWould you like to terminate autoinstall? [yes]: ") is State.AUTOINSTALL


def replying(initial: bytes, reply: bytes) -> tuple[Session, FakeDevice]:
    device = FakeDevice(Transcript(records=(Record(0.0, Direction.RX, reply),)), strict=False)
    tracker = StateTracker()
    tracker.feed(initial)
    session = Session(
        FakeTransport(device), tracker=tracker, clock=device.monotonic, sleep=device.sleeper()
    )
    return session, device


def test_declining_setup_can_lead_to_autoinstall_which_is_answered_yes() -> None:
    decline, terminate = ios_switch.after_boot_steps()

    session, device = replying(
        b"\r\nWould you like to enter the initial configuration dialog? [yes/no]: ",
        b"no\r\nWould you like to terminate autoinstall? [yes]: ",
    )
    _run(decline, session)
    assert session.state.state is State.AUTOINSTALL
    assert device.sent == (b"no\r",)

    session, device = replying(
        b"\r\nWould you like to terminate autoinstall? [yes]: ",
        b"yes\r\n\r\nPress RETURN to get started!\r\n",
    )
    _run(terminate, session)
    assert device.sent == (b"yes\r",)
    assert session.state.state is State.PRESS_RETURN


def test_both_boot_environment_spellings_are_read() -> None:
    assert ios_switch.boot_environment("CONFIG_FILE=flash:/a.cfg\n\r")["CONFIG_FILE"] == "flash:/a.cfg"
    assert ios_switch.boot_environment("CONFIG_FILE: flash:/b.cfg\n\r")["CONFIG_FILE"] == "flash:/b.cfg"


# -- adapters -----------------------------------------------------------------


def test_cisco_usb_console_is_recognised_by_its_real_vendor_id() -> None:
    assert PortIdentity(device="COM9", vid=0x05A6, pid=0x0009).adapter == "Cisco USB console"


def test_doctor_names_the_troublesome_adapters() -> None:
    checks = doctor.check_adapters(
        (
            PortIdentity(device="COM5", vid=0x067B, pid=0x2303),
            PortIdentity(device="COM6", vid=0x05A6, pid=0x0009),
            PortIdentity(device="COM7", vid=0x0403, pid=0x6001, serial_number="A1"),
        )
    )
    details = {check.name: check.detail for check in checks}
    assert "Code 10" in details["adapter COM5"]
    assert "driver" in details["adapter COM6"]
    assert "adapter COM7" not in details  # FTDI: nothing to warn about


# -- failure records ------------------------------------------------------------


def test_the_last_unrecognised_prompt_is_found_even_under_a_log_line() -> None:
    stream = (
        "Switch#rename a b\r\nSome question we never taught it [yes/no]: "
        "18: %LINEPROTO-5-UPDOWN: Line protocol on Interface Vlan1, changed state to down\r\n"
    )
    assert unrecognised_prompt(stream) == "Some question we never taught it [yes/no]:"
    assert unrecognised_prompt("Building configuration...\r\n[OK]\r\nsome output\r\n") is None


def test_a_failure_record_carries_what_a_fix_needs(tmp_path: Path) -> None:
    tracker = StateTracker()
    tracker.feed(b"System serial number: FOC1234X56Y\r\n\r\nNew prompt nobody expected? [y/n]: ")
    step = ios_switch.after_boot_steps()[0]
    error = StepFailedError(step, tracker.current.state, "timed out")

    record = record_failure(
        operation="recover_access",
        error=error,
        tracker=tracker,
        identity=PortIdentity(device="COM4", vid=0x0403, pid=0x6001, serial_number="A1"),
        profile=CATALYST_2960.to_json(),
        model="WS-C2960-24TT-L",
        transcript=tmp_path / "run.ytx",
    )
    data = record.to_json()
    assert data["failed_step"] == "decline_setup_dialog"
    assert "press_return" in data["expected"]
    assert data["unrecognised_prompt"] == "New prompt nobody expected? [y/n]:"
    assert data["adapter"]["adapter"] == "FTDI"
    assert data["profile"]["evidence"] == "documentation"
    assert "FOC1234X56Y" not in json.dumps(data)  # scrubbed like a transcript
    assert failure_path(tmp_path / "run.ytx").name == "run.failure.json"


# -- the report -----------------------------------------------------------------

SCHOOL_CONFIG = (
    b"banner exec ^C\r\n*School Name: Example Academy*\r\n^C\r\n"
    b"radius-server host 192.0.2.5 auth-port 1812 acct-port 1813 key K3y!\r\n"
    b"hostname EXAMPLE-SW\r\n"
)


def run_with_config() -> Transcript:
    return Transcript(
        records=(
            Record(0.0, Direction.TX, b"more flash:config.old\r"),
            Record(0.1, Direction.RX, b"more flash:config.old\r\n" + SCHOOL_CONFIG),
            Record(0.2, Direction.RX, b"Switch#"),
            Record(1.0, Direction.TX, b"dir flash:\r"),
            Record(1.1, Direction.RX, b"Directory of flash:/\r\n  2  -rwx  273  env_vars\r\nSwitch#"),
        )
    )


def test_configuration_output_is_removed_not_scrubbed() -> None:
    elided, blocks = elide_config_output(run_with_config())
    text = "".join(r.data.decode("latin-1") for r in elided)
    assert blocks == 1
    assert "Example Academy" not in text
    assert "K3y!" not in text
    assert "configuration output removed from report" in text
    assert "env_vars" in text  # a flash listing is file names, and stays


def test_a_report_is_one_zip_with_nothing_private_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    run = tmp_path / "20260930-120000-reset.ytx"
    write(run, run_with_config())
    failure_path(run).write_text(
        json.dumps({"last_output": SCHOOL_CONFIG.decode(), "unrecognised_prompt": None}),
        encoding="utf-8",
    )

    outcome = do_report(run, tmp_path)

    with zipfile.ZipFile(outcome.path) as archive:
        names = set(archive.namelist())
        assert {"README.txt", "transcript.ytx.pub", "failure.json", "host/doctor.json"} <= names
        assert "host/README.txt" not in names
        everything = b"".join(archive.read(name) for name in names)
    assert b"Example Academy" not in everything
    assert b"K3y!" not in everything
    assert outcome.elided == 1 and outcome.had_failure


# -- replay -----------------------------------------------------------------------


def test_replay_of_a_real_stall_shows_where_it_stopped() -> None:
    result = replay(read(FIXTURES / "hw-switch-2950-recover-rename-hidden-by-syslog.ytx.pub"))
    assert result.final_state is State.FILENAME_PROMPT
    assert any("rename flash:config.text.ciscoyoke" in e.detail for e in result.events)
    assert "Destination filename" in (result.unrecognised_prompt or "")


def test_replay_suggests_a_detector_for_an_unknown_prompt() -> None:
    transcript = Transcript(
        records=(Record(0.0, Direction.RX, b"\r\nProceed with factory reset? (y/n) "),)
    )
    rendered = replay(transcript).render()
    assert "unrecognised prompt" in rendered
    assert "signals.py" in rendered
