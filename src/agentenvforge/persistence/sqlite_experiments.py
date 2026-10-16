"""SQLite implementation of :class:`~.experiment_repository.ExperimentRepository`."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime

from agentenvforge.analysis.models import ExperimentSummary
from agentenvforge.domain.clock import utc_now
from agentenvforge.experiments.manifest import ExperimentManifest
from agentenvforge.experiments.status import ExperimentStatus, ExperimentStatusChange
from agentenvforge.persistence.experiment_repository import (
    ExperimentEntry,
    ExperimentRecord,
    NewExperiment,
    StoredExperiment,
)
from agentenvforge.persistence.sqlite_database import SqliteDatabase


def _history_json(history: Sequence[ExperimentStatusChange]) -> str:
    return json.dumps(
        [{"status": c.status.value, "entered_at": c.entered_at.isoformat()} for c in history]
    )


def _parse_history(value: str) -> tuple[ExperimentStatusChange, ...]:
    return tuple(
        ExperimentStatusChange(
            ExperimentStatus(item["status"]), datetime.fromisoformat(item["entered_at"])
        )
        for item in json.loads(value)
    )


class SqliteExperimentRepository:
    """Experiments stored in the shared :class:`SqliteDatabase`."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def create_experiment(self, experiment: NewExperiment) -> None:
        """Insert the experiment and all planned runs in one transaction."""
        created = experiment.created_at.isoformat()
        history = [ExperimentStatusChange(ExperimentStatus.CREATED, experiment.created_at)]
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO experiments (experiment_id, spec_id, name, status, created_at, "
                "updated_at, planned_runs, workers, definition_hash, owner, history_json, "
                "manifest_json, output_dir) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment.experiment_id,
                    experiment.spec_id,
                    experiment.name,
                    ExperimentStatus.CREATED.value,
                    created,
                    created,
                    len(experiment.combinations),
                    experiment.workers,
                    experiment.definition_hash,
                    experiment.owner,
                    _history_json(history),
                    experiment.manifest.model_dump_json(),
                    experiment.output_dir,
                ),
            )
            connection.executemany(
                "INSERT INTO experiment_runs (experiment_id, position, task_id, agent_id, "
                "repetition, seed) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        experiment.experiment_id,
                        c.position,
                        c.task_id,
                        c.agent_id,
                        c.repetition,
                        c.seed,
                    )
                    for c in experiment.combinations
                ],
            )

    def record_status(self, experiment_id: str, history: Sequence[ExperimentStatusChange]) -> None:
        """Store the status history; the last entry is the current status."""
        current = history[-1]
        finished = current.entered_at.isoformat() if current.status.is_terminal else None
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE experiments SET status = ?, updated_at = ?, finished_at = ?, "
                "history_json = ? WHERE experiment_id = ?",
                (
                    current.status.value,
                    current.entered_at.isoformat(),
                    finished,
                    _history_json(history),
                    experiment_id,
                ),
            )

    def update_manifest(self, experiment_id: str, manifest: ExperimentManifest) -> None:
        """Replace the manifest."""
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE experiments SET manifest_json = ? WHERE experiment_id = ?",
                (manifest.model_dump_json(), experiment_id),
            )

    def link_run(self, experiment_id: str, position: int, run_id: str) -> None:
        """Record the run executing the planned run at ``position``."""
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE experiment_runs SET run_id = ? "
                "WHERE experiment_id = ? AND position = ? AND run_id IS NULL",
                (run_id, experiment_id, position),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"planned run {experiment_id}#{position} is unknown or already linked"
                )

    def store_summary(
        self, experiment_id: str, summary: ExperimentSummary, output_dir: str | None
    ) -> None:
        """Store the summary JSON and output directory."""
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE experiments SET summary_json = ?, output_dir = ?, updated_at = ? "
                "WHERE experiment_id = ?",
                (summary.model_dump_json(), output_dir, utc_now().isoformat(), experiment_id),
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> ExperimentRecord:
        return ExperimentRecord(
            experiment_id=row["experiment_id"],
            spec_id=row["spec_id"],
            name=row["name"],
            status=ExperimentStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            planned_runs=row["planned_runs"],
            workers=row["workers"],
            owner=row["owner"],
            output_dir=row["output_dir"],
        )

    @staticmethod
    def _entry(row: sqlite3.Row) -> ExperimentEntry:
        return ExperimentEntry(
            experiment_id=row["experiment_id"],
            position=row["position"],
            task_id=row["task_id"],
            agent_id=row["agent_id"],
            repetition=row["repetition"],
            seed=row["seed"],
            run_id=row["run_id"],
        )

    def get_experiment(self, experiment_id: str) -> StoredExperiment | None:
        """Return an experiment with all its entries."""
        rows = self._database.query(
            "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
        )
        if not rows:
            return None
        row = rows[0]
        entries = self._database.query(
            "SELECT * FROM experiment_runs WHERE experiment_id = ? ORDER BY position",
            (experiment_id,),
        )
        return StoredExperiment(
            record=self._record(row),
            manifest=ExperimentManifest.model_validate_json(row["manifest_json"]),
            history=_parse_history(row["history_json"]),
            entries=tuple(self._entry(entry) for entry in entries),
            summary=(
                ExperimentSummary.model_validate_json(row["summary_json"])
                if row["summary_json"]
                else None
            ),
        )

    def list_experiments(self, *, limit: int = 50) -> list[ExperimentRecord]:
        """Most recent experiments first."""
        rows = self._database.query(
            "SELECT * FROM experiments ORDER BY created_at DESC, experiment_id DESC LIMIT ?",
            (limit,),
        )
        return [self._record(row) for row in rows]

    def find_entry_by_run(self, run_id: str) -> ExperimentEntry | None:
        """The entry linked to ``run_id``."""
        rows = self._database.query("SELECT * FROM experiment_runs WHERE run_id = ?", (run_id,))
        return self._entry(rows[0]) if rows else None
