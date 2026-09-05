"""Documented, stable exit codes.

These are part of the public interface. A shell script, a CI job or an Ansible
wrapper branches on them, so they are covered by tests and changing one is a
breaking change.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    DEVICE_NOT_FOUND = 10
    STATE_UNCERTAIN = 11
    AUTHENTICATION_REQUIRED = 20
    DESTRUCTIVE_ACTION_REFUSED = 30
    TRANSPORT_CAPABILITY_UNAVAILABLE = 40
    TRANSFER_FAILED = 50
    INTERRUPTED_RECOVERY = 60
    TARGET_LEASE_HELD = 70

    @property
    def explanation(self) -> str:
        return _EXPLANATIONS[self]


_EXPLANATIONS: dict[ExitCode, str] = {
    ExitCode.SUCCESS: "success",
    ExitCode.DEVICE_NOT_FOUND: "device not found",
    ExitCode.STATE_UNCERTAIN: "state uncertain",
    ExitCode.AUTHENTICATION_REQUIRED: "authentication required",
    ExitCode.DESTRUCTIVE_ACTION_REFUSED: "destructive action refused",
    ExitCode.TRANSPORT_CAPABILITY_UNAVAILABLE: "transport capability unavailable",
    ExitCode.TRANSFER_FAILED: "transfer failed",
    ExitCode.INTERRUPTED_RECOVERY: "interrupted recovery requires resolution",
    ExitCode.TARGET_LEASE_HELD: "target lease held by another session",
}
