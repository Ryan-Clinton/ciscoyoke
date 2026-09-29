"""A clean switch in one pass: the six gaps a real WS-C2950G-24-EI exposed.

Every text sample here is byte-for-byte what that switch sent, LF-CR line
endings included.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ciscoyoke.identify import memory
from ciscoyoke.identify.facts import from_boot_banner
from ciscoyoke.journal import model
from ciscoyoke.journal.paths import STATE_DIR_ENV
from ciscoyoke.journal.store import Journal, connect, find_interrupted
from ciscoyoke.lifecycle import reset
from ciscoyoke.playbook import ios_switch
from ciscoyoke.stream.signals import strip_syslog
from ciscoyoke.stream.tracker import State, StateTracker

BOOTLOADER_AFTER_MODE = (
    "\r\n\rC2950 Boot Loader (CALHOUN-HBOOT-M) Version 12.1(0.0.34)EA2, "
    "CISCO DEVELOPMENT TEST VERSION\n\r"
    "Compiled Wed 07-Nov-01 20:59 by antonino\n\r"
    "WS-C2950G-24 starting...\n\r"
    "Base ethernet MAC Address: 00:1b:2c:3d:4e:5f\n\r"
)
BOOTLOADER_SET = (
    "set\n\rMAC_ADDR=00:1B:2C:3D:4E:5F\n\rMODEL_NUM=WS-C2950G-24-EI\n\r"
    "MODEL_REVISION_NUM=C0\n\rSYSTEM_SERIAL_NUM=ABC1234D567\n\rswitch: "
)
IOS_BOOT = "Model number: WS-C2950G-24-EI\r\nSystem serial number: ABC1234D567\r\n"
FLASH_FILES = frozenset(
    {
        "env_vars",
        "vlan.dat",
        "info",
        "config.text",
        "config.old",
        "c2950-i6q4l2-mz.121-9.EA1.bin",
        "html",
        "info.ver",
    }
)


# -- 4: console log messages ------------------------------------------------


def feed(text: bytes) -> State:
    tracker = StateTracker()
    tracker.feed(text)
    return tracker.current.state


def test_a_log_message_spliced_onto_a_question_does_not_hide_it() -> None:
    """The exact bytes that stalled the rename back on the real switch."""
    assert (
        feed(
            b"Switch#rename flash:config.text.ciscoyoke flash:config.text\r\n"
            b"Destination filename [config.text]? 18: %LINEPROTO-5-UPDOWN: "
            b"Line protocol on Interface Vlan1, changed state to down\r\n"
        )
        is State.FILENAME_PROMPT
    )


def test_a_log_message_after_a_prompt_leaves_the_prompt_last() -> None:
    assert (
        feed(
            b"\r\nSwitch#\r\n*Mar  1 00:12:18.759: %IP_SNMP-3-SOCKET: "
            b"can't open UDP socket\r\n"
        )
        is State.PRIV_EXEC
    )


def test_a_log_message_before_a_prompt_keeps_the_prompt_on_its_own_line() -> None:
    text = "Loading\r\n00:00:12: %ENVIRONMENT-2-FAN_FAULT: FAN FAULT\r\nSwitch#"
    assert strip_syslog(text) == "Loading\nSwitch#"


def test_ordinary_output_is_untouched() -> None:
    text = "Directory of flash:/\r\n  17  -rwx  6087  config.old\r\nSwitch#"
    assert strip_syslog(text) == text


# -- 6: identity without --platform -----------------------------------------


def test_the_bootloader_banner_names_the_switch() -> None:
    facts = from_boot_banner(BOOTLOADER_AFTER_MODE)
    assert facts.model.value == "WS-C2950G-24"


def test_the_bootloader_set_output_names_the_exact_model() -> None:
    facts = from_boot_banner(BOOTLOADER_AFTER_MODE + BOOTLOADER_SET)
    assert facts.model.value == "WS-C2950G-24-EI"
    assert facts.serial_number.value == "ABC1234D567"


def test_a_normal_ios_boot_names_the_model() -> None:
    assert from_boot_banner(IOS_BOOT).model.value == "WS-C2950G-24-EI"


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path))
    return tmp_path


def test_a_model_is_remembered_per_adapter(state_dir: Path) -> None:
    memory.remember("usb:0403:6001:A9C5K8V5A", "WS-C2950G-24-EI", "capture")

    recalled = memory.recall("usb:0403:6001:A9C5K8V5A")
    assert recalled is not None
    assert recalled.model == "WS-C2950G-24-EI"
    assert "capture" in recalled.describe()
    assert memory.recall("usb:0403:6001:OTHER") is None


def test_corrupt_memory_is_forgotten_not_fatal(state_dir: Path) -> None:
    (state_dir / memory.MEMORY_FILE).write_text("{not json", encoding="utf-8")
    assert memory.recall("anything") is None


# -- 2: recovery without restoring ------------------------------------------


def test_no_restore_stops_with_the_configuration_still_aside() -> None:
    decision = ios_switch.decide_stash({}, FLASH_FILES)
    names = [s.name for s in ios_switch.after_prepare(decision, restore=False).steps]
    assert "enable" in names
    assert "unstash_configuration" not in names
    assert "load_configuration" not in names


# -- 1: reset removes every configuration -----------------------------------


def test_stale_configurations_are_found_and_nvram_files_left_to_write_erase() -> None:
    files = FLASH_FILES | {"config.text.ciscoyoke", "private-config.text", "lab.cfg"}
    assert reset.stale_configs(files) == ("config.old", "config.text.ciscoyoke", "lab.cfg")


def test_the_image_and_system_files_are_never_stale() -> None:
    assert reset.stale_configs(frozenset({"c2950-i6q4l2-mz.121-9.EA1.bin", "html", "env_vars", "info", "vlan.dat"})) == ()


def test_a_default_boot_config_file_needs_no_change() -> None:
    assert reset.nondefault_config_file("Config file:          flash:/config.text\r\n") is None
    assert (
        reset.nondefault_config_file("Config file:          flash:/switch.cfg\r\n")
        == "flash:/switch.cfg"
    )


def test_reset_deletes_each_stale_file_and_proves_it() -> None:
    cleanup = reset.FlashCleanup(stale=("config.old",), vlan_present=True)
    steps = {step.name: step for step in reset.switch_reset(cleanup).steps}

    assert "delete_vlan_database" in steps
    assert "delete_config.old" in steps
    deleted = steps["delete_config.old"]
    assert deleted.mutation is not None
    assert deleted.mutation.before["name"] == "flash:config.old"

    confirm = steps["confirm_flash_clean"]
    assert confirm.verify is not None
    assert confirm.verify("  17  -rwx  6087  Mar 01 1993 00:02:22  config.old\r\n")
    assert confirm.verify("   5  -rwx  2490607  Mar 01 1993  c2950.bin\r\n") is None


def test_an_absent_vlan_database_is_not_deleted() -> None:
    names = [s.name for s in reset.switch_reset(reset.FlashCleanup(vlan_present=False)).steps]
    assert "delete_vlan_database" not in names
    assert "confirm_flash_clean" not in names


def test_a_nondefault_boot_config_file_is_cleared_reversibly() -> None:
    cleanup = reset.FlashCleanup(config_file="flash:/switch.cfg")
    steps = {step.name: step for step in reset.switch_reset(cleanup).steps}
    cleared = steps["clear_boot_config_file"]
    assert cleared.mutation is not None
    assert cleared.mutation.compensation == {
        "action": "boot config-file",
        "value": "flash:/switch.cfg",
    }


# -- 5: a failed run is not invisible ---------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "state.db")
    try:
        yield connection
    finally:
        connection.close()


KEY = "usb:0403:6001:A9C5K8V5A"


def run(db: sqlite3.Connection) -> Journal:
    return Journal.begin(db, target_key=KEY, target_name="COM4", operation="recover_access")


def test_a_failed_run_with_an_unobserved_mutation_blocks_the_next(
    db: sqlite3.Connection,
) -> None:
    """The real case: the rename back was sent, never seen, and the run marked
    finished -- so the next command walked straight past it."""
    journal = run(db)
    with pytest.raises(RuntimeError), journal.attempting(
        model.rename_flash_file("flash:config.text.ciscoyoke", "flash:config.text")
    ):
        raise RuntimeError("the prompt was hidden by a log message")
    journal.finish("failed")

    found = find_interrupted(db, KEY)
    assert found is not None
    assert found.unresolved()


def test_a_failed_run_with_nothing_unsettled_does_not_block(db: sqlite3.Connection) -> None:
    run(db).finish("failed")
    assert find_interrupted(db, KEY) is None


def test_a_later_run_supersedes_an_earlier_failure(db: sqlite3.Connection) -> None:
    journal = run(db)
    with pytest.raises(RuntimeError), journal.attempting(
        model.rename_flash_file("flash:a", "flash:b")
    ):
        raise RuntimeError
    journal.finish("failed")
    run(db).finish("completed")

    assert find_interrupted(db, KEY) is None
