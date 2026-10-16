"""Reading an experiment's results back from persistence.

Used by the executor when an experiment finishes and by ``agentenv experiment report`` and
``experiments show`` later, so every view of an experiment is computed the same way, from
the same persisted facts.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from agentenvforge.analysis.aggregate import AgentInfo, ExperimentFacts, RunObservation, summarize
from agentenvforge.analysis.models import ExperimentSummary
from agentenvforge.experiments.status import ExperimentStatus
from agentenvforge.persistence.experiment_repository import ExperimentRecord, StoredExperiment
from agentenvforge.persistence.repository import RunRepository
from agentenvforge.processes import Owner, process_exists

PATCH_ARTIFACT = "solution.patch"


def observations(stored: StoredExperiment, runs: RunRepository) -> list[RunObservation]:
    """One observation per planned run, joined with its run's result and patch hash."""
    result: list[RunObservation] = []
    for entry in stored.entries:
        run_result = None
        patch_hash = None
        if entry.run_id is not None:
            run = runs.get_run(entry.run_id)
            run_result = run.result if run is not None else None
            patch_hash = next(
                (a.sha256 for a in runs.list_artifacts(entry.run_id) if a.name == PATCH_ARTIFACT),
                None,
            )
        result.append(
            RunObservation(
                position=entry.position,
                task_id=entry.task_id,
                agent_id=entry.agent_id,
                repetition=entry.repetition,
                seed=entry.seed,
                run_id=entry.run_id,
                result=run_result,
                patch_sha256=patch_hash,
            )
        )
    return result


def running_seconds(stored: StoredExperiment) -> float | None:
    """Wall-clock duration of the RUNNING phase, if it began and ended."""
    history = stored.history
    for index, change in enumerate(history[:-1]):
        if change.status is ExperimentStatus.RUNNING:
            return (history[index + 1].entered_at - change.entered_at).total_seconds()
    return None


def effective_status(record: ExperimentRecord) -> str:
    """The stored status, or ``interrupted`` when a non-terminal experiment's process died."""
    if record.status.is_terminal:
        return record.status.value
    owner = Owner.parse(record.owner)
    if owner is not None and owner.is_local() and not process_exists(owner.pid):
        return "interrupted"
    return record.status.value


def summarize_experiment(
    stored: StoredExperiment,
    runs: RunRepository,
    *,
    generated_at: datetime,
    status: str | None = None,
) -> ExperimentSummary:
    """Aggregate a persisted experiment (``status`` overrides the stored one, e.g. the
    terminal status an experiment is about to enter)."""
    manifest = stored.manifest
    agents = tuple(
        AgentInfo(
            agent_id=agent.agent_id,
            role=agent.role,
            adapter=agent.adapter,
            model=_model_name(agent.provenance, agent.spec),
            budget_override_seconds=agent.agent_timeout_seconds,
        )
        for agent in manifest.agents
    )
    facts = ExperimentFacts(
        experiment_id=stored.record.experiment_id,
        spec_id=stored.record.spec_id,
        name=stored.record.name,
        status=status or effective_status(stored.record),
        workers=stored.record.workers,
        repetitions=manifest.repetitions,
        task_ids=tuple(task.task_id for task in manifest.tasks),
        agents=agents,
        running_seconds=running_seconds(stored),
    )
    return summarize(facts, observations(stored, runs), generated_at=generated_at)


def _model_name(provenance: Mapping[str, object], spec: Mapping[str, object]) -> str | None:
    model = provenance.get("model")
    if isinstance(model, str):
        return model
    options = spec.get("options")
    if isinstance(options, dict) and isinstance(options.get("model"), str):
        return str(options["model"])
    return None
