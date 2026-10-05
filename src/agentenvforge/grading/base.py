"""The grader extension point.

A grader inspects the agent's solution inside the *grading sandbox* and returns a
structured :class:`~agentenvforge.domain.results.GradeResult`.

Two outcomes must never be confused:

* the solution is wrong -> return a ``GradeResult`` with ``passed=False`` (an agent
  failure, a perfectly normal benchmark outcome);
* the grader could not judge -> raise :class:`~agentenvforge.domain.failures.GraderError`
  (an infrastructure or task-authoring failure; the run ends as ``FAILED_GRADER``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

from agentenvforge.agents.base import ArtifactWriter
from agentenvforge.domain.results import GradeResult
from agentenvforge.sandbox.session import SandboxSession


@dataclass(frozen=True, slots=True)
class GradingContext:
    """What a grader receives."""

    run_id: str
    task_id: str
    session: SandboxSession
    """Session on the fresh grading sandbox (never the agent's sandbox)."""
    workspace: PurePosixPath
    assets_dir: PurePosixPath | None
    """Where hidden grader assets were placed, if the task has any."""
    save_artifact: ArtifactWriter


@runtime_checkable
class Grader(Protocol):
    """Public extension point: decide whether a solution is correct."""

    @property
    def name(self) -> str:
        """Unique name within the task (used for artifacts and reports)."""
        ...

    @property
    def grader_type(self) -> str:
        """Kind of grader, e.g. ``pytest``."""
        ...

    def grade(self, context: GradingContext) -> GradeResult:
        """Evaluate the solution, or raise ``GraderError`` if it cannot be evaluated."""
        ...
