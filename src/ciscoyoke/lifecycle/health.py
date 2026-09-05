"""Is this box actually worth keeping?

Fifteen-year-old hardware fails in specific, recognisable ways, and the useful
question after a rescue is not "did it boot" but "will it still be booting next
week". This checks the handful of things that are both readable from the console
and actually predictive: POST results, flash health, memory, and the
configuration register.

Every check reports `unknown` rather than guessing. A device that does not
support ``show post`` has not passed or failed it, and recording a pass there
would make the whole report worthless -- the point of a health check is to be
believed when it says something is wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ciscoyoke.identify.facts import DeviceFacts

_POST_FAIL = re.compile(r"POST:.*?(fail|error)", re.IGNORECASE)
_POST_PASS = re.compile(r"POST:.*?(pass|ok)", re.IGNORECASE)
_FLASH_ERROR = re.compile(
    r"(flashfs\[\d+\]:.*?error|Error initializing flash|bad sectors?)",
    re.IGNORECASE,
)
_MEMORY_ERROR = re.compile(
    r"(memory (?:test )?(?:failure|error)|parity error|ECC error)", re.IGNORECASE
)
_ENV_ALARM = re.compile(
    r"(fan.*?(fail|not rotating)|temperature.*?(alarm|critical)|"
    r"power supply.*?(fail|not present))",
    re.IGNORECASE,
)

#: A router booting with this ignores its configuration on every restart. Not a
#: hardware fault, but the single most common way a "repaired" device gets
#: returned as still broken.
BYPASS_REGISTER = "0x2142"


class Health(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAULTY = "faulty"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    health: Health
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"check": self.name, "health": self.health.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class HealthReport:
    checks: tuple[Check, ...]

    @property
    def worst(self) -> Health:
        for level in (Health.FAULTY, Health.DEGRADED, Health.OK):
            if any(check.health is level for check in self.checks):
                return level
        return Health.UNKNOWN

    @property
    def faults(self) -> tuple[Check, ...]:
        return tuple(
            c for c in self.checks if c.health in (Health.FAULTY, Health.DEGRADED)
        )

    def render(self) -> str:
        marks = {
            Health.OK: "ok",
            Health.DEGRADED: "WARN",
            Health.FAULTY: "FAIL",
            Health.UNKNOWN: "?",
        }
        lines = ["Health", ""]
        for check in self.checks:
            lines.append(f"  {marks[check.health]:<6} {check.name:<20} {check.detail}")
        lines += ["", f"Overall: {self.worst.value}"]
        if self.worst is Health.UNKNOWN:
            lines.append(
                "Nothing could be established. This is not a clean bill of health."
            )
        return "\n".join(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "overall": self.worst.value,
            "checks": [check.to_json() for check in self.checks],
        }


def assess(boot_output: str, facts: DeviceFacts) -> HealthReport:
    """Read health from what the device said while booting, plus its facts.

    Boot output is used because it needs no authentication -- which means a
    locked device can still be assessed, and that is exactly the device somebody
    is deciding whether to keep.
    """
    checks: list[Check] = [
        _post(boot_output),
        _flash(boot_output),
        _memory(boot_output),
        _environment(boot_output),
        _config_register(facts),
        _dram(facts),
    ]
    return HealthReport(tuple(checks))


def _post(output: str) -> Check:
    if _POST_FAIL.search(output):
        return Check("power-on self test", Health.FAULTY, "POST reported a failure")
    if _POST_PASS.search(output):
        return Check("power-on self test", Health.OK, "POST passed")
    return Check(
        "power-on self test",
        Health.UNKNOWN,
        "no POST result seen in the captured boot output",
    )


def _flash(output: str) -> Check:
    match = _FLASH_ERROR.search(output)
    if match:
        return Check("flash", Health.FAULTY, f"flash reported: {match.group(1)}")
    if "flashfs[" in output.lower() or "initializing flash" in output.lower():
        return Check("flash", Health.OK, "flash initialised without error")
    return Check("flash", Health.UNKNOWN, "flash initialisation not observed")


def _memory(output: str) -> Check:
    match = _MEMORY_ERROR.search(output)
    if match:
        return Check("memory", Health.FAULTY, f"reported: {match.group(1)}")
    return Check("memory", Health.UNKNOWN, "no memory diagnostics in boot output")


def _environment(output: str) -> Check:
    match = _ENV_ALARM.search(output)
    if match:
        return Check("environment", Health.DEGRADED, f"reported: {match.group(1)}")
    return Check(
        "environment", Health.UNKNOWN, "no fan or temperature alarms observed"
    )


def _config_register(facts: DeviceFacts) -> Check:
    register = facts.config_register.value
    if register is None:
        return Check("config register", Health.UNKNOWN, "not read")
    if register.lower() == BYPASS_REGISTER:
        return Check(
            "config register",
            Health.DEGRADED,
            f"{register}: this device boots ignoring its startup configuration, "
            f"which is almost certainly left over from a password recovery",
        )
    return Check("config register", Health.OK, register)


def _dram(facts: DeviceFacts) -> Check:
    dram = facts.dram_kb.value
    if dram is None:
        return Check("installed memory", Health.UNKNOWN, "not read")
    megabytes = int(dram) / 1024
    if megabytes < 32:
        return Check(
            "installed memory",
            Health.DEGRADED,
            f"{megabytes:.0f} MB: too little for most modern IOS images on this "
            f"vintage of hardware",
        )
    return Check("installed memory", Health.OK, f"{megabytes:.0f} MB")
