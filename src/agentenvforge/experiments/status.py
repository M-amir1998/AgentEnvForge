"""The experiment lifecycle state machine.

It mirrors the run lifecycle (:mod:`agentenvforge.domain.status`): an experiment moves
forward through its active states and always passes through ``FINALIZING`` before reaching
exactly one terminal state, so every finished experiment has had its summary computed and
persisted, including cancelled ones::

    CREATED -> PREPARING -> RUNNING -> FINALIZING -> COMPLETED | CANCELLED | FAILED
                   |           |  \\                    ^
                   |           |   -> CANCELLING ------/
                   +-----------+----> FINALIZING (preparation failed or cancelled early)

``COMPLETED`` means every planned run was executed; it says nothing about how many runs the
agents passed. ``FAILED`` means the experiment itself could not proceed (for example, an agent
preflight check or an image build failed), not that agents failed tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from agentenvforge.domain.clock import utc_now


class ExperimentStatus(StrEnum):
    """Every state an experiment can be in. Values are stable and persisted."""

    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    CANCELLING = "cancelling"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """True when no further transitions are possible."""
        return self in _TERMINAL


_TERMINAL = frozenset(
    {ExperimentStatus.COMPLETED, ExperimentStatus.CANCELLED, ExperimentStatus.FAILED}
)

ALLOWED_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.CREATED: frozenset({ExperimentStatus.PREPARING, ExperimentStatus.FINALIZING}),
    ExperimentStatus.PREPARING: frozenset({ExperimentStatus.RUNNING, ExperimentStatus.FINALIZING}),
    ExperimentStatus.RUNNING: frozenset({ExperimentStatus.CANCELLING, ExperimentStatus.FINALIZING}),
    ExperimentStatus.CANCELLING: frozenset({ExperimentStatus.FINALIZING}),
    ExperimentStatus.FINALIZING: _TERMINAL,
    ExperimentStatus.COMPLETED: frozenset(),
    ExperimentStatus.CANCELLED: frozenset(),
    ExperimentStatus.FAILED: frozenset(),
}


class InvalidExperimentTransitionError(RuntimeError):
    """An illegal experiment transition was attempted (always a programming error)."""

    def __init__(self, current: ExperimentStatus, requested: ExperimentStatus) -> None:
        super().__init__(f"invalid experiment transition {current.value} -> {requested.value}")
        self.current = current
        self.requested = requested


@dataclass(frozen=True, slots=True)
class ExperimentStatusChange:
    """One entry in an experiment's status history."""

    status: ExperimentStatus
    entered_at: datetime


@dataclass(slots=True)
class ExperimentStateMachine:
    """Tracks an experiment's status and rejects illegal transitions."""

    history: list[ExperimentStatusChange] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append(ExperimentStatusChange(ExperimentStatus.CREATED, utc_now()))

    @property
    def current(self) -> ExperimentStatus:
        """The status the experiment is in right now."""
        return self.history[-1].status

    def transition(self, requested: ExperimentStatus) -> ExperimentStatusChange:
        """Move to ``requested`` or raise :class:`InvalidExperimentTransitionError`."""
        if requested not in ALLOWED_TRANSITIONS[self.current]:
            raise InvalidExperimentTransitionError(self.current, requested)
        change = ExperimentStatusChange(requested, utc_now())
        self.history.append(change)
        return change

    def entered(self, status: ExperimentStatus) -> datetime | None:
        """When the experiment first entered ``status``, if it did."""
        return next((c.entered_at for c in self.history if c.status is status), None)
