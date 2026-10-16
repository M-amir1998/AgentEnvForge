"""Turning an experiment's persisted runs into an :class:`ExperimentSummary` (pure).

The same function produces the summary when an experiment finishes and when
``agentenv experiment report`` regenerates outputs later, so both always agree.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from agentenvforge.analysis.models import (
    AgentSummary,
    CellSummary,
    Distribution,
    ExperimentSummary,
    FailureCount,
    OutcomeCounts,
    PassAtK,
    Rate,
    TaskSummary,
    TokenUsage,
)
from agentenvforge.analysis.outcomes import Outcome, classify
from agentenvforge.analysis.stats import describe, pass_at_k, wilson_interval
from agentenvforge.domain.results import RunResult
from agentenvforge.domain.status import RunStatus

MIN_RUNS_FOR_P95 = 20
"""Below this many runs, the 95th percentile is essentially the maximum and is not reported."""


@dataclass(frozen=True, slots=True)
class RunObservation:
    """One planned run as persisted: its matrix position and, if it ran, its result."""

    position: int
    task_id: str
    agent_id: str
    repetition: int
    seed: int
    run_id: str | None
    result: RunResult | None
    patch_sha256: str | None = None
    """Hash of the run's solution.patch artifact (detects identical repetitions)."""

    @property
    def outcome(self) -> Outcome:
        """How this run counts (see :mod:`agentenvforge.analysis.outcomes`)."""
        return classify(self.result, started=self.run_id is not None)


@dataclass(frozen=True, slots=True)
class AgentInfo:
    """What the summary needs to know about an agent."""

    agent_id: str
    role: str
    adapter: str
    model: str | None = None
    budget_override_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ExperimentFacts:
    """Experiment-level facts the summary reports."""

    experiment_id: str
    spec_id: str
    name: str | None
    status: str
    workers: int
    repetitions: int
    task_ids: tuple[str, ...]
    agents: tuple[AgentInfo, ...]
    running_seconds: float | None


def summarize(
    facts: ExperimentFacts, observations: Sequence[RunObservation], *, generated_at: datetime
) -> ExperimentSummary:
    """Aggregate ``observations`` (one per planned run) into a summary."""
    by_cell = {
        key: list(group)
        for key, group in itertools.groupby(
            sorted(observations, key=lambda o: (o.agent_id, o.task_id, o.repetition)),
            key=lambda o: (o.agent_id, o.task_id),
        )
    }
    cells = tuple(
        _cell(agent.agent_id, task_id, by_cell.get((agent.agent_id, task_id), []))
        for agent in facts.agents
        for task_id in facts.task_ids
    )
    agents = tuple(
        _agent(agent, [o for o in observations if o.agent_id == agent.agent_id], cells, facts)
        for agent in facts.agents
    )
    tasks = tuple(_task(task_id, observations, facts) for task_id in facts.task_ids)
    counts = OutcomeCounts.of([o.outcome for o in observations])
    partial = facts.status != "completed" or counts.not_started > 0 or counts.incomplete > 0
    violations = tuple(_invariant_violations(counts, len(observations)))
    warnings = tuple(_warnings(counts, partial, tasks, violations))
    runs_per_hour = (
        counts.finished / facts.running_seconds * 3600
        if facts.running_seconds and counts.finished
        else None
    )
    return ExperimentSummary(
        experiment_id=facts.experiment_id,
        spec_id=facts.spec_id,
        name=facts.name,
        status=facts.status,
        partial=partial,
        generated_at=generated_at,
        workers=facts.workers,
        repetitions=facts.repetitions,
        counts=counts,
        running_seconds=facts.running_seconds,
        runs_per_hour=round(runs_per_hour, 2) if runs_per_hour is not None else None,
        agents=agents,
        tasks=tasks,
        cells=cells,
        failures=_failure_counts(observations),
        warnings=warnings,
        results_valid=not warnings,
        invariant_violations=violations,
    )


# ------------------------------------------------------------------------- building blocks


def _rate(successes: int, trials: int, *, note: str | None = None, interval: bool = True) -> Rate:
    if trials == 0:
        return Rate(successes=0, trials=0, estimate=None, note="no scored runs")
    bounds = wilson_interval(successes, trials) if interval else None
    return Rate(
        successes=successes,
        trials=trials,
        estimate=successes / trials,
        ci_low=bounds.low if bounds else None,
        ci_high=bounds.high if bounds else None,
        note=note,
    )


def _scored(observations: Sequence[RunObservation]) -> list[RunObservation]:
    return [o for o in observations if o.outcome.is_scored]


def _cell(agent_id: str, task_id: str, observations: list[RunObservation]) -> CellSummary:
    scored = _scored(observations)
    passed = sum(o.outcome is Outcome.PASSED for o in scored)
    identical = len(scored) >= 2 and (
        len({o.outcome for o in scored}) == 1
        and None not in {o.patch_sha256 for o in scored}
        and len({o.patch_sha256 for o in scored}) == 1
    )
    note = "identical repetitions (effective n = 1): no interval" if identical else None
    return CellSummary(
        agent_id=agent_id,
        task_id=task_id,
        counts=OutcomeCounts.of([o.outcome for o in observations]),
        success=_rate(passed, len(scored), note=note, interval=not identical),
        identical_repetitions=identical,
    )


def _distribution(values: list[float], censored: int) -> Distribution | None:
    summary = describe(values)
    if summary is None:
        return None
    return Distribution(
        count=summary.count,
        mean=round(summary.mean, 3),
        median=round(summary.median, 3),
        minimum=round(summary.minimum, 3),
        maximum=round(summary.maximum, 3),
        p95=round(summary.p95, 3) if summary.count >= MIN_RUNS_FOR_P95 else None,
        censored=censored,
    )


def _agent_seconds(result: RunResult) -> float | None:
    timing = next((t for t in result.stage_timings if t.status is RunStatus.RUNNING_AGENT), None)
    return timing.duration_ms / 1000 if timing else None


def _agent(
    info: AgentInfo,
    observations: list[RunObservation],
    cells: tuple[CellSummary, ...],
    facts: ExperimentFacts,
) -> AgentSummary:
    scored = _scored(observations)
    passed = sum(o.outcome is Outcome.PASSED for o in scored)
    own_cells = [c for c in cells if c.agent_id == info.agent_id]
    repeated = [c for c in own_cells if c.success.trials >= 2]
    deterministic = bool(repeated) and all(c.identical_repetitions for c in repeated)
    empty_tasks = tuple(c.task_id for c in own_cells if c.success.trials == 0)
    rates = [c.success.estimate for c in own_cells if c.success.estimate is not None]
    task_mean = sum(rates) / len(rates) if rates and not empty_tasks else None

    note = "pooled over this experiment's tasks (conditional on this task mix)"
    if deterministic:
        note = "deterministic: repetitions were identical, so no interval"
    results = [o.result for o in scored if o.result is not None]
    durations = [r.duration_ms / 1000 for r in results]
    agent_seconds = [s for r in results if (s := _agent_seconds(r)) is not None]
    censored = sum(o.outcome is Outcome.AGENT_TIMEOUT for o in scored)
    return AgentSummary(
        agent_id=info.agent_id,
        role=info.role,
        adapter=info.adapter,
        model=info.model,
        counts=OutcomeCounts.of([o.outcome for o in observations]),
        success=_rate(passed, len(scored), note=note, interval=not deterministic),
        task_mean_success=round(task_mean, 6) if task_mean is not None else None,
        tasks_without_scored_runs=empty_tasks,
        deterministic=deterministic,
        pass_at_k=_pass_at_k(own_cells, facts.repetitions, deterministic),
        run_seconds=_distribution(durations, censored),
        agent_seconds=_distribution(agent_seconds, censored),
        agent_budget_seconds=(
            (info.budget_override_seconds,) if info.budget_override_seconds else ()
        ),
        tokens=_tokens(results),
    )


def _pass_at_k(cells: list[CellSummary], repetitions: int, deterministic: bool) -> PassAtK | None:
    k = repetitions
    if k < 2:
        return None
    if deterministic:
        return PassAtK(k=k, value=None, note="deterministic agent: pass@k equals pass@1")
    short = [c.task_id for c in cells if c.success.trials < k]
    if short:
        return PassAtK(k=k, value=None, note=f"fewer than {k} scored runs on: {', '.join(short)}")
    values = [pass_at_k(c.success.trials, c.success.successes, k) for c in cells]
    return PassAtK(k=k, value=round(sum(values) / len(values), 6))


def _tokens(results: list[RunResult]) -> TokenUsage | None:
    prompt = completion = 0
    generation_seconds = 0.0
    reported = False
    for result in results:
        metadata = result.agent.metadata if result.agent else {}
        p, c, g = (
            metadata.get("prompt_tokens"),
            metadata.get("completion_tokens"),
            metadata.get("generation_seconds"),
        )
        if isinstance(p, int) and isinstance(c, int):
            reported = True
            prompt, completion = prompt + p, completion + c
            if isinstance(g, (int, float)):
                generation_seconds += float(g)
    if not reported:
        return None
    speed = round(completion / generation_seconds, 2) if generation_seconds else None
    return TokenUsage(
        prompt_tokens=prompt, completion_tokens=completion, generation_tokens_per_second=speed
    )


def _task(
    task_id: str, observations: Sequence[RunObservation], facts: ExperimentFacts
) -> TaskSummary:
    roles = {agent.agent_id: agent.role for agent in facts.agents}
    of_task = [o for o in observations if o.task_id == task_id]
    subjects = [o for o in of_task if roles.get(o.agent_id) == "subject"]
    scored = _scored(subjects)

    def all_outcomes(role: str, outcome: Outcome, expected: bool) -> bool | None:
        runs = [o for o in of_task if roles.get(o.agent_id) == role and o.outcome.is_scored]
        if not runs:
            return None
        return all((o.outcome is outcome) is expected for o in runs)

    return TaskSummary(
        task_id=task_id,
        counts=OutcomeCounts.of([o.outcome for o in subjects]),
        success=_rate(
            sum(o.outcome is Outcome.PASSED for o in scored),
            len(scored),
            note="pooled over subject agents",
        ),
        reference_passed_all=all_outcomes("reference", Outcome.PASSED, True),
        baseline_failed_all=all_outcomes("baseline", Outcome.PASSED, False),
    )


def _failure_counts(observations: Sequence[RunObservation]) -> tuple[FailureCount, ...]:
    keys: list[tuple[str, str, str | None]] = []
    for o in observations:
        outcome = o.outcome
        if outcome in {Outcome.PASSED, Outcome.NOT_STARTED, Outcome.INCOMPLETE}:
            continue
        if outcome is Outcome.FAILED or o.result is None or o.result.failure is None:
            keys.append((outcome.value, "graded_failure", None))
        else:
            failure = o.result.failure
            keys.append((outcome.value, failure.reason.value, failure.stage.value))
    return tuple(
        FailureCount(outcome=outcome, reason=reason, stage=stage, count=keys.count(key))
        for key in sorted(set(keys), key=lambda k: (k[0], k[1], k[2] or ""))
        for outcome, reason, stage in [key]
    )


def _invariant_violations(counts: OutcomeCounts, planned: int) -> list[str]:
    violations = []
    if counts.planned != planned:
        violations.append(f"outcome counts cover {counts.planned} runs, {planned} were planned")
    if counts.started + counts.not_started != counts.planned:
        violations.append("started + not_started != planned")
    if counts.finished + counts.incomplete != counts.started:
        violations.append("finished + incomplete != started")
    if counts.scored + counts.excluded + counts.not_started + counts.incomplete != counts.planned:
        violations.append("scored + excluded + not_started + incomplete != planned")
    return violations


def _warnings(
    counts: OutcomeCounts,
    partial: bool,
    tasks: tuple[TaskSummary, ...],
    violations: tuple[str, ...],
) -> list[str]:
    warnings = list(violations)
    if partial:
        warnings.append(
            f"partial experiment: {counts.not_started} runs not started and "
            f"{counts.incomplete} incomplete; rates cover only the runs that finished"
        )
    if counts.infrastructure_error or counts.agent_unavailable:
        warnings.append(
            f"{counts.infrastructure_error + counts.agent_unavailable} runs failed for reasons "
            "outside the agents' control and are excluded from success rates"
        )
    for task in tasks:
        if task.reference_passed_all is False:
            warnings.append(
                f"task {task.task_id}: the reference agent did not pass every run; the task "
                "or the infrastructure may be broken"
            )
        if task.baseline_failed_all is False:
            warnings.append(
                f"task {task.task_id}: a baseline agent passed; the task does not discriminate"
            )
    return warnings
