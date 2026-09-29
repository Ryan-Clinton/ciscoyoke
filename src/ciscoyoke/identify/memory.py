"""The model last seen on each console adapter.

A locked device identifies itself only while booting, and access recovery needs
the platform *before* the physical step, because the platform decides which
physical step to ask for: the Mode button for a Catalyst, a break for a router.
So identity seen during an earlier boot -- a ``capture``, an ``intake`` that
happened to catch a banner -- is remembered against the adapter it arrived on.

Remembered identity is **inferred**, never observed: the cable may have moved to
another device since. It is only used to choose the physical step, and that
step's own evidence confirms it -- a router cannot produce ``switch:`` -- before
anything on the device is changed.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from ciscoyoke.journal.paths import ensure_state_dir

MEMORY_FILE = "identities.json"


@dataclass(frozen=True, slots=True)
class Remembered:
    """A model seen on an adapter, and when and how."""

    model: str
    source: str
    seen_at: float

    def describe(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.seen_at))
        return f"{self.model} (seen on this adapter {when}, from {self.source})"


def _path() -> Path:
    return ensure_state_dir() / MEMORY_FILE


def _load() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(adapter_key: str, model: str, source: str) -> None:
    """Record that ``model`` identified itself on this adapter.

    Best effort: failing to remember must never fail the command that saw it.
    """
    data = _load()
    data[adapter_key] = {"model": model, "source": source, "seen_at": time.time()}
    with contextlib.suppress(OSError):
        _path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def recall(adapter_key: str) -> Remembered | None:
    entry = _load().get(adapter_key)
    if not isinstance(entry, dict) or not entry.get("model"):
        return None
    return Remembered(
        model=str(entry["model"]),
        source=str(entry.get("source", "unknown")),
        seen_at=float(entry.get("seen_at") or 0.0),  # type: ignore[arg-type]
    )
