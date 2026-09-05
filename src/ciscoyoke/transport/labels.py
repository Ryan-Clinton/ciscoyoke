"""Human names for physical ports.

``COM3`` today, ``COM5`` tomorrow. A label like ``bench-left`` survives that,
because it binds to the adapter's USB identity rather than to whatever name the
OS assigned this boot.

Labels are refused where they cannot be kept. An adapter with no serial number
and no location is indistinguishable from an identical twin, so naming one would
create exactly the false confidence that leads to erasing the wrong device --
and the whole point of a label is to be trusted later, when nobody remembers
which cable went where.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ciscoyoke.journal.paths import ensure_state_dir
from ciscoyoke.transport.identity import (
    IdentityStrength,
    PortIdentity,
    enumerate_ports,
)

LABELS_FILE = "labels.json"


class LabelRefusedError(ValueError):
    """A label cannot be bound to this port."""


@dataclass(frozen=True, slots=True)
class Label:
    name: str
    key: str
    adapter: str = ""
    note: str = ""

    def to_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "key": self.key,
            "adapter": self.adapter,
            "note": self.note,
        }


def labels_path() -> Path:
    return ensure_state_dir() / LABELS_FILE


def load() -> dict[str, Label]:
    """Every label, by name."""
    path = labels_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        name: Label(
            name=name,
            key=str(body.get("key", "")),
            adapter=str(body.get("adapter", "")),
            note=str(body.get("note", "")),
        )
        for name, body in raw.items()
        if isinstance(body, dict) and body.get("key")
    }


def save(labels: dict[str, Label]) -> None:
    path = labels_path()
    path.write_text(
        json.dumps(
            {name: label.to_json() for name, label in labels.items()}, indent=2
        ),
        encoding="utf-8",
    )


def assign(name: str, port: str) -> Label:
    """Bind a name to whatever adapter is at ``port`` right now.

    Refuses a port with no stable identity. A label that cannot be resolved
    again is worse than no label: it looks authoritative and points nowhere in
    particular.
    """
    identity = _find(port)
    if identity is None:
        raise LabelRefusedError(f"{port} is not present")

    if identity.strength is IdentityStrength.NAME_ONLY:
        raise LabelRefusedError(
            f"{port} reports neither a USB serial number nor a physical "
            f"location, so it cannot be recognised again after a replug. "
            f"Labelling it would create confidence the hardware cannot support."
        )

    labels = load()
    for existing in labels.values():
        if existing.key == identity.key and existing.name != name:
            raise LabelRefusedError(
                f"{port} is already labelled {existing.name!r}; remove that "
                f"label first"
            )

    note = (
        "bound to USB serial number"
        if identity.strength is IdentityStrength.SERIAL
        else "bound to physical port location; moving the cable to another "
        "socket will break this label"
    )
    label = Label(
        name=name, key=identity.key, adapter=identity.adapter, note=note
    )
    labels[name] = label
    save(labels)
    return label


def remove(name: str) -> bool:
    labels = load()
    if name not in labels:
        return False
    del labels[name]
    save(labels)
    return True


def resolve(name: str) -> PortIdentity | None:
    """Find the port a label currently points at, if it is plugged in."""
    label = load().get(name)
    if label is None:
        return None
    for identity in enumerate_ports():
        if identity.key == label.key:
            return identity
    return None


def name_for(identity: PortIdentity) -> str | None:
    """The label bound to this port, if any."""
    for label in load().values():
        if label.key == identity.key:
            return label.name
    return None


def resolve_target(target: str) -> str:
    """Turn a label or a port name into a port name.

    Lets every command take ``bench-left`` wherever it takes ``COM3``, which is
    the point of labels existing at all.
    """
    identity = resolve(target)
    if identity is not None:
        return identity.device
    return target


def _find(port: str) -> PortIdentity | None:
    for identity in enumerate_ports():
        if identity.device == port:
            return identity
    return None
