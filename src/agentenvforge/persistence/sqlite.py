"""SQLite implementation of :class:`~.repository.RunRepository`.

Why the stdlib ``sqlite3`` module rather than an ORM (ADR-0004): the schema is a few small
tables accessed through small repository classes, so an ORM would add a large dependency
without removing meaningful code. Connection handling, concurrency and migrations live in
:mod:`agentenvforge.persistence.sqlite_database`, shared with the experiment repository.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from agentenvforge.artifacts import ArtifactRecord
from agentenvforge.domain.events import EventType, ExecutionEvent
from agentenvforge.domain.manifest import RunManifest
from agentenvforge.domain.results import RunResult
from agentenvforge.domain.status import RunStatus
from agentenvforge.persistence.repository import NewRun, RunSummary, StoredRun
from agentenvforge.persistence.sqlite_database import MIGRATIONS, SqliteDatabase

__all__ = ["MIGRATIONS", "SqliteRunRepository"]


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SqliteRunRepository:
    """Run repository stored in a SQLite database.

    Accepts an open :class:`SqliteDatabase` (shared with other repositories; the caller
    closes it) or a path, for which it opens and owns its own database.
    """

    def __init__(self, database: SqliteDatabase | Path) -> None:
        self._owns_database = not isinstance(database, SqliteDatabase)
        self._database = (
            database if isinstance(database, SqliteDatabase) else SqliteDatabase(database)
        )

    def close(self) -> None:
        """Close the database if this repository opened it."""
        if self._owns_database:
            self._database.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        """Number of migrations applied."""
        return self._database.schema_version

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._database.transaction()

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
        return self._database.query(sql, parameters)

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
