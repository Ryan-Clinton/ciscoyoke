"""Image recovery drivers: Catalyst bootloader and router ROMMON are separate programs.

Each platform gets its own commands and its own flash-listing parser, and a
device in a state neither understands is refused rather than guessed at.
"""

from __future__ import annotations

import pytest

from ciscoyoke.image.drivers import (
    CatalystBootloaderDriver,
    Family,
    NoDriverError,
    RouterRommonDriver,
    classify,
    driver_for,
)
from ciscoyoke.stream.tracker import State


def test_the_xmodem_destination_carries_a_filename() -> None:
    """`copy xmodem: flash:` with no name is rejected by the bootloader.

    Cisco documents `copy xmodem: flash:c2955-i6q4l2-mz.121-13.EA1.bin`.

    Asserted on the bytes the driver produces, not on the source text.
    """
    command = CatalystBootloaderDriver().transfer_command("c2950-foo.bin", 115200)

    assert command == b"copy xmodem: flash:c2950-foo.bin\r"


def test_a_router_in_rommon_gets_rommon_commands_not_catalyst_ones() -> None:
    """The two are different programs, not two dialects of one.

    Sending `set BAUD` and `copy xmodem: flash:` to a ROMMON prompt produces a
    device that ignores them -- and the flash listing shapes differ too, so a
    Catalyst parser yields an empty inventory that the planner reads as "no
    images present".
    """
    rommon = RouterRommonDriver()

    # No inline speed flag. An earlier version emitted `-s115200` while
    # reporting can_change_baud=False, so the host never followed and the
    # router would have transmitted at 115200 into a port listening at 9600.
    # Cisco changes the ROMMON rate through confreg and a reconnect, not a
    # transfer flag -- so until a real router proves otherwise, transfers run
    # at the current rate.
    assert rommon.transfer_command("c2600-i-mz.bin", 115200) == (
        b"xmodem -c c2600-i-mz.bin\r"
    )
    assert rommon.transfer_command("c2600-i-mz.bin", 9600) == (
        b"xmodem -c c2600-i-mz.bin\r"
    )
    assert rommon.set_baud_command(115200) is None
    assert rommon.can_change_baud is False


def test_each_platform_parses_its_own_flash_listing() -> None:
    catalyst_output = (
        "Directory of flash:/\n"
        "    2  -rwx     5001728   Mar 01 1993 00:01:24  c2950-i6q4l2-mz.bin\n"
        "7741440 bytes total (2739712 bytes free)\n"
    )
    rommon_output = (
        "File size           Checksum   File name\n"
        "5358032 bytes (0x51c1d0)   0x7b16    c2600-i-mz.122-10b.bin\n"
    )

    catalyst = CatalystBootloaderDriver().parse_inventory(catalyst_output)
    rommon = RouterRommonDriver().parse_inventory(rommon_output)

    assert [f.name for f in catalyst.images] == ["c2950-i6q4l2-mz.bin"]
    assert catalyst.free_bytes == 2739712
    assert [f.name for f in rommon.images] == ["c2600-i-mz.122-10b.bin"]
    assert rommon.images[0].size == 5358032

    # The crucial half: each parser must NOT silently read the other's output
    # as an empty inventory, which the planner would act on.
    assert CatalystBootloaderDriver().parse_inventory(rommon_output).images == ()


def test_a_device_in_an_unsupported_state_is_refused_a_driver() -> None:
    with pytest.raises(NoDriverError, match="no verified image-recovery"):
        driver_for(State.LOGIN_PASSWORD, None)


def test_a_booted_device_is_told_it_does_not_need_a_rescue() -> None:
    with pytest.raises(NoDriverError, match="does not need an image rescue"):
        driver_for(State.PRIV_EXEC, "WS-C2950-24")


def test_state_selects_the_platform_family() -> None:
    assert classify(State.BOOTLOADER, None) is Family.CATALYST_BOOTLOADER
    assert classify(State.ROMMON, None) is Family.ROUTER_ROMMON
    assert classify(State.PRIV_EXEC, None) is Family.BOOTED_IOS
    assert classify(State.LOGIN_PASSWORD, None) is Family.UNSUPPORTED


def test_the_two_platforms_produce_different_commands() -> None:
    catalyst = CatalystBootloaderDriver()
    rommon = RouterRommonDriver()

    assert catalyst.set_baud_command(115200) == b"set BAUD 115200\r"
    assert rommon.set_baud_command(115200) is None

    # `unset BAUD` returns the platform default, which is only correct when
    # that is where the session began. A non-default starting rate is named
    # explicitly so both ends end up agreeing.
    assert catalyst.restore_baud_command(9600) == b"unset BAUD\r"
    assert catalyst.restore_baud_command(19200) == b"set BAUD 19200\r"
    assert rommon.restore_baud_command(9600) is None

    assert b"copy xmodem:" in catalyst.transfer_command("a.bin", 115200)
    assert b"xmodem -c" in rommon.transfer_command("a.bin", 9600)


def test_catalyst_mounts_flash_before_listing_it() -> None:
    commands = CatalystBootloaderDriver().inventory_command()
    assert commands[0] == b"flash_init\r"
    assert b"dir flash:\r" in commands

    # ROMMON needs no mount step.
    assert RouterRommonDriver().inventory_command() == (b"dir flash:\r",)
