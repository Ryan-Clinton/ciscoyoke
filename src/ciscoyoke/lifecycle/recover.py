"""Choosing and running the right recovery for a platform.

A thin layer, deliberately. The knowledge lives in the playbooks; this decides
which one applies and makes sure the physical prerequisite happens first.

The router and switch paths differ in a way that matters to the caller. A router
can be driven entirely from the console once someone power-cycles it, because
the break is electrical. A Catalyst cannot: the bootloader is reached only by
holding the Mode button, so the switch path *always* contains a human step and
there is no configuration in which it does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ciscoyoke.playbook import human, ios_router, ios_switch
from ciscoyoke.playbook.base import Playbook
from ciscoyoke.playbook.human import HumanStep
from ciscoyoke.stream.tracker import State


class Platform(StrEnum):
    ROUTER = "router"
    SWITCH = "switch"
    UNKNOWN = "unknown"


# Model prefixes that identify a fixed-configuration Catalyst.
_SWITCH_PREFIXES = ("WS-C", "CAT")
# IOS router families this project targets.
_ROUTER_PREFIXES = ("CISCO1", "CISCO2", "CISCO3", "C1700", "C2600", "C2800")


def classify(model: str | None) -> Platform:
    """Work out which recovery family a model belongs to."""
    if not model:
        return Platform.UNKNOWN
    upper = model.upper()
    if upper.startswith(_SWITCH_PREFIXES):
        return Platform.SWITCH
    if upper.startswith(_ROUTER_PREFIXES):
        return Platform.ROUTER
    return Platform.UNKNOWN


@dataclass(frozen=True, slots=True)
class RecoveryPath:
    """What recovering access to this device involves."""

    platform: Platform
    playbook: Playbook
    human_step: HumanStep | None
    note: str

    @property
    def needs_a_person(self) -> bool:
        return self.human_step is not None

    def render(self) -> str:
        lines = [
            f"Recovery path: {self.platform.value}",
            f"  {self.playbook.description}",
        ]
        if self.human_step is not None:
            lines += [
                "",
                "  Requires someone at the device:",
                f"    {self.human_step.instruction}",
            ]
        if self.note:
            lines += ["", f"  {self.note}"]
        return "\n".join(lines)


def path_for(model: str | None, state: State) -> RecoveryPath:
    """Pick a recovery path from what is actually known.

    An unknown model is not guessed at. Recovery procedures are destructive in
    different ways on the two families -- one changes a register, the other
    renames a file in flash -- and running the wrong one is worse than asking.
    """
    platform = classify(model)

    if platform is Platform.SWITCH:
        return RecoveryPath(
            platform=platform,
            playbook=ios_switch.full_access_recovery(),
            human_step=(
                None if state is State.BOOTLOADER else human.catalyst_mode_button()
            ),
            note=(
                "The configuration is renamed rather than erased, so the "
                "previous owner's settings survive and can be restored."
            ),
        )

    if platform is Platform.ROUTER:
        return RecoveryPath(
            platform=platform,
            playbook=(
                ios_router.bypass_startup_config()
                if state is State.ROMMON
                else ios_router.full_access_recovery()
            ),
            human_step=(
                None if state is State.ROMMON else human.router_break_window()
            ),
            note=(
                "Setting 0x2142 bypasses the startup configuration without "
                "erasing it; the configuration is copied back afterwards."
            ),
        )

    return RecoveryPath(
        platform=platform,
        playbook=Playbook(name="unknown", steps=()),
        human_step=None,
        note=(
            "The platform could not be identified, and the two recovery "
            "procedures are destructive in different ways. Identify the device "
            "first: ciscoyoke intake, or read the label on the chassis."
        ),
    )
