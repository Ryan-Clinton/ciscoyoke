"""The front door, health assessment, and recovery-path selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciscoyoke.identify.facts import from_show_version, unknown_facts
from ciscoyoke.identify.probe import intake
from ciscoyoke.lifecycle.health import Health, assess
from ciscoyoke.lifecycle.recover import Platform, classify, path_for
from ciscoyoke.lifecycle.rescue import Recommendation, rescue
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import Direction, Record, Transcript, read
from ciscoyoke.transport.base import TransportCapabilities
from ciscoyoke.transport.rfc2217 import RemoteTransport, RemoteUnavailableError

FIXTURES = Path(__file__).parent / "fixtures"


def session_for(name: str) -> Session:
    device = FakeDevice(read(FIXTURES / name, require_public=True), strict=False)
    return Session(
        FakeTransport(device), clock=device.monotonic, sleep=device.sleeper()
    )


# -- rescue: the front door ------------------------------------------------


def test_rescue_changes_nothing_destructive() -> None:
    report = rescue(session_for("router-1760-intake.ytx.pub"), "COM3")
    assert "Nothing destructive has happened." in report.render()


def self_test_failure_session() -> Session:
    output = (
        b"System Bootstrap, Version 12.3(8r)YH6, RELEASE SOFTWARE (fc1)\r\n"
        b"Failed all 0x00000000 test\r\n"
        b"Bad RAM at location 0x80000000: wrote 0x00000000, read 0xFFFF0000\r\n"
        b"DDR memory test failed.  Resetting the router ...\r\n"
    )
    device = FakeDevice(
        Transcript(records=(Record(0.0, Direction.RX, output),)),
        strict=False,
    )
    return Session(
        FakeTransport(device),
        clock=device.monotonic,
        sleep=device.sleeper(),
    )


def test_self_test_failure_is_reported_as_hardware_fault_not_cable() -> None:
    found = intake(self_test_failure_session())
    assert found.observation.state is State.SELF_TEST_FAILURE
    assert "hardware fault" in found.note
    assert "cable" in found.note  # explicitly says this is not a cable problem

    report = rescue(self_test_failure_session(), "COM8")
    assert report.recommendation is Recommendation.HARDWARE_FAULT
    assert "hardware fault" in report.reason
    assert "not a cable" in report.reason
    assert "Hardware fault" in report.next_command

    memory = next(c for c in report.health.checks if c.name == "memory")
    assert memory.health is Health.FAULTY


def test_a_bootloader_recommends_image_rescue() -> None:
    report = rescue(session_for("switch-2950-bootloader.ytx.pub"), "COM5")

    assert report.recommendation is Recommendation.IMAGE_RESCUE
    assert "recover image COM5" in report.next_command
    assert "never fetch one" in report.reason


def test_a_locked_device_recommends_access_recovery() -> None:
    report = rescue(session_for("switch-2950-locked.ytx.pub"), "COM4")

    assert report.recommendation is Recommendation.ACCESS_RECOVERY
    assert "recover access COM4" in report.next_command
    assert "Mode button" in report.reason


def test_recovery_disabled_recommends_stopping() -> None:
    """The one case where the tool declines to suggest a next step at all."""
    report = rescue(session_for("switch-2950-recovery-disabled.ytx.pub"), "COM5")

    assert report.recommendation is Recommendation.STOP
    assert "would destroy the startup configuration" in report.reason


def test_a_reachable_device_recommends_reset() -> None:
    report = rescue(session_for("router-1760-intake.ytx.pub"), "COM3")
    assert report.recommendation in (Recommendation.RESET, Recommendation.INVESTIGATE)


def test_the_report_shows_preservation_status() -> None:
    report = rescue(session_for("switch-2950-locked.ytx.pub"), "COM4")
    rendered = report.render()

    assert "Preserve available evidence" in rendered
    assert "PARTIAL" in rendered


def test_the_json_report_carries_the_gaps() -> None:
    payload = rescue(session_for("switch-2950-locked.ytx.pub"), "COM4").to_json()

    assert payload["preservation"] == "partial"
    assert "startup_config" in payload["preservation_gaps"]  # type: ignore[operator]
    assert payload["recommendation"] == "access_recovery"


# -- health ----------------------------------------------------------------


def test_a_clean_boot_reports_no_faults() -> None:
    session = session_for("router-1760-intake.ytx.pub")
    found = intake(session)
    report = assess(session.tracker.buffer.text, found.facts)

    assert report.faults == ()


def test_a_flash_error_is_a_fault() -> None:
    report = assess("flashfs[0]: error initializing flash", unknown_facts())
    flash = next(c for c in report.checks if c.name == "flash")

    assert flash.health is Health.FAULTY
    assert report.worst is Health.FAULTY


def test_a_post_failure_is_a_fault() -> None:
    report = assess("POST: Ethernet Loopback Test : FAIL", unknown_facts())
    assert report.worst is Health.FAULTY


def test_the_bypass_register_is_flagged_as_degraded() -> None:
    """The commonest way a 'repaired' device comes back still broken."""
    facts = from_show_version("Configuration register is 0x2142")
    report = assess("", facts)
    register = next(c for c in report.checks if c.name == "config register")

    assert register.health is Health.DEGRADED
    assert "ignoring its startup configuration" in register.detail


def test_a_normal_register_is_fine() -> None:
    facts = from_show_version("Configuration register is 0x2102")
    register = next(
        c for c in assess("", facts).checks if c.name == "config register"
    )
    assert register.health is Health.OK


def test_nothing_known_is_not_a_clean_bill_of_health() -> None:
    """The point of a health check is to be believed when it says something is
    wrong, which requires it to admit when it knows nothing."""
    report = assess("", unknown_facts())

    assert report.worst is Health.UNKNOWN
    assert "not a clean bill of health" in report.render()


def test_too_little_memory_is_degraded() -> None:
    facts = from_show_version(
        "cisco 1760 (MPC860P) processor with 16384K/2048K bytes of memory."
    )
    memory = next(
        c for c in assess("", facts).checks if c.name == "installed memory"
    )
    assert memory.health is Health.DEGRADED


# -- recovery path selection ------------------------------------------------


def test_platforms_are_classified() -> None:
    assert classify("WS-C2950-24") is Platform.SWITCH
    assert classify("CISCO1760") is Platform.ROUTER
    assert classify(None) is Platform.UNKNOWN
    assert classify("SOMETHING-ELSE") is Platform.UNKNOWN


def test_a_switch_recovery_always_needs_a_person() -> None:
    """There is no software route to a Catalyst bootloader."""
    path = path_for("WS-C2950-24", State.LOGIN_PASSWORD)

    assert path.needs_a_person is True
    assert path.human_step is not None
    assert "MODE button" in path.human_step.instruction


def test_a_switch_already_at_the_bootloader_needs_nobody() -> None:
    path = path_for("WS-C2950-24", State.BOOTLOADER)
    assert path.needs_a_person is False


def test_a_router_in_rommon_skips_the_break() -> None:
    path = path_for("CISCO1760", State.ROMMON)

    assert path.needs_a_person is False
    assert path.playbook.name == "bypass_startup_config"


def test_an_unknown_platform_is_not_guessed_at() -> None:
    """The two procedures are destructive in different ways; running the wrong
    one is worse than asking."""
    path = path_for(None, State.LOGIN_PASSWORD)

    assert path.platform is Platform.UNKNOWN
    assert path.playbook.steps == ()
    assert "Identify the device first" in path.note


def test_a_stated_platform_fills_in_for_a_locked_device() -> None:
    """A 2950 at ``Username:`` discloses nothing; the chassis label can."""
    path = path_for(None, State.LOGIN_USERNAME, platform="catalyst")

    assert path.platform is Platform.SWITCH
    assert path.needs_a_person is True


def test_a_stated_platform_that_contradicts_the_device_is_refused() -> None:
    path = path_for("CISCO1760", State.LOGIN_PASSWORD, platform="catalyst")

    assert path.platform is Platform.UNKNOWN
    assert path.playbook.steps == ()
    assert "disagree" in path.note


def test_a_stated_platform_that_agrees_changes_nothing() -> None:
    agreed = path_for("WS-C2950-24", State.BOOTLOADER, platform="catalyst")
    alone = path_for("WS-C2950-24", State.BOOTLOADER)

    assert agreed.platform is alone.platform is Platform.SWITCH
    assert agreed.playbook.name == alone.playbook.name
    assert agreed.note == alone.note


def test_an_unrecognised_stated_platform_is_an_error() -> None:
    with pytest.raises(ValueError, match="expected 'router' or 'catalyst'"):
        path_for(None, State.LOGIN_USERNAME, platform="firewall")


def test_the_switch_path_explains_it_preserves_the_config() -> None:
    path = path_for("WS-C2950-24", State.BOOTLOADER)
    assert "renamed rather than erased" in path.note


# -- remote transport -------------------------------------------------------


def test_a_bare_socket_cannot_break_or_change_baud() -> None:
    transport = RemoteTransport("socket://consolepi:7001")

    assert transport.controllable is False
    assert transport.capabilities.send_break.usable is False
    assert transport.capabilities.change_baud.usable is False


def test_rfc2217_carries_serial_control() -> None:
    transport = RemoteTransport("rfc2217://consolepi:7001")

    assert transport.controllable is True
    assert transport.capabilities.send_break.usable is True


def test_a_socket_refuses_a_break_loudly() -> None:
    """Silently doing nothing would look like a device that ignored the break."""
    transport = RemoteTransport("socket://consolepi:7001")

    with pytest.raises(RemoteUnavailableError, match="cannot send a break"):
        transport.send_break()


def test_remote_transports_are_never_encrypted() -> None:
    assert RemoteTransport("rfc2217://host:7001").encrypted is False


def test_a_bad_url_is_refused() -> None:
    with pytest.raises(RemoteUnavailableError, match="expected"):
        RemoteTransport("http://example.com")


def test_the_capability_sets_actually_differ() -> None:
    assert (
        TransportCapabilities.raw_socket().send_break.usable
        is not TransportCapabilities.rfc2217().send_break.usable
    )
