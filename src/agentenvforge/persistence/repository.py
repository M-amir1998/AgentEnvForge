"""The run repository interface (repository pattern, ADR-004).

The orchestrator records runs through :class:`RunRepository` without knowing how they
are stored. SQLite implements it for local use; a PostgreSQL implementation for a shared
multi-worker deployment would implement the same protocol without touching callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agentenvforge.artifacts import ArtifactRecord
from agentenvforge.domain.events import ExecutionEvent
from agentenvforge.domain.manifest import RunManifest
from agentenvforge.domain.results import RunResult
from agentenvforge.domain.status import RunStatus


@dataclass(frozen=True, slots=True)
class NewRun:
    """Initial record written when a run is created."""

    run_id: str
    task_id: str
    task_path: str
    agent: str
    created_at: datetime
    manifest: RunManifest


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One row of ``runs list``."""

    run_id: str
    task_id: str
    agent: str
    status: RunStatus
    passed: bool | None
    score: float | None
    created_at: datetime
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class StoredRun:
    """Everything persisted about one run."""

    summary: RunSummary
    task_path: str
    manifest: RunManifest
    result: RunResult | None
    """``None`` while running, or if the process died before finishing."""


class RunRepository(Protocol):
    """Persistence of runs, their events and artifact records."""

    def create_run(self, run: NewRun) -> None:
        """Insert a new run in status ``CREATED``."""
        ...

    def update_status(self, run_id: str, status: RunStatus, at: datetime) -> None:
        """Record a status transition."""
        ...

    def update_manifest(self, run_id: str, manifest: RunManifest) -> None:
        """Replace the run's manifest as provenance information becomes known."""
        ...

    def append_event(self, event: ExecutionEvent) -> None:
        """Append one event (sequence numbers are unique per run)."""
        ...

    def add_artifact(self, record: ArtifactRecord) -> None:
        """Record (or replace) an artifact by ``(run_id, name)``."""
        ...

    def complete_run(self, result: RunResult) -> None:
        """Store the final result of a run."""
        ...

    def get_run(self, run_id: str) -> StoredRun | None:
        """Return a run, or ``None`` if it does not exist."""
        ...

    def list_runs(
        self, *, limit: int = 50, task_id: str | None = None, status: RunStatus | None = None
    ) -> list[RunSummary]:
        """Most recent runs first."""
        ...

    def list_events(self, run_id: str) -> list[ExecutionEvent]:
        """A run's events in sequence order."""
        ...

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        """A run's artifact records ordered by name."""
        ...
