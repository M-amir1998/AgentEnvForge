"""Structured outcomes of agents, graders and complete runs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agentenvforge.domain.failures import FailureInfo
from agentenvforge.domain.status import RunStatus
from agentenvforge.domain.types import Metadata


class AgentOutcome(StrEnum):
    """How the agent's own process ended. Independent of whether the task was solved."""

    COMPLETED = "completed"
    """The agent ran to completion and reported success (e.g. exit status 0)."""
    FAILED = "failed"
    """The agent ran but reported failure (e.g. non-zero exit). Its work is still graded."""


class AgentResult(BaseModel):
    """What an :class:`~agentenvforge.agents.base.AgentAdapter` reports after running."""

    model_config = ConfigDict(frozen=True)

    adapter: str
    outcome: AgentOutcome
    exit_code: int | None = None
    summary: str = ""
    duration_ms: int = 0
    command_count: int = 0
    metadata: Metadata = {}


class GradeResult(BaseModel):
    """A grader's verdict. Deliberately richer than a boolean.

    ``score`` is normalised to ``[0, 1]`` so results are comparable across graders.
    Test counters are ``None`` when the grader has no notion of individual tests.
    """

    model_config = ConfigDict(frozen=True)

    grader: str
    grader_type: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    tests_passed: int | None = None
    tests_failed: int | None = None
    tests_errored: int | None = None
    tests_skipped: int | None = None
    duration_ms: int = 0
    summary: str = ""
    details: Metadata = {}
    components: tuple[GradeResult, ...] = ()
    """Sub-results when this grade aggregates several graders."""


class StageTiming(BaseModel):
    """Wall-clock time spent in one lifecycle state."""

    model_config = ConfigDict(frozen=True)

    status: RunStatus
    started_at: datetime
    duration_ms: int


class CleanupReport(BaseModel):
    """Outcome of releasing sandboxes and temporary files.

    Cleanup errors never replace the run's primary failure; they are reported here so
    users still see the root cause while learning that something may have leaked.
    """

    model_config = ConfigDict(frozen=True)

    succeeded: bool = True
    sandboxes_removed: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class RunResult(BaseModel):
    """The final, persisted summary of one run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    task_id: str
    status: RunStatus
    passed: bool | None = None
    """Whether the agent solved the task; ``None`` if grading never produced a verdict."""
    score: float | None = None
    failure: FailureInfo | None = None
    agent: AgentResult | None = None
    grade: GradeResult | None = None
    stage_timings: tuple[StageTiming, ...] = ()
    cleanup: CleanupReport = CleanupReport()
    created_at: datetime
    finished_at: datetime
    duration_ms: int

    @property
    def succeeded(self) -> bool:
        """True when the infrastructure completed *and* the agent passed the grader."""
        return self.status is RunStatus.COMPLETED and self.passed is True
