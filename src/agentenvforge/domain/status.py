"""The run lifecycle state machine.

A run moves forward through a fixed sequence of *active* states. Whatever happens, it
always passes through ``CLEANING_UP`` before reaching exactly one *terminal* state. This
guarantees that every terminal run has had cleanup attempted, which is what keeps
orphaned containers from accumulating.

::

    CREATED -> VALIDATING -> PREPARING_SOURCE -> BUILDING_SANDBOX -> STARTING_SANDBOX
      -> RUNNING_AGENT -> CAPTURING_SOLUTION -> RUNNING_GRADER -> COLLECTING_ARTIFACTS
      -> CLEANING_UP -> COMPLETED | FAILED_* | TIMED_OUT | CANCELLED | INTERNAL_ERROR

Any active state may jump straight to ``CLEANING_UP`` when a failure occurs.

Note that ``COMPLETED`` describes the *infrastructure* outcome: the run executed and the
grader produced a verdict. Whether the agent actually solved the task is the separate
``passed`` field of the grade. Keeping the two apart lets benchmarks distinguish "the
agent failed" from "our infrastructure failed", which matter very differently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from agentenvforge.domain.clock import utc_now


class RunStatus(StrEnum):
    """Every state a run can be in. Values are stable and persisted."""

    CREATED = "created"
    VALIDATING = "validating"
    PREPARING_SOURCE = "preparing_source"
    BUILDING_SANDBOX = "building_sandbox"
    STARTING_SANDBOX = "starting_sandbox"
    RUNNING_AGENT = "running_agent"
    CAPTURING_SOLUTION = "capturing_solution"
    RUNNING_GRADER = "running_grader"
    COLLECTING_ARTIFACTS = "collecting_artifacts"
    CLEANING_UP = "cleaning_up"

    COMPLETED = "completed"
    FAILED_VALIDATION = "failed_validation"
    FAILED_SOURCE = "failed_source"
    FAILED_BUILD = "failed_build"
    FAILED_SANDBOX = "failed_sandbox"
    FAILED_AGENT = "failed_agent"
    FAILED_GRADER = "failed_grader"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"

    @property
    def is_terminal(self) -> bool:
        """True when no further transitions are possible."""
        return self in TERMINAL_STATUSES

    @property
    def is_failure(self) -> bool:
        """True for terminal states other than ``COMPLETED``."""
        return self.is_terminal and self is not RunStatus.COMPLETED


ACTIVE_SEQUENCE: tuple[RunStatus, ...] = (
    RunStatus.CREATED,
    RunStatus.VALIDATING,
    RunStatus.PREPARING_SOURCE,
    RunStatus.BUILDING_SANDBOX,
    RunStatus.STARTING_SANDBOX,
    RunStatus.RUNNING_AGENT,
    RunStatus.CAPTURING_SOLUTION,
    RunStatus.RUNNING_GRADER,
    RunStatus.COLLECTING_ARTIFACTS,
    RunStatus.CLEANING_UP,
)

TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED_VALIDATION,
        RunStatus.FAILED_SOURCE,
        RunStatus.FAILED_BUILD,
        RunStatus.FAILED_SANDBOX,
        RunStatus.FAILED_AGENT,
        RunStatus.FAILED_GRADER,
        RunStatus.TIMED_OUT,
        RunStatus.CANCELLED,
        RunStatus.INTERNAL_ERROR,
    }
)


def _build_transition_table() -> dict[RunStatus, frozenset[RunStatus]]:
    table: dict[RunStatus, frozenset[RunStatus]] = {}
    for index, status in enumerate(ACTIVE_SEQUENCE[:-1]):
        following = ACTIVE_SEQUENCE[index + 1]
        table[status] = frozenset({following, RunStatus.CLEANING_UP})
    table[RunStatus.CLEANING_UP] = TERMINAL_STATUSES
    for terminal in TERMINAL_STATUSES:
        table[terminal] = frozenset()
    return table


ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = _build_transition_table()


class InvalidTransitionError(RuntimeError):
    """Raised when code attempts a lifecycle transition the state machine forbids.

    This always indicates a programming error in the orchestrator, never bad user input.
    """

    def __init__(self, current: RunStatus, requested: RunStatus) -> None:
        super().__init__(f"invalid run transition {current.value} -> {requested.value}")
        self.current = current
        self.requested = requested


def can_transition(current: RunStatus, requested: RunStatus) -> bool:
    """Return whether ``current -> requested`` is a legal transition."""
    return requested in ALLOWED_TRANSITIONS[current]


@dataclass(frozen=True, slots=True)
class StatusChange:
    """One entry in a run's status history."""

    status: RunStatus
    entered_at: datetime


@dataclass(slots=True)
class RunStateMachine:
    """Tracks the current status of one run and rejects illegal transitions.

    The machine is deliberately tiny and synchronous: it owns *which* state a run is in,
    while the orchestrator owns *what work* happens in each state.
    """

    history: list[StatusChange] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append(StatusChange(RunStatus.CREATED, utc_now()))

    @property
    def current(self) -> RunStatus:
        """The status the run is in right now."""
        return self.history[-1].status

    def transition(self, requested: RunStatus, *, at: datetime | None = None) -> StatusChange:
        """Move to ``requested`` or raise :class:`InvalidTransitionError`."""
        if not can_transition(self.current, requested):
            raise InvalidTransitionError(self.current, requested)
        change = StatusChange(requested, at or utc_now())
        self.history.append(change)
        return change

    def visited(self, status: RunStatus) -> bool:
        """Return whether the run has ever been in ``status``."""
        return any(change.status is status for change in self.history)
