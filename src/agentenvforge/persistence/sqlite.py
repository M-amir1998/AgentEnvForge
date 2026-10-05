"""SQLite implementation of :class:`~.repository.RunRepository`.

Why the stdlib ``sqlite3`` module rather than an ORM (ADR-004): the schema is three small
tables accessed through one class, so an ORM would add a large dependency without
removing meaningful code. Schema changes are versioned migrations in
:data:`MIGRATIONS`, applied automatically and recorded in ``schema_migrations``, so every
database is reproducibly at a known schema version.

Concurrency: one connection per repository guarded by a lock (events arrive from agent
worker threads), WAL journaling so readers (``agentenv runs list``) never block a running
execution, and a busy timeout so separate processes queue instead of failing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from agentenvforge.artifacts import ArtifactRecord
from agentenvforge.domain.events import EventType, ExecutionEvent
from agentenvforge.domain.manifest import RunManifest
from agentenvforge.domain.results import RunResult
from agentenvforge.domain.status import RunStatus
from agentenvforge.persistence.repository import NewRun, RunSummary, StoredRun

MIGRATIONS: tuple[str, ...] = (
    # 1: initial schema
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
)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SqliteRunRepository:
    """Run repository stored in a single SQLite file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._connection.close()

    @property
    def schema_version(self) -> int:
        """Number of migrations applied."""
        row = self._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # One lock + one transaction per logical write keeps each operation atomic.
        with self._lock, self._connection:
            yield self._connection

    def _migrate(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        current = self.schema_version
        for version, script in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            with self._transaction() as connection:
                for statement in filter(str.strip, script.split(";")):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (?, datetime('now'))",
                    (version,),
                )

    # ------------------------------------------------------------------ writes

    def create_run(self, run: NewRun) -> None:
        """Insert a new run."""
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO runs (run_id, task_id, task_path, agent, status, created_at, "
                "updated_at, manifest_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.task_id,
                    run.task_path,
                    run.agent,
                    RunStatus.CREATED.value,
                    _timestamp(run.created_at),
                    _timestamp(run.created_at),
                    run.manifest.model_dump_json(),
                ),
            )

    def update_status(self, run_id: str, status: RunStatus, at: datetime) -> None:
        """Record a status transition."""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status.value, _timestamp(at), run_id),
            )

    def update_manifest(self, run_id: str, manifest: RunManifest) -> None:
        """Replace the stored manifest."""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE runs SET manifest_json = ? WHERE run_id = ?",
                (manifest.model_dump_json(), run_id),
            )

    def append_event(self, event: ExecutionEvent) -> None:
        """Insert one event."""
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO events (event_id, run_id, task_id, sequence, timestamp, event_type, "
                "status, duration_ms, data_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.run_id,
                    event.task_id,
                    event.sequence,
                    _timestamp(event.timestamp),
                    event.event_type.value,
                    event.status.value,
                    event.duration_ms,
                    json.dumps(event.data),
                ),
            )

    def add_artifact(self, record: ArtifactRecord) -> None:
        """Insert or replace an artifact record."""
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO artifacts (artifact_id, run_id, name, size_bytes, sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (run_id, name) DO UPDATE SET "
                "artifact_id = excluded.artifact_id, size_bytes = excluded.size_bytes, "
                "sha256 = excluded.sha256, created_at = excluded.created_at",
                (
                    record.artifact_id,
                    record.run_id,
                    record.name,
                    record.size_bytes,
                    record.sha256,
                    _timestamp(record.created_at),
                ),
            )

    def complete_run(self, result: RunResult) -> None:
        """Store the final result."""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ?, finished_at = ?, passed = ?, "
                "score = ?, failure_reason = ?, duration_ms = ?, result_json = ? WHERE run_id = ?",
                (
                    result.status.value,
                    _timestamp(result.finished_at),
                    _timestamp(result.finished_at),
                    None if result.passed is None else int(result.passed),
                    result.score,
                    result.failure.reason.value if result.failure else None,
                    result.duration_ms,
                    result.model_dump_json(),
                    result.run_id,
                ),
            )

    # ------------------------------------------------------------------- reads

    def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, parameters).fetchall())

    @staticmethod
    def _summary(row: sqlite3.Row) -> RunSummary:
        return RunSummary(
            run_id=row["run_id"],
            task_id=row["task_id"],
            agent=row["agent"],
            status=RunStatus(row["status"]),
            passed=None if row["passed"] is None else bool(row["passed"]),
            score=row["score"],
            created_at=_parse_timestamp(row["created_at"]),
            duration_ms=row["duration_ms"],
        )

    def get_run(self, run_id: str) -> StoredRun | None:
        """Return a run by ID."""
        rows = self._query("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if not rows:
            return None
        row = rows[0]
        return StoredRun(
            summary=self._summary(row),
            task_path=row["task_path"],
            manifest=RunManifest.model_validate_json(row["manifest_json"]),
            result=RunResult.model_validate_json(row["result_json"])
            if row["result_json"]
            else None,
        )

    def list_runs(
        self, *, limit: int = 50, task_id: str | None = None, status: RunStatus | None = None
    ) -> list[RunSummary]:
        """Most recent runs first, optionally filtered."""
        clauses, parameters = [], []
        if task_id is not None:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(
            f"SELECT * FROM runs {where} ORDER BY created_at DESC, run_id DESC LIMIT ?",  # noqa: S608 - clauses are constants
            (*parameters, limit),
        )
        return [self._summary(row) for row in rows]

    def list_events(self, run_id: str) -> list[ExecutionEvent]:
        """Events of a run in order."""
        rows = self._query("SELECT * FROM events WHERE run_id = ? ORDER BY sequence", (run_id,))
        return [
            ExecutionEvent(
                event_id=row["event_id"],
                run_id=row["run_id"],
                task_id=row["task_id"],
                sequence=row["sequence"],
                timestamp=_parse_timestamp(row["timestamp"]),
                event_type=EventType(row["event_type"]),
                status=RunStatus(row["status"]),
                duration_ms=row["duration_ms"],
                data=json.loads(row["data_json"]),
            )
            for row in rows
        ]

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        """Artifact records of a run ordered by name."""
        rows = self._query("SELECT * FROM artifacts WHERE run_id = ? ORDER BY name", (run_id,))
        return [
            ArtifactRecord(
                artifact_id=row["artifact_id"],
                run_id=row["run_id"],
                name=row["name"],
                size_bytes=row["size_bytes"],
                sha256=row["sha256"],
                created_at=_parse_timestamp(row["created_at"]),
            )
            for row in rows
        ]
