"""The experiment repository interface.

An experiment record holds the experiment's own state (status history, manifest, summary)
and one *entry* per planned run. An entry links a matrix position (task, agent,
repetition, seed) to the run that executed it; that run stays an ordinary, independently
inspectable run in the run repository. All entries are written when the experiment is
created, so planned, started and finished counts can always be reconciled, also after a
crash. Entry state is never stored: an entry without a run ID was not started, and
otherwise the linked run's record says what happened.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agentenvforge.analysis.models import ExperimentSummary
from agentenvforge.experiments.manifest import ExperimentManifest
from agentenvforge.experiments.matrix import RunCombination
from agentenvforge.experiments.status import ExperimentStatus, ExperimentStatusChange


@dataclass(frozen=True, slots=True)
class ExperimentEntry:
    """One planned run of an experiment."""

    experiment_id: str
    position: int
    task_id: str
    agent_id: str
    repetition: int
    seed: int
    run_id: str | None
    """``None`` until the run is started."""


@dataclass(frozen=True, slots=True)
class NewExperiment:
    """Initial record of an experiment and its complete matrix."""

    experiment_id: str
    spec_id: str
    name: str | None
    created_at: datetime
    workers: int
    definition_hash: str
    owner: str
    """``<host>:<pid>`` of the executing process (see :mod:`agentenvforge.processes`)."""
    manifest: ExperimentManifest
    output_dir: str | None
    combinations: Sequence[RunCombination]


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    """One row of ``experiments list``."""

    experiment_id: str
    spec_id: str
    name: str | None
    status: ExperimentStatus
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    planned_runs: int
    workers: int
    owner: str
    output_dir: str | None


@dataclass(frozen=True, slots=True)
class StoredExperiment:
    """Everything persisted about one experiment."""

    record: ExperimentRecord
    manifest: ExperimentManifest
    history: tuple[ExperimentStatusChange, ...]
    entries: tuple[ExperimentEntry, ...]
    summary: ExperimentSummary | None


class ExperimentRepository(Protocol):
    """Persistence of experiments and their planned runs."""

    def create_experiment(self, experiment: NewExperiment) -> None:
        """Insert the experiment (status ``CREATED``) and every planned run, atomically."""
        ...

    def record_status(self, experiment_id: str, history: Sequence[ExperimentStatusChange]) -> None:
        """Store the status history (its last entry is the current status)."""
        ...

    def update_manifest(self, experiment_id: str, manifest: ExperimentManifest) -> None:
        """Replace the manifest (preparation adds environment and agent provenance)."""
        ...

    def link_run(self, experiment_id: str, position: int, run_id: str) -> None:
        """Record that the planned run at ``position`` is executed as ``run_id``."""
        ...

    def store_summary(
        self, experiment_id: str, summary: ExperimentSummary, output_dir: str | None
    ) -> None:
        """Store the aggregated summary and where its files were written."""
        ...

    def get_experiment(self, experiment_id: str) -> StoredExperiment | None:
        """Return an experiment, or ``None``."""
        ...

    def list_experiments(self, *, limit: int = 50) -> list[ExperimentRecord]:
        """Most recent experiments first."""
        ...

    def find_entry_by_run(self, run_id: str) -> ExperimentEntry | None:
        """The experiment entry that produced ``run_id``, if any."""
        ...
