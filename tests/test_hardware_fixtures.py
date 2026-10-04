"""The first fixtures captured from real hardware, and the scrubbing they needed.

Everything in ``hw-*.ytx.pub`` came off a WS-C2950G-24-EI and was scrubbed with
``ciscoyoke transcript scrub --source 'hardware: ...'``. These tests are what
earn that platform its marks in the README's support matrix, so they replay the
real bytes rather than describing them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciscoyoke.identify.facts import from_boot_banner
from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.schema import Direction, Record, Transcript, read
from ciscoyoke.transcript.scrub import scrub

FIXTURES = Path(__file__).parent / "fixtures"
HARDWARE = sorted(FIXTURES.glob("hw-*.ytx.pub"))


def replay(name: str) -> tuple[StateTracker, str]:
    transcript = read(FIXTURES / name, require_public=True)
    tracker = StateTracker()
    for record in transcript:
        if record.direction is Direction.RX:
            tracker.feed(record.data)
        else:
            tracker.note_sent(record.data)
    return tracker, tracker.buffer.text


# -- provenance ---------------------------------------------------------------


def test_there_are_hardware_fixtures() -> None:
    assert len(HARDWARE) >= 3


@pytest.mark.parametrize("path", HARDWARE, ids=lambda p: p.name)
def test_every_hardware_fixture_says_so(path: Path) -> None:
    transcript = read(path, require_public=True)
    assert transcript.source is not None
    assert transcript.source.startswith("hardware: WS-C2950G-24-EI")


@pytest.mark.parametrize("path", HARDWARE, ids=lambda p: p.name)
def test_no_unit_identity_survived_scrubbing(path: Path) -> None:
    text = "".join(r.data.decode("latin-1") for r in read(path, require_public=True))
    assert "FOC0" not in text  # the chassis serial
    assert "00:0a:8a" not in text.lower()  # the base MAC
    assert "0a8adb" not in text.lower()  # the same MAC, unseparated (SNMP engine ID)


# -- replay: what the real device did ---------------------------------------------


def test_a_cold_boot_names_the_model_before_any_login() -> None:
    tracker, text = replay("hw-switch-2950-cold-boot-locked.ytx.pub")
    assert from_boot_banner(text).model.value == "WS-C2950G-24-EI"
    assert State.PRESS_RETURN in tracker.states


def test_the_no_restore_recovery_reaches_an_unlocked_privileged_prompt() -> None:
    """The switch had a console login (shown by an `intake` just before). The
    operator was already power-cycling it when this run began, so the recording
    opens on the bootloader banner rather than on ``Username:``."""
    tracker, text = replay("hw-switch-2950-recover-no-restore.ytx.pub")
    states = tracker.states
    assert State.BOOTLOADER in states
    assert State.SETUP_DIALOG in states  # booted with no configuration
    assert states[-1] is State.PRIV_EXEC
    # Moved aside, confirmed from a fresh listing, never moved back.
    assert "config.text.ciscoyoke" in text
    assert "rename flash:config.text.ciscoyoke flash:config.text" not in text


def test_the_bootloader_prompt_uses_lf_cr_line_endings() -> None:
    """The first real-hardware defect: every synthetic fixture used CRLF."""
    _, text = replay("hw-switch-2950-recover-no-restore.ytx.pub")
    assert "\n\rswitch: " in text


def test_the_syslog_line_that_hid_a_question_no_longer_hides_it() -> None:
    """The recording ends mid-stall: IOS asked for the destination filename and
    a link-state message landed after it. Replayed now, the question is seen."""
    tracker, text = replay("hw-switch-2950-recover-rename-hidden-by-syslog.ytx.pub")
    assert "Destination filename [config.text]?" in text
    assert "%LINEPROTO-5-UPDOWN" in text
    assert tracker.states[-1] is State.FILENAME_PROMPT


def test_a_reset_erases_a_lab_configuration_and_comes_back_empty() -> None:
    """Recorded against a made-up configuration loaded for the purpose, so the
    archive step reads nothing that belongs to anyone."""
    tracker, text = replay("hw-switch-2950-reset-lab-config.ytx.pub")
    states = tracker.states
    assert State.PRIV_EXEC in states
    assert State.FILENAME_PROMPT in states  # `Delete filename [vlan.dat]?`
    assert State.BOOTING in states
    assert State.SETUP_DIALOG in states  # nothing left to boot into
    assert states[-1] is State.PRESS_RETURN
    assert "description lab-only port" in text  # the configuration it erased
    erase = text.index("write erase")
    delete = text.index("delete flash:vlan.dat")
    reload = text.index("device#reload")
    assert erase < delete < reload
    # The listing taken after the deletion no longer has the VLAN database.
    listing = text[delete:reload].split("dir flash:", 1)[1]
    assert "config.text" not in listing
    assert "vlan.dat" not in listing


# -- scrubbing lessons from the same device -------------------------------------


def transcript_of(*chunks: bytes) -> Transcript:
    return Transcript(
        records=tuple(Record(float(i), Direction.RX, c) for i, c in enumerate(chunks))
    )


def scrubbed_text(*chunks: bytes) -> str:
    result = scrub(transcript_of(*chunks))
    return "".join(r.data.decode("latin-1") for r in result.transcript)


def test_a_secret_split_across_serial_reads_is_still_redacted() -> None:
    text = scrubbed_text(b"username admin secret 5 $1$wJ", b"fp$gBUc8P1RBxm0\r\n")
    assert "$1$" not in text
    assert "gBUc8" not in text


def test_a_radius_key_after_port_options_is_redacted() -> None:
    text = scrubbed_text(
        b"radius-server host 192.0.2.10 auth-port 1812 acct-port 1813 key S3cr3t#\r\n"
    )
    assert "S3cr3t#" not in text
    assert "key <redacted>" in text


def test_a_banner_body_is_removed_whatever_it_says() -> None:
    text = scrubbed_text(
        b"banner exec ^C\r\n*School Name:  Example Academy *\r\n"
        b"*Address: 1 High Street     *\r\n^C\r\n"
    )
    assert "Example Academy" not in text
    assert "High Street" not in text
    assert "<banner removed>" in text


def test_a_hostname_seen_in_a_log_line_is_replaced_everywhere() -> None:
    text = scrubbed_text(
        b"\r\nSITE123_SW con0 is now available\r\n",
        b"\r\nSITE123_SW>enable\r\nSITE123_SW#",
    )
    assert "SITE123" not in text


def test_serials_and_macs_are_anonymised() -> None:
    text = scrubbed_text(
        b"System serial number: ABC1234D567\r\n",
        b"MAC_ADDR=00:1B:2C:3D:4E:5F\n\rBase ethernet MAC Address: 001b.2c3d.4e5f\r\n",
    )
    assert "ABC1234D567" not in text
    assert "3D:4E:5F" not in text.upper()
    assert "2c3d" not in text


def test_a_mac_inside_an_snmp_engine_id_is_anonymised() -> None:
    text = scrubbed_text(b"snmp-server engineID local 800000090300001B2C3D4E5F\r\n")
    assert "1B2C3D4E5F" not in text.upper()
    assert "engineID local 80000009030000005E005301" in text


def test_generic_names_are_left_alone() -> None:
    assert "Switch#" in scrubbed_text(b"\r\nSwitch#")
