"""One owner per physical transport.

Two ciscoyoke processes on one device is a corruption scenario: two writers
interleaving on a single console, and two journals each recording half the
truth. It is also easy to arrive at by accident -- a forgotten background
process, or two terminals and a moment's inattention.

The lease is keyed on **transport identity**, not port name. Port names are
assigned in enumeration order and shuffle between reboots, so two labels can
resolve to the same physical adapter; keying on ``PortIdentity.key`` means such
a pair collides correctly, where port-name locking would let both through.

A lease held by a process that no longer exists is reclaimed automatically. A
crash must never leave a device requiring manual unlocking -- that turns a
recoverable interruption into a support problem, and teaches people to delete
lock files, which defeats the mechanism entirely.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from ciscoyoke.journal.paths import locks_dir


class TargetBusyError(RuntimeError):
    """Another session holds the lease on this transport."""

    def __init__(self, holder: LeaseInfo) -> None:
        self.holder = holder
        super().__init__(
            f"target is already in use\n"
            f"  PID       {holder.pid}\n"
            f"  Command   {holder.operation}\n"
            f"  Started   {holder.started_display}\n"
            f"  Journal   {holder.run_id or 'not yet created'}\n"
            f"  Held by   {holder.target_name}"
        )


@dataclass(frozen=True, slots=True)
class LeaseInfo:
    """Who holds a lease, and on what."""

    target_key: str
    target_name: str
    pid: int
    operation: str
    started_at: float
    run_id: str | None = None

    @property
    def started_display(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.started_at))

    def to_json(self) -> dict[str, Any]:
        return {
            "target_key": self.target_key,
            "target_name": self.target_name,
            "pid": self.pid,
            "operation": self.operation,
            "started_at": self.started_at,
            "run_id": self.run_id,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> LeaseInfo:
        return cls(
            target_key=str(obj["target_key"]),
            target_name=str(obj.get("target_name", obj["target_key"])),
            pid=int(obj["pid"]),
            operation=str(obj.get("operation", "unknown")),
            started_at=float(obj.get("started_at", 0.0)),
            run_id=obj.get("run_id"),
        )


def process_alive(pid: int) -> bool:
    """Whether a process with this PID currently exists.

    Deliberately conservative: anything ambiguous is treated as alive, because
    wrongly declaring a live process dead would break the one invariant this
    module exists to hold. The cost of the opposite error is a stale lease the
    user can see and reason about.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it just belongs to someone else.
        return True
    except OSError:  # pragma: no cover - defensive
        return True
    return True


def _alive_windows(pid: int) -> bool:
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259

    # windll exists only on Windows; this branch is guarded by os.name.
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == still_active
        return True  # pragma: no cover - defensive
    finally:
        kernel32.CloseHandle(handle)


def _sanitise(key: str) -> str:
    """A filesystem-safe name for a transport key."""
    return "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in key)[:120]


class Lease:
    """An exclusive claim on one transport, for the life of an operation."""

    def __init__(
        self,
        target_key: str,
        *,
        target_name: str = "",
        operation: str = "unknown",
        directory: Path | None = None,
    ) -> None:
        self.target_key = target_key
        self.target_name = target_name or target_key
        self.operation = operation
        self._directory = directory if directory is not None else locks_dir()
        self._path = self._directory / f"{_sanitise(target_key)}.lock"
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._held

    def read(self) -> LeaseInfo | None:
        """Whoever currently holds this lease, if anyone."""
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return LeaseInfo.from_json(payload)
        except (KeyError, TypeError, ValueError):
            # A corrupt lock file is not a valid claim on anything.
            return None

    def acquire(self, *, run_id: str | None = None) -> LeaseInfo:
        """Claim the transport, or raise :class:`TargetBusyError`.

        Exclusivity does not depend on which process is asking. Two ``Lease``
        objects for one transport inside a single process is the double-open
        bug this exists to catch, so a same-PID holder blocks exactly like any
        other. Re-entrancy is a property of *this object* -- re-acquiring a
        lease already held here is a no-op -- rather than of the PID.
        """
        if self._held:
            current = self.read()
            if current is not None and current.pid == os.getpid():
                return current

        existing = self.read()
        if existing is not None:
            if process_alive(existing.pid):
                raise TargetBusyError(existing)
            # Stale: the holder is gone, so the claim goes with it.
            self._path.unlink(missing_ok=True)

        info = LeaseInfo(
            target_key=self.target_key,
            target_name=self.target_name,
            pid=os.getpid(),
            operation=self.operation,
            started_at=time.time(),
            run_id=run_id,
        )

        self._directory.mkdir(parents=True, exist_ok=True)
        # O_EXCL makes the claim atomic against another process racing us
        # between the check above and the write below.
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Another process won the race between the check above and here.
            holder = self.read()
            if holder is not None and process_alive(holder.pid):
                raise TargetBusyError(holder) from None
            self._path.unlink(missing_ok=True)
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(info.to_json(), handle)
        self._held = True
        return info

    def release(self) -> None:
        """Give up the claim, but only if it is still ours."""
        if not self._held:
            return
        current = self.read()
        if current is not None and current.pid == os.getpid():
            self._path.unlink(missing_ok=True)
        self._held = False

    def __enter__(self) -> Lease:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
