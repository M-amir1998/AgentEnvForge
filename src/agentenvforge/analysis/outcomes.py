"""How a run counts in experiment statistics.

Every planned run gets exactly one :class:`Outcome`, derived from data every run already
persists (its grade and its failure's reason and stage); no new failure vocabulary is
invented here. The rule is about *what the agent could have influenced*:

1. A grade, once produced, stands: ``PASSED`` or ``FAILED``, whatever happened afterwards.
2. Failures before the agent started (validation, source, image build, sandbox start, setup
   commands) are **unscored**: the agent never had a chance. So are failures to start a
   sandbox at any stage, cancellations, and an unreachable model server
   (``AGENT_UNAVAILABLE``), none of which says anything about the agent.
3. Everything else that ends a run without a passing grade after the agent started is a
   **scored failure**, broken down as ``AGENT_TIMEOUT``, ``SOLUTION_TIMEOUT`` (the grader
   timed out: the solution made the tests hang), ``AGENT_ERROR`` (the adapter crashed) or
   ``POST_AGENT_ERROR`` (the grader could not judge, or the workspace could not be captured,
   after the agent acted - often because of what the agent did, e.g. a corrupted
   ``pyproject.toml`` or a runaway workspace).

Rule 3 deliberately counts ambiguous post-agent failures *against* the agent: excluding
them would raise the success rates of exactly the agents that break things. The counts of
each category are always reported, so readers can see how much they matter.

Success rate = ``PASSED / scored``. Unscored runs are excluded from the denominator and
reported separately; a high count is flagged as a validity problem.
"""

from __future__ import annotations

from enum import StrEnum

from agentenvforge.domain.failures import FailureReason
from agentenvforge.domain.results import RunResult
from agentenvforge.domain.status import RunStatus


class Outcome(StrEnum):
    """The statistical outcome of one planned run. Values are stable and exported."""

    PASSED = "passed"
    FAILED = "failed"
    AGENT_TIMEOUT = "agent_timeout"
    SOLUTION_TIMEOUT = "solution_timeout"
    AGENT_ERROR = "agent_error"
    POST_AGENT_ERROR = "post_agent_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    AGENT_UNAVAILABLE = "agent_unavailable"
    CANCELLED = "cancelled"
    NOT_STARTED = "not_started"
    INCOMPLETE = "incomplete"

    @property
    def is_scored(self) -> bool:
        """True when the outcome is attributable to the agent and counts in success rates."""
        return self in SCORED_OUTCOMES


SCORED_OUTCOMES = frozenset(
    {
        Outcome.PASSED,
        Outcome.FAILED,
        Outcome.AGENT_TIMEOUT,
        Outcome.SOLUTION_TIMEOUT,
        Outcome.AGENT_ERROR,
        Outcome.POST_AGENT_ERROR,
    }
)

PRE_AGENT_STAGES = frozenset(
    {
        RunStatus.CREATED,
        RunStatus.VALIDATING,
        RunStatus.PREPARING_SOURCE,
        RunStatus.BUILDING_SANDBOX,
        RunStatus.STARTING_SANDBOX,
    }
)
"""Stages in which a failure cannot have been caused by the agent."""


_UNSCORED_BY_REASON: dict[FailureReason, Outcome] = {
    FailureReason.CANCELLED: Outcome.CANCELLED,
    FailureReason.AGENT_UNAVAILABLE: Outcome.AGENT_UNAVAILABLE,
    # Even in the grading stage: the grader container starts before the solution is copied in.
    FailureReason.SANDBOX_START: Outcome.INFRASTRUCTURE_ERROR,
}
_SCORED_BY_REASON: dict[FailureReason, Outcome] = {
    FailureReason.AGENT_TIMEOUT: Outcome.AGENT_TIMEOUT,
    FailureReason.GRADER_TIMEOUT: Outcome.SOLUTION_TIMEOUT,
    FailureReason.AGENT_ERROR: Outcome.AGENT_ERROR,
}


def classify(result: RunResult | None, *, started: bool) -> Outcome:
    """Return the outcome of a planned run (``result`` is ``None`` if it has none)."""
    if result is None:
        return Outcome.INCOMPLETE if started else Outcome.NOT_STARTED
    if result.grade is not None:
        return Outcome.PASSED if result.grade.passed else Outcome.FAILED
    failure = result.failure
    if failure is None:  # pragma: no cover - a run without a grade always has a failure
        return Outcome.INFRASTRUCTURE_ERROR
    if failure.reason in _UNSCORED_BY_REASON:
        return _UNSCORED_BY_REASON[failure.reason]
    if failure.stage in PRE_AGENT_STAGES:
        return Outcome.INFRASTRUCTURE_ERROR
    return _SCORED_BY_REASON.get(failure.reason, Outcome.POST_AGENT_ERROR)
