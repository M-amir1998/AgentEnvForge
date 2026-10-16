"""The SQLite database shared by the run and experiment repositories (ADR-0004).

One file, one connection per process-level :class:`SqliteDatabase`, guarded by a lock: runs
executing in parallel worker threads write through it. WAL journaling lets readers (``agentenv
runs list`` in another terminal) proceed while an experiment writes, and the busy timeout makes
separate processes queue instead of failing.

Schema changes are append-only migrations in :data:`MIGRATIONS`, applied in order and recorded
in ``schema_migrations``. Migrations run inside ``BEGIN IMMEDIATE``, which takes the database
write lock first, and re-check the version inside the transaction, so two processes opening an
old database at the same moment cannot both apply the same migration.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS: tuple[str, ...] = (
    # 1: runs, events and artifact records
    """
    CREATE TABLE runs (
        run_id          TEXT PRIMARY KEY,
        task_id         TEXT NOT NULL,
        task_path       TEXT NOT NULL,
        agent           TEXT NOT NULL,
        status          TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        finished_at     TEXT,
        passed          INTEGER,
        score           REAL,
        failure_reason  TEXT,
        duration_ms     INTEGER,
        manifest_json   TEXT NOT NULL,
        result_json     TEXT
    );
    CREATE INDEX runs_task_id ON runs (task_id);
    CREATE INDEX runs_status ON runs (status);
    CREATE INDEX runs_created_at ON runs (created_at);

    CREATE TABLE events (
        event_id     TEXT PRIMARY KEY,
        run_id       TEXT NOT NULL REFERENCES runs (run_id),
        task_id      TEXT NOT NULL,
        sequence     INTEGER NOT NULL,
        timestamp    TEXT NOT NULL,
        event_type   TEXT NOT NULL,
        status       TEXT NOT NULL,
        duration_ms  INTEGER,
        data_json    TEXT NOT NULL,
        UNIQUE (run_id, sequence)
    );

    CREATE TABLE artifacts (
        artifact_id  TEXT PRIMARY KEY,
        run_id       TEXT NOT NULL REFERENCES runs (run_id),
        name         TEXT NOT NULL,
        size_bytes   INTEGER NOT NULL,
        sha256       TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        UNIQUE (run_id, name)
    );
    """,
    # 2: experiments and their planned runs (Milestone 8). Single runs are unaffected.
    #    experiment_runs.run_id has no foreign key on purpose: the run ID is assigned (and
    #    recorded here) just before the run creates its own row in `runs`. An entry's state
    #    is derived (no run_id: not started; otherwise the linked run's own state), so it can
    #    never disagree with the run record.
    """
    CREATE TABLE experiments (
        experiment_id    TEXT PRIMARY KEY,
        spec_id          TEXT NOT NULL,
        name             TEXT,
        status           TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        finished_at      TEXT,
        planned_runs     INTEGER NOT NULL,
        workers          INTEGER NOT NULL,
        definition_hash  TEXT NOT NULL,
        owner            TEXT NOT NULL,
        history_json     TEXT NOT NULL,
        manifest_json    TEXT NOT NULL,
        summary_json     TEXT,
        output_dir       TEXT
    );
    CREATE INDEX experiments_created_at ON experiments (created_at);
    CREATE INDEX experiments_spec_id ON experiments (spec_id);

    CREATE TABLE experiment_runs (
        experiment_id  TEXT NOT NULL REFERENCES experiments (experiment_id),
        position       INTEGER NOT NULL,
        task_id        TEXT NOT NULL,
        agent_id       TEXT NOT NULL,
        repetition     INTEGER NOT NULL,
        seed           INTEGER NOT NULL,
        run_id         TEXT,
        PRIMARY KEY (experiment_id, position)
    );
    CREATE INDEX experiment_runs_run_id ON experiment_runs (run_id);
    """,
)


class SqliteDatabase:
    """A migrated SQLite database file with a thread-safe connection."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        # isolation_level=None: transactions are explicit (BEGIN/COMMIT below), so that the
        # migration can use BEGIN IMMEDIATE.
        self._connection = sqlite3.connect(
            path, check_same_thread=False, timeout=10.0, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        # Safe with WAL: an OS crash can lose the last commits but never corrupts the file,
        # and avoids an fsync per event, which matters with several runs writing at once.
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._connection.close()

    @property
    def schema_version(self) -> int:
        """Number of migrations applied."""
        with self._lock:
            return self._current_version()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run the block as one atomic transaction (one lock + one transaction per write)."""
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Execute a read query and return all rows."""
        with self._lock:
            return list(self._connection.execute(sql, parameters).fetchall())

    def _current_version(self) -> int:
        row = self._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def _migrate(self) -> None:
        with self._lock:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for version, script in enumerate(MIGRATIONS, start=1):
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    if version > self._current_version():
                        for statement in filter(str.strip, script.split(";")):
                            self._connection.execute(statement)
                        self._connection.execute(
                            "INSERT INTO schema_migrations (version, applied_at) "
                            "VALUES (?, datetime('now'))",
                            (version,),
                        )
                except BaseException:
                    self._connection.execute("ROLLBACK")
                    raise
                self._connection.execute("COMMIT")
