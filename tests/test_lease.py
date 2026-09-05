"""One owner per transport, and no manual unlocking after a crash."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ciscoyoke.journal.lease import (
    Lease,
    LeaseInfo,
    TargetBusyError,
    process_alive,
)

KEY = "usb:0403:6001:A10K8Z4P"


def lease(tmp_path: Path, *, key: str = KEY, operation: str = "recover_image") -> Lease:
    return Lease(key, target_name="bench-left", operation=operation, directory=tmp_path)


def test_a_lease_can_be_taken_and_released(tmp_path: Path) -> None:
    held = lease(tmp_path)
    held.acquire()
    assert held.held is True
    assert held.path.exists()

    held.release()
    assert held.path.exists() is False


def test_a_second_session_is_refused(tmp_path: Path) -> None:
    """The invariant: at most one mutating session owns a transport."""
    first = lease(tmp_path)
    first.acquire(run_id="7f3a")

    second = lease(tmp_path, operation="reset")
    with pytest.raises(TargetBusyError) as excinfo:
        second.acquire()

    message = str(excinfo.value)
    assert "already in use" in message
    assert str(os.getpid()) in message
    assert "recover_image" in message
    assert "7f3a" in message


def test_the_lease_keys_on_transport_identity_not_port_name(tmp_path: Path) -> None:
    """Two labels resolving to one adapter must collide.

    Port names shuffle between reboots, so locking on `COM3` would let a second
    session through whenever the same physical adapter had been renamed -- which
    is precisely the situation the lock exists to prevent.
    """
    first = Lease(KEY, target_name="COM3", directory=tmp_path)
    second = Lease(KEY, target_name="bench-left", directory=tmp_path)

    first.acquire()
    with pytest.raises(TargetBusyError):
        second.acquire()


def test_different_devices_do_not_block_each_other(tmp_path: Path) -> None:
    first = Lease(KEY, directory=tmp_path)
    second = Lease("usb:0403:6001:DIFFERENT", directory=tmp_path)

    first.acquire()
    second.acquire()  # must not raise
    assert second.held is True


def test_a_lease_held_by_a_dead_process_is_reclaimed(tmp_path: Path) -> None:
    """A crash must never require manual unlocking.

    Otherwise people learn to delete lock files, which defeats the mechanism
    entirely -- and they will do it at exactly the wrong moment.
    """
    stale = LeaseInfo(
        target_key=KEY,
        target_name="bench-left",
        # PID 1 is init on POSIX and does not exist on Windows; either way this
        # PID cannot be a dead ciscoyoke. Use an implausible one instead.
        pid=0x7FFFFFF0,
        operation="recover_image",
        started_at=0.0,
    )
    path = tmp_path / "usb_0403_6001_A10K8Z4P.lock"
    path.write_text(json.dumps(stale.to_json()), encoding="utf-8")

    reclaimed = lease(tmp_path)
    reclaimed.acquire()

    assert reclaimed.held is True
    assert reclaimed.read() is not None
    assert reclaimed.read().pid == os.getpid()  # type: ignore[union-attr]


def test_a_corrupt_lock_file_is_not_a_valid_claim(tmp_path: Path) -> None:
    """Half-written JSON must not lock a device out forever."""
    path = tmp_path / "usb_0403_6001_A10K8Z4P.lock"
    path.write_text("{not json at all", encoding="utf-8")

    held = lease(tmp_path)
    held.acquire()
    assert held.held is True


def test_release_does_not_remove_another_process_lease(tmp_path: Path) -> None:
    """Releasing must only ever drop our own claim."""
    other = LeaseInfo(
        target_key=KEY,
        target_name="bench-left",
        pid=os.getpid() + 1,
        operation="reset",
        started_at=0.0,
    )
    path = tmp_path / "usb_0403_6001_A10K8Z4P.lock"

    held = lease(tmp_path)
    held.acquire()
    path.write_text(json.dumps(other.to_json()), encoding="utf-8")

    held.release()
    assert path.exists() is True


def test_context_manager_releases_on_exception(tmp_path: Path) -> None:
    held = lease(tmp_path)
    with pytest.raises(RuntimeError), held:
        raise RuntimeError("boom")
    assert held.path.exists() is False


def test_the_current_process_is_alive() -> None:
    assert process_alive(os.getpid()) is True


def test_an_implausible_pid_is_not_alive() -> None:
    assert process_alive(0x7FFFFFF0) is False


def test_a_nonsense_pid_is_not_alive() -> None:
    assert process_alive(0) is False
    assert process_alive(-1) is False
