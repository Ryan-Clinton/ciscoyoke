"""Crash-safe journal storage.

SQLite in WAL mode, not a hand-rolled JSON or YAML state file. The requirement
here is exactly what a database provides and what an ad-hoc file does not:
atomic commits and durability across a process that dies mid-write. A state
file truncated by the crash it exists to survive is worse than no state file,
because it turns a recoverable situation into a confusing one.

The write-ahead discipline lives here too. :meth:`Journal.attempting` records
the intent, yields so the caller can act, and leaves the mutation in
``ATTEMPTED`` if the process dies -- deliberately, because that is the honest
record of what is actually known at that moment.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ciscoyoke.journal.model import Mutation, MutationStatus
from ciscoyoke.journal.paths import journal_db

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    target_key    TEXT NOT NULL,
    target_name   TEXT NOT NULL,
    operation     TEXT NOT NULL,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    outcome       TEXT,
    starting_state TEXT,
    device_identity TEXT
);

CREATE TABLE IF NOT EXISTS mutations (
    mutation_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    sequence    INTEGER NOT NULL,
    payload     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS mutations_by_run ON mutations(run_id, sequence);
CREATE INDEX IF NOT EXISTS runs_by_target ON runs(target_key, finished_at);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the journal database with durable settings."""
    location = path if path is not None else journal_db()
    connection = sqlite3.connect(location, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # WAL survives a process crash without losing committed transactions;
    # FULL synchronous means a commit is on disk before it is acknowledged,
    # which is the entire point of a write-ahead journal.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    return connection


class Journal:
    """The record of one operation against one device."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        target_key: str,
    ) -> None:
        self._connection = connection
        self.run_id = run_id
        self.target_key = target_key
        self._sequence = 0

    # -- construction ------------------------------------------------------

    @classmethod
    def begin(
        cls,
        connection: sqlite3.Connection,
        *,
        target_key: str,
        target_name: str,
        operation: str,
        starting_state: str | None = None,
        device_identity: dict[str, Any] | None = None,
    ) -> Journal:
        run_id = uuid.uuid4().hex[:16]
        connection.execute(
            "INSERT INTO runs(run_id, target_key, target_name, operation, "
            "started_at, starting_state, device_identity) VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                target_key,
                target_name,
                operation,
                time.time(),
                starting_state,
                json.dumps(device_identity or {}),
            ),
        )
        return cls(connection, run_id, target_key)

    # -- write-ahead protocol ---------------------------------------------

    def prepare(self, mutation: Mutation) -> Mutation:
        """Record intent durably, before anything is sent to the device."""
        prepared = mutation.advance(MutationStatus.PREPARED)
        self._sequence += 1
        self._connection.execute(
            "INSERT INTO mutations(mutation_id, run_id, sequence, payload) "
            "VALUES (?,?,?,?)",
            (
                prepared.mutation_id,
                self.run_id,
                self._sequence,
                json.dumps(prepared.to_json()),
            ),
        )
        return prepared

    def update(self, mutation: Mutation) -> Mutation:
        """Persist a mutation's new status."""
        self._connection.execute(
            "UPDATE mutations SET payload = ? WHERE mutation_id = ?",
            (json.dumps(mutation.to_json()), mutation.mutation_id),
        )
        return mutation

    @contextmanager
    def attempting(self, mutation: Mutation) -> Iterator[Mutation]:
        """Record intent, mark the attempt, and hand control to the caller.

        On leaving the block normally the mutation is left at ``ATTEMPTED``: the
        caller is expected to observe the device and call
        :meth:`observed` or :meth:`failed`. That is not an oversight -- claiming
        a mutation applied because the code that sent it returned would be
        exactly the false certainty this design exists to avoid.

        If the process dies inside the block, ``ATTEMPTED`` is what survives,
        which is the honest record of what is actually known.
        """
        prepared = self.prepare(mutation)
        attempted = self.update(prepared.advance(MutationStatus.ATTEMPTED))
        yield attempted

    def observed(self, mutation: Mutation) -> Mutation:
        """Record that the device was seen in the expected condition."""
        return self.update(mutation.advance(MutationStatus.OBSERVED_APPLIED))

    def verified(self, mutation: Mutation) -> Mutation:
        return self.update(mutation.advance(MutationStatus.POSTCONDITION_VERIFIED))

    def committed(self, mutation: Mutation) -> Mutation:
        return self.update(mutation.advance(MutationStatus.COMMITTED))

    def compensated(self, mutation: Mutation) -> Mutation:
        return self.update(mutation.advance(MutationStatus.COMPENSATED))

    def failed(self, mutation: Mutation) -> Mutation:
        """Record that the mutation was observed *not* to have taken effect."""
        return self.update(mutation.advance(MutationStatus.FAILED))

    # -- reading -----------------------------------------------------------

    def mutations(self) -> tuple[Mutation, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM mutations WHERE run_id = ? ORDER BY sequence",
            (self.run_id,),
        ).fetchall()
        return tuple(Mutation.from_json(json.loads(row["payload"])) for row in rows)

    def unresolved(self) -> tuple[Mutation, ...]:
        """Mutations whose real effect is still unknown."""
        return tuple(m for m in self.mutations() if m.unresolved)

    def in_effect(self) -> tuple[Mutation, ...]:
        """Mutations currently applied, newest first -- rollback order."""
        return tuple(reversed([m for m in self.mutations() if m.in_effect]))

    def finish(self, outcome: str) -> None:
        self._connection.execute(
            "UPDATE runs SET finished_at = ?, outcome = ? WHERE run_id = ?",
            (time.time(), outcome, self.run_id),
        )

    @property
    def finished(self) -> bool:
        row = self._connection.execute(
            "SELECT finished_at FROM runs WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        return bool(row and row["finished_at"] is not None)


def find_interrupted(
    connection: sqlite3.Connection, target_key: str
) -> Journal | None:
    """The most recent unfinished run against this device, if any.

    An unfinished run is the signal that a previous invocation did not complete.
    What it means for the device is not knowable from here -- that requires
    probing, which is :mod:`ciscoyoke.journal.converge`'s job.
    """
    row = connection.execute(
        "SELECT run_id FROM runs WHERE target_key = ? AND finished_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (target_key,),
    ).fetchone()
    if row is None:
        return None

    journal = Journal(connection, str(row["run_id"]), target_key)
    count = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS n FROM mutations WHERE run_id = ?",
        (journal.run_id,),
    ).fetchone()
    journal._sequence = int(count["n"])
    return journal


def run_summary(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return {}
    return {
        "run_id": row["run_id"],
        "target": row["target_name"],
        "operation": row["operation"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "outcome": row["outcome"],
        "starting_state": row["starting_state"],
        "pid": os.getpid(),
    }
