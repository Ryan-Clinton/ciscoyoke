"""Where operational state lives.

The journal is not a transcript and does not belong beside one. A transcript is
*evidence*: what the device said, kept for the record and for replay. A journal
is *operational truth*: the minimum needed to resume safely after an
interruption. They have different lifetimes, different privacy properties and
different consumers, and conflating them means either the journal gets published
or the transcripts get treated as disposable.

So state goes to the OS state directory -- ``~/.local/state/ciscoyoke`` on
Linux, ``%LOCALAPPDATA%\\ciscoyoke`` on Windows -- resolved by ``platformdirs``
rather than by guessing at platform conventions.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

APP_NAME = "ciscoyoke"

#: Overrides the state directory. Exists for tests and for anyone who wants
#: their state somewhere specific; nothing else should need it.
STATE_DIR_ENV = "CISCOYOKE_STATE_DIR"


def state_dir() -> Path:
    """The directory holding the journal database and transport leases."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)

    from platformdirs import user_state_dir

    return Path(user_state_dir(APP_NAME, appauthor=False))


def ensure_state_dir() -> Path:
    """Create the state directory if needed and return it.

    Created with owner-only permissions where the platform honours them: a
    journal records what was done to which device and when, which is not
    catastrophic to disclose but is nobody else's business either.
    """
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # POSIX honours this; Windows does not implement it and the directory
    # relies on the user profile ACL instead.
    with contextlib.suppress(OSError, NotImplementedError):
        directory.chmod(0o700)
    return directory


def journal_db() -> Path:
    return ensure_state_dir() / "state.db"


def locks_dir() -> Path:
    directory = ensure_state_dir() / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
