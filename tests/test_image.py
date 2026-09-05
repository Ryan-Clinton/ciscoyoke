"""Image rescue: four kinds of verification, and the stranding rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciscoyoke.identify.facts import DeviceFacts, from_show_version, unknown_facts
from ciscoyoke.image.compat import Verdict, assess
from ciscoyoke.image.fingerprint import ImageUnreadableError, fingerprint
from ciscoyoke.image.plan import (
    ACCEPT_STRANDED_RISK,
    FlashFile,
    FlashInventory,
    Strategy,
    make_plan,
    override_stranded,
    parse_flash,
)
from ciscoyoke.image.tftp import (
    EmbeddedTftpProvider,
    ExternalTftpProvider,
    TftpUnavailableError,
    rommon_variables,
)
from ciscoyoke.playbook.transfer import Method, Progress, TransferStep, estimate
from ciscoyoke.stream.tracker import Confidence

SHOW_VERSION_2950 = """
Cisco IOS Software, C2950 Software (C2950-I6Q4L2-M), Version 12.1(22)EA14
cisco WS-C2950-24 (RC32300) processor (revision R0) with 20temp K bytes of memory.
Model number             : WS-C2950-24
System serial number     : FOC0912X1AB
Configuration register is 0x F
"""


def make_image(tmp_path: Path, name: str, size: int) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x7fELF" + b"\x00" * (size - 4))
    return path


# -- fingerprint: provenance only ------------------------------------------


def test_fingerprint_hashes_and_reads_the_filename(tmp_path: Path) -> None:
    path = make_image(tmp_path, "c2950-i6q4l2-mz.121-22.EA14.bin", 4096)
    result = fingerprint(path)

    assert len(result.sha256) == 64
    assert result.size == 4096
    assert result.platform == "c2950"
    assert result.feature_set == "i6q4l2"


def test_fingerprint_says_what_it_does_not_prove(tmp_path: Path) -> None:
    """The word 'verified' quietly grows to mean whatever the reader hopes."""
    path = make_image(tmp_path, "c2950-i6q4l2-mz.121-22.EA14.bin", 4096)
    note = str(fingerprint(path).to_json()["note"])

    assert "authenticity" in note
    assert "compatibility" in note
    assert "bootability" in note


def test_an_unconventional_filename_yields_no_platform(tmp_path: Path) -> None:
    path = make_image(tmp_path, "downloaded (3).bin", 4096)
    assert fingerprint(path).platform is None


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    with pytest.raises(ImageUnreadableError):
        fingerprint(path)


# -- compatibility: evidence, never assertion ------------------------------


def test_a_matching_platform_is_compatible(tmp_path: Path) -> None:
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.bin", 4096))
    facts = from_show_version(SHOW_VERSION_2950)

    result = assess(image, facts, flash_free_bytes=8192)

    assert result.verdict is Verdict.COMPATIBLE
    assert result.confidence is not Confidence.HIGH


def test_confidence_is_never_high_for_a_pass(tmp_path: Path) -> None:
    """A filename and a byte count cannot establish an image will boot."""
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.bin", 4096))
    facts = from_show_version(SHOW_VERSION_2950)

    result = assess(image, facts, flash_free_bytes=8192)
    assert result.confidence is not Confidence.HIGH


def test_wrong_platform_is_incompatible(tmp_path: Path) -> None:
    image = fingerprint(make_image(tmp_path, "c3560-ipbase-mz.122-25.bin", 4096))
    facts = from_show_version(SHOW_VERSION_2950)

    result = assess(image, facts, flash_free_bytes=8192)

    assert result.verdict is Verdict.INCOMPATIBLE
    assert result.blocking


def test_insufficient_flash_is_reported_not_judged(tmp_path: Path) -> None:
    """Capacity is a planning question, not a compatibility one.

    Treating "not enough room" as INCOMPATIBLE short-circuited make_plan before
    it could consider freeing space -- which made the deletion and stranding
    machinery unreachable whenever free space was known and insufficient.
    """
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.bin", 8192))
    facts = from_show_version(SHOW_VERSION_2950)

    result = assess(image, facts, flash_free_bytes=1024)

    assert result.verdict is not Verdict.INCOMPATIBLE
    space = next(e for e in result.evidence if e.check == "flash space")
    assert space.passed is None
    assert "make room" in space.detail


def test_the_stranding_branch_is_now_reachable(tmp_path: Path) -> None:
    """The point of the split: insufficient space reaches the planner.

    Previously this returned BLOCKED_INCOMPATIBLE and the stranding rule never
    ran at all.
    """
    from ciscoyoke.image.plan import FlashFile, FlashInventory, Strategy, make_plan

    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.bin", 5_500_000))
    facts = from_show_version(SHOW_VERSION_2950)
    inventory = FlashInventory(
        files=(FlashFile("c2950-old.bin", 5_000_000),), free_bytes=2_000_000
    )

    plan = make_plan(image, inventory, assess(image, facts, flash_free_bytes=2_000_000))

    assert plan.strategy is Strategy.BLOCKED_WOULD_STRAND


def test_unknown_device_yields_undetermined_not_compatible(tmp_path: Path) -> None:
    """Not knowing must never read as a pass."""
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.bin", 4096))

    result = assess(image, unknown_facts(), flash_free_bytes=None)

    assert result.verdict is Verdict.UNDETERMINED
    assert result.confidence is Confidence.LOW


def test_dram_is_always_reported_as_undeterminable(tmp_path: Path) -> None:
    """It is published in release notes, not encoded in the file."""
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.bin", 4096))
    result = assess(image, from_show_version(SHOW_VERSION_2950), flash_free_bytes=8192)

    dram = next(e for e in result.evidence if e.check == "DRAM requirement")
    assert dram.passed is None
    assert "release notes" in dram.detail


# -- flash planning: the stranding rule ------------------------------------


def compatible(tmp_path: Path, size: int) -> tuple:  # type: ignore[type-arg]
    image = fingerprint(make_image(tmp_path, "c2950-i6q4l2-mz.121-22.bin", size))
    return image, assess(
        image, from_show_version(SHOW_VERSION_2950), flash_free_bytes=10_000_000
    )


def test_deleting_the_only_bootable_image_is_blocked(tmp_path: Path) -> None:
    """The rule this module exists for.

    A failed transfer after deleting the last image leaves a device that cannot
    boot at all -- worse than the state the operator arrived with.
    """
    image, compat = compatible(tmp_path, 5_500_000)
    inventory = FlashInventory(
        files=(FlashFile("c2950-old.bin", 5_000_000),),
        free_bytes=2_000_000,
        total_bytes=8_000_000,
    )

    plan = make_plan(image, inventory, compat)

    assert plan.strategy is Strategy.BLOCKED_WOULD_STRAND
    assert plan.blocked is True
    assert "no rollback image" in plan.reason
    assert "--accept-stranded-risk" in plan.reason


def test_the_stranding_block_can_be_overridden_explicitly(tmp_path: Path) -> None:
    image, compat = compatible(tmp_path, 5_500_000)
    inventory = FlashInventory(
        files=(FlashFile("c2950-old.bin", 5_000_000),), free_bytes=2_000_000
    )

    overridden = override_stranded(make_plan(image, inventory, compat))

    assert overridden.strategy is Strategy.TRANSFER_AFTER_DELETING_SPARE
    assert "accepted explicitly" in overridden.reason

    mutations = overridden.mutations()
    assert mutations
    assert mutations[0].guard is not None
    assert "no rollback image remains during transfer" in mutations[0].destructive_effects


def test_a_spare_image_may_be_deleted_without_stranding(tmp_path: Path) -> None:
    image, compat = compatible(tmp_path, 5_500_000)
    inventory = FlashInventory(
        files=(
            FlashFile("c2950-old.bin", 5_000_000),
            FlashFile("c2950-spare.bin", 4_000_000),
        ),
        free_bytes=2_000_000,
    )

    plan = make_plan(image, inventory, compat)

    assert plan.strategy is Strategy.TRANSFER_AFTER_DELETING_SPARE
    assert "still leaves the device able to boot" in plan.reason


def test_sufficient_space_deletes_nothing(tmp_path: Path) -> None:
    image, compat = compatible(tmp_path, 1_000_000)
    inventory = FlashInventory(
        files=(FlashFile("c2950-old.bin", 5_000_000),), free_bytes=6_000_000
    )

    plan = make_plan(image, inventory, compat)

    assert plan.strategy is Strategy.TRANSFER_ALONGSIDE
    assert plan.delete == ()


def test_an_image_already_present_avoids_the_transfer(tmp_path: Path) -> None:
    """Cisco's own advice: look for a valid image before transferring."""
    image, compat = compatible(tmp_path, 4096)
    inventory = FlashInventory(
        files=(FlashFile("c2950-i6q4l2-mz.121-22.bin", 4096),), free_bytes=1_000_000
    )

    plan = make_plan(image, inventory, compat)

    assert plan.strategy is Strategy.ALREADY_PRESENT
    assert plan.requires_transfer is False


def test_an_incompatible_image_is_blocked_before_any_planning(tmp_path: Path) -> None:
    image = fingerprint(make_image(tmp_path, "c3560-ipbase-mz.122-25.bin", 4096))
    compat = assess(
        image, from_show_version(SHOW_VERSION_2950), flash_free_bytes=10_000_000
    )

    plan = make_plan(image, FlashInventory(free_bytes=10_000_000), compat)

    assert plan.strategy is Strategy.BLOCKED_INCOMPATIBLE


def test_unknown_free_space_does_not_delete_anything(tmp_path: Path) -> None:
    """Guessing here risks deleting an image to make room that already existed."""
    image, compat = compatible(tmp_path, 5_000_000)
    inventory = FlashInventory(files=(FlashFile("c2950-old.bin", 5_000_000),))

    plan = make_plan(image, inventory, compat)

    assert plan.delete == ()
    assert "could not be determined" in plan.reason


def test_flash_listing_is_parsed() -> None:
    output = """
Directory of flash:/

    2  -rwx     5001728   Mar 01 1993 00:01:24  c2950-i6q4l2-mz.121-22.EA14.bin
    3  -rwx         676   Mar 01 1993 00:02:11  vlan.dat

7741440 bytes total (2739712 bytes free)
"""
    inventory = parse_flash(output)

    assert len(inventory.images) == 1
    assert inventory.images[0].size == 5001728
    assert inventory.free_bytes == 2739712
    assert inventory.total_bytes == 7741440


def test_an_unreadable_listing_yields_unknown_not_an_exception() -> None:
    """A half-broken device is exactly what produces garbage here."""
    inventory = parse_flash("flashfs[0]: error initializing")

    assert inventory.images == ()
    assert inventory.free_bytes is None


# -- TFTP: constrained by construction -------------------------------------


def test_an_external_server_is_not_managed_here() -> None:
    endpoint = ExternalTftpProvider("192.168.1.5", "ios.bin").start()

    assert endpoint.embedded is False
    assert "not managed here" in endpoint.render()


def test_the_endpoint_states_its_constraints() -> None:
    rendered = ExternalTftpProvider("192.168.1.5", "ios.bin").start().render()

    assert "Write access" in rendered
    assert "disabled" in rendered
    assert "Directory traversal" in rendered


def test_a_missing_image_is_refused(tmp_path: Path) -> None:
    provider = EmbeddedTftpProvider(tmp_path / "absent.bin")
    with pytest.raises(TftpUnavailableError, match="not a file"):
        provider.start()


def test_rommon_variables_carry_the_addressing() -> None:
    endpoint = ExternalTftpProvider("192.168.1.5", "ios.bin").start()
    commands = rommon_variables(endpoint, "192.168.1.10", "255.255.255.0", "192.168.1.1")
    joined = b"".join(commands).decode()

    assert "IP_ADDRESS=192.168.1.10" in joined
    assert "TFTP_SERVER=192.168.1.5" in joined
    assert "TFTP_FILE=ios.bin" in joined
    assert "DEFAULT_GATEWAY=192.168.1.1" in joined


# -- transfer progress -----------------------------------------------------


def test_xmodem_at_9600_is_predicted_in_hours() -> None:
    """Cisco warns this can take two hours. The estimate must not hide it."""
    prediction = estimate(5_000_000, Method.XMODEM, 9600)
    assert prediction.seconds > 3600
    assert "hours" in prediction.human


def test_raising_the_baud_makes_it_minutes() -> None:
    prediction = estimate(5_000_000, Method.XMODEM, 115200)
    assert "minutes" in prediction.human


def test_an_early_eta_is_withheld_rather_than_guessed() -> None:
    """An ETA swinging from four minutes to four hours destroys trust in every
    later figure."""
    progress = Progress(total_bytes=5_000_000, sent_bytes=100, started_at=0.0, now=0.5)
    assert progress.remaining is None
    assert "estimating" in progress.render()


def test_a_settled_transfer_reports_a_remaining_time() -> None:
    progress = Progress(
        total_bytes=5_000_000, sent_bytes=1_000_000, started_at=0.0, now=100.0
    )
    assert progress.remaining is not None
    assert "min left" in progress.render()


def test_the_baud_change_is_a_journalled_mutation() -> None:
    """An interrupted transfer is exactly when someone needs to know the device
    is no longer at 9600."""
    step = TransferStep(Method.XMODEM, 5_000_000, "ios.bin")
    mutations = step.mutations()

    assert len(mutations) == 1
    assert mutations[0].kind == "console_baud"
    assert mutations[0].compensation == {"action": "set_baud", "baud": 9600}
    assert step.required_capabilities() == ("change_baud",)


def test_tftp_needs_no_baud_change() -> None:
    step = TransferStep(Method.TFTP, 5_000_000, "ios.bin")
    assert step.mutations() == ()
    assert step.required_capabilities() == ()


def test_accept_stranded_risk_guard_is_named() -> None:
    assert ACCEPT_STRANDED_RISK == "accept_stranded_risk"


def test_facts_helper_is_available() -> None:
    assert isinstance(unknown_facts(), DeviceFacts)
