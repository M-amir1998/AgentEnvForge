"""The aggregated results of an experiment (serialised as ``summary.json``).

Every statistic carries its sample size, and statistics that cannot be computed honestly
(no scored runs, too few runs, identical repetitions) are ``None`` with a ``note`` saying
why, instead of a misleading number. The methodology is documented in ADR-0009 and
docs/architecture/experiments.md.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from agentenvforge.analysis.outcomes import SCORED_OUTCOMES, Outcome


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class OutcomeCounts(_Model):
    """How many planned runs ended in each outcome."""

    passed: int = 0
    failed: int = 0
    agent_timeout: int = 0
    solution_timeout: int = 0
    agent_error: int = 0
    post_agent_error: int = 0
    infrastructure_error: int = 0
    agent_unavailable: int = 0
    cancelled: int = 0
    not_started: int = 0
    incomplete: int = 0

    @classmethod
    def of(cls, outcomes: list[Outcome]) -> OutcomeCounts:
        """Count ``outcomes``."""
        return cls(**{outcome.value: outcomes.count(outcome) for outcome in Outcome})

    def count(self, outcome: Outcome) -> int:
        """Number of runs with ``outcome``."""
        return int(getattr(self, outcome.value))

    @property
    def planned(self) -> int:
        """All planned runs."""
        return sum(self.count(outcome) for outcome in Outcome)

    @property
    def scored(self) -> int:
        """Runs whose outcome is attributable to the agent (the success-rate denominator)."""
        return sum(self.count(outcome) for outcome in SCORED_OUTCOMES)

    @property
    def unscored(self) -> int:
        """Runs excluded from success rates."""
        return self.planned - self.scored

    @property
    def started(self) -> int:
        """Runs that were started."""
        return self.planned - self.not_started

    @property
    def finished(self) -> int:
        """Started runs that recorded a result."""
        return self.started - self.incomplete

    @property
    def excluded(self) -> int:
        """Started runs excluded from success rates (infrastructure, unavailable, cancelled)."""
        return self.infrastructure_error + self.agent_unavailable + self.cancelled


class Rate(_Model):
    """A success proportion with a 95% Wilson score interval."""

    successes: int
    trials: int
    estimate: float | None
    ci_low: float | None = None
    ci_high: float | None = None
    confidence: float = 0.95
    note: str | None = None


class Distribution(_Model):
    """Summary of durations in seconds."""

    count: int
    mean: float
    median: float
    minimum: float
    maximum: float
    p95: float | None
    """``None`` below :data:`~agentenvforge.analysis.aggregate.MIN_RUNS_FOR_P95` runs."""
    censored: int = 0
    """Runs that hit their time budget: their true duration is unknown (at least the budget),
    so the mean and the upper percentiles are lower bounds when this is non-zero."""


class PassAtK(_Model):
    """Mean over tasks of the unbiased pass@k estimator (Chen et al., 2021)."""

    k: int
    value: float | None
    note: str | None = None


class CellSummary(_Model):
    """One (agent, task) cell: the unit every interval is computed on."""

    agent_id: str
    task_id: str
    counts: OutcomeCounts
    success: Rate
    identical_repetitions: bool
    """All scored repetitions had the same outcome and the same patch: the agent behaved
    deterministically, so the repetitions are effectively one observation."""


class TokenUsage(_Model):
    """Model usage reported by the agent adapter (only when it reports it)."""

    prompt_tokens: int
    completion_tokens: int
    generation_tokens_per_second: float | None = None


class AgentSummary(_Model):
    """Results of one agent over all tasks."""

    agent_id: str
    role: str
    adapter: str
    model: str | None = None
    counts: OutcomeCounts
    success: Rate
    """Pooled over this experiment's tasks: an estimate *conditional on this task mix*, not a
    general capability estimate."""
    task_mean_success: float | None
    """Unweighted mean of the per-task success rates (equals ``success`` when every task has
    the same number of scored runs)."""
    tasks_without_scored_runs: tuple[str, ...] = ()
    deterministic: bool = False
    """*Observed*: every repeated cell had identical repetitions (same outcome and patch).
    A deterministic agent looks like this, but so does a sampled one that converges on the
    same patch every time; both are counted conservatively, as one observation per task."""
    pass_at_k: PassAtK | None = None
    run_seconds: Distribution | None = None
    agent_seconds: Distribution | None = None
    agent_budget_seconds: tuple[int, ...] = ()
    tokens: TokenUsage | None = None


class TaskSummary(_Model):
    """Results on one task, pooled over *subject* agents only."""

    task_id: str
    counts: OutcomeCounts
    success: Rate
    reference_passed_all: bool | None = None
    """``None`` without a reference agent; otherwise whether it passed every repetition."""
    baseline_failed_all: bool | None = None
    """``None`` without a baseline agent; otherwise whether it failed every repetition."""


class FailureCount(_Model):
    """How often runs ended with one outcome for one failure reason in one stage."""

    outcome: str
    reason: str
    stage: str | None
    count: int


class ExperimentSummary(_Model):
    """Everything computed about an experiment (``summary.json``)."""

    summary_version: int = 1
    experiment_id: str
    spec_id: str
    name: str | None = None
    status: str
    partial: bool
    """True unless every planned run executed: rates are then over the runs that did."""
    generated_at: datetime
    workers: int
    repetitions: int
    counts: OutcomeCounts
    running_seconds: float | None
    """Wall-clock duration of the RUNNING phase (scheduling to the last run's completion)."""
    runs_per_hour: float | None
    """Finished runs per hour of running time (all outcomes)."""
    agents: tuple[AgentSummary, ...]
    tasks: tuple[TaskSummary, ...]
    cells: tuple[CellSummary, ...]
    failures: tuple[FailureCount, ...]
    warnings: tuple[str, ...] = ()
    results_valid: bool
    """False when any warning undermines the results (excluded runs, partial execution,
    a reference agent failing, a baseline passing, or a violated count invariant)."""
    invariant_violations: tuple[str, ...] = ()
