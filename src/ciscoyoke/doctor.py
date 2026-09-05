"""Host-side diagnostics.

Everything a recovery depends on that is *not* the device: serial permissions,
adapter identity, break support, and whether the embedded TFTP server can bind
its port at all.

The rule this module exists to enforce: capability is **probed and reported**,
never assumed. A recovery that discovers a missing capability halfway through
leaves the device in an intermediate state, so the discovery happens here
instead, before anything is touched.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from enum import StrEnum

from ciscoyoke.transport.identity import PortIdentity, enumerate_ports, find_ambiguities

TFTP_PORT = 69


class CheckStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: CheckStatus
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status.value, "detail": self.detail}


def check_serial_library() -> Check:
    try:
        import serial
    except ImportError:
        return Check("pyserial", CheckStatus.FAILED, "not installed")
    return Check("pyserial", CheckStatus.OK, f"version {serial.__version__}")


def check_ports() -> tuple[Check, tuple[PortIdentity, ...]]:
    identities = enumerate_ports()
    if not identities:
        return (
            Check(
                "serial ports",
                CheckStatus.UNKNOWN,
                "no serial ports found - check the adapter is plugged in",
            ),
            (),
        )

    ambiguous = find_ambiguities(identities)
    if ambiguous:
        groups = "; ".join(
            ", ".join(identity.device for identity in group) for group in ambiguous
        )
        return (
            Check(
                "serial ports",
                CheckStatus.OK,
                f"{len(identities)} found; indistinguishable: {groups}",
            ),
            identities,
        )
    return Check("serial ports", CheckStatus.OK, f"{len(identities)} found"), identities


def check_port_permissions(identities: tuple[PortIdentity, ...]) -> Check:
    """On POSIX, report whether the ports are actually readable and writable.

    Reports what is needed rather than instructing anyone to run as root: this
    process handles transcripts, journals and firmware images, and none of that
    should be elevated.
    """
    if os.name == "nt":
        return Check(
            "port permissions",
            CheckStatus.UNKNOWN,
            "not applicable on Windows (governed by device ACLs)",
        )
    if not identities:
        return Check("port permissions", CheckStatus.UNKNOWN, "no ports to check")

    denied = [
        identity.device
        for identity in identities
        if not os.access(identity.device, os.R_OK | os.W_OK)
    ]
    if denied:
        return Check(
            "port permissions",
            CheckStatus.FAILED,
            f"no read/write access to {', '.join(denied)} - "
            f"add your user to the dialout group rather than using sudo",
        )
    return Check("port permissions", CheckStatus.OK, "read/write on all ports")


def check_tftp_bind(port: int = TFTP_PORT) -> Check:
    """Probe whether the embedded TFTP server could bind its port.

    UDP 69 is privileged on POSIX, so this commonly fails for an unprivileged
    user. That is a *reportable condition with alternatives*, not an instruction
    to escalate: the fallbacks are an external TFTP server, or XMODEM.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", port))
    except PermissionError:
        return Check(
            f"UDP/{port} bind",
            CheckStatus.FAILED,
            "permission denied - use an external TFTP server or XMODEM",
        )
    except OSError as exc:
        return Check(f"UDP/{port} bind", CheckStatus.FAILED, str(exc))
    else:
        return Check(f"UDP/{port} bind", CheckStatus.OK, "available")
    finally:
        probe.close()


def check_firewall() -> Check:
    """Host firewall state.

    Reported as unknown, deliberately. TFTP recovery failing because a host
    firewall silently dropped UDP is a well-documented Cisco support scenario,
    and there is no portable way to determine the answer. Saying so is more
    useful than guessing, and it makes the firewall the first thing named when
    a transfer stalls.
    """
    return Check(
        "firewall",
        CheckStatus.UNKNOWN,
        "not portably determinable - check first if a TFTP transfer stalls",
    )


@dataclass(frozen=True, slots=True)
class Diagnosis:
    checks: tuple[Check, ...]
    ports: tuple[PortIdentity, ...]

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.FAILED)

    @property
    def embedded_tftp_available(self) -> bool:
        return any(
            c.name.startswith("UDP/") and c.status is CheckStatus.OK
            for c in self.checks
        )

    def to_json(self) -> dict[str, object]:
        return {
            "checks": [check.to_json() for check in self.checks],
            "embedded_tftp_available": self.embedded_tftp_available,
        }


def run() -> Diagnosis:
    """Run every host check."""
    library = check_serial_library()
    ports_check, identities = check_ports()
    return Diagnosis(
        checks=(
            library,
            ports_check,
            check_port_permissions(identities),
            check_tftp_bind(),
            check_firewall(),
        ),
        ports=identities,
    )
