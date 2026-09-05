"""Generate the synthetic transcript fixtures.

Deterministic: re-running this produces byte-identical files, so a regenerated
fixture creates no spurious diff and a real diff means something changed.

These transcripts are **constructed from Cisco's published output**, not
captured from hardware. Their `source` field says so, and the support matrix
treats them accordingly -- see tests/fixtures/README.md. Replacing one with a
real capture is a strict upgrade and the only thing that earns a hardware mark.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ciscoyoke.transcript.schema import (  # noqa: E402
    Direction,
    Record,
    Transcript,
    write,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

SYNTHETIC = "synthetic: constructed from Cisco published output, not captured"


def _t(events: list[tuple[float, Direction, bytes]]) -> Transcript:
    return Transcript(
        records=tuple(Record(t, direction, data) for t, direction, data in events),
        public=True,
        scrub_version=1,
        source=SYNTHETIC,
    )


# A 1760 booting to a user-exec prompt, then being interrogated.
ROUTER_INTAKE = _t(
    [
        (0.00, Direction.RX, b"\r\nSystem Bootstrap, Version 12.2(8r)T2, RELEASE SOFTWARE (fc1)\r\n"),
        (0.10, Direction.RX, b"Copyright (c) 2003 by cisco Systems, Inc.\r\n"),
        (0.20, Direction.RX, b"C1760 platform with 131072 Kbytes of main memory\r\n"),
        (1.50, Direction.RX, b"\r\nSelf decompressing the image : ###########\r\n"),
        (18.0, Direction.RX, b"\r\nPress RETURN to get started!\r\n"),
        (18.5, Direction.RX, b"\r\nRouter>"),
        (19.0, Direction.TX, b"terminal length 0\r"),
        (19.2, Direction.RX, b"terminal length 0\r\nRouter>"),
        (19.5, Direction.TX, b"show version\r"),
        (
            20.0,
            Direction.RX,
            b"show version\r\n"
            b"Cisco IOS Software, C1700 Software (C1700-ADVIPSERVICESK9-M), "
            b"Version 12.4(25c), RELEASE SOFTWARE (fc2)\r\n"
            b"Copyright (c) 1986-2011 by Cisco Systems, Inc.\r\n"
            b"\r\n"
            b"ROM: System Bootstrap, Version 12.2(8r)T2, RELEASE SOFTWARE (fc1)\r\n"
            b"\r\n"
            b"Router uptime is 3 minutes\r\n"
            b"System image file is \"flash:c1700-advipservicesk9-mz.124-25c.bin\"\r\n"
            b"\r\n"
            b"cisco 1760 (MPC860P) processor (revision 0x500) with 121856K/9216K bytes of memory.\r\n"
            b"Processor board ID FTX0812A1BC\r\n"
            b"32768K bytes of processor board System flash (Read/Write)\r\n"
            b"\r\n"
            b"Configuration register is 0x2102\r\n"
            b"\r\n"
            b"Router>",
        ),
    ]
)

# A 2950 sitting at a login prompt: locked, and identity therefore unavailable.
SWITCH_LOCKED = _t(
    [
        (0.00, Direction.RX, b"\r\n"),
        (0.50, Direction.TX, b"\r"),
        (0.80, Direction.RX, b"\r\n\r\nUser Access Verification\r\n\r\nUsername: "),
    ]
)

# A Catalyst stranded at the bootloader with no bootable image.
SWITCH_BOOTLOADER = _t(
    [
        (0.00, Direction.RX, b"\r\nBase ethernet MAC Address: 00:0b:be:2f:11:80\r\n"),
        (0.20, Direction.RX, b"Xmodem file system is available.\r\n"),
        (0.40, Direction.RX, b"The password-recovery mechanism is enabled.\r\n"),
        (1.00, Direction.RX, b"Initializing Flash...\r\nflashfs[0]: 1 files, 1 directories\r\n"),
        (2.00, Direction.RX, b"\r\nError loading \"flash:/c2950-i6q4l2-mz.bin\"\r\n"),
        (2.40, Direction.RX, b"\r\nswitch: "),
    ]
)

# The interlock case: recovery disabled, so interrupting the boot has a cost.
SWITCH_RECOVERY_DISABLED = _t(
    [
        (0.00, Direction.RX, b"\r\nBase ethernet MAC Address: 00:0b:be:2f:11:81\r\n"),
        (0.30, Direction.RX, b"Xmodem file system is available.\r\n"),
        (
            0.60,
            Direction.RX,
            b"WARNING: PASSWORD RECOVERY FUNCTIONALITY IS DISABLED\r\n",
        ),
        (1.20, Direction.RX, b"\r\nswitch: "),
    ]
)

# A router in ROMMON, the entry point for image rescue.
ROUTER_ROMMON = _t(
    [
        (0.00, Direction.RX, b"\r\nSystem Bootstrap, Version 12.2(8r)T2\r\n"),
        (0.40, Direction.RX, b"\r\nrommon 1 > "),
    ]
)

FILES = {
    "router-1760-intake.ytx.pub": ROUTER_INTAKE,
    "switch-2950-locked.ytx.pub": SWITCH_LOCKED,
    "switch-2950-bootloader.ytx.pub": SWITCH_BOOTLOADER,
    "switch-2950-recovery-disabled.ytx.pub": SWITCH_RECOVERY_DISABLED,
    "router-1760-rommon.ytx.pub": ROUTER_ROMMON,
}


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, transcript in FILES.items():
        write(FIXTURES / name, transcript)
        print(f"wrote {name} ({len(transcript)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
