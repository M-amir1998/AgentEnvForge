"""The execution event model (see ADR-005).

Events are an append-only, ordered record of everything that happens during a run. They
are persisted as they occur, so even a run whose process crashed leaves a trace showing
how far it got. Events carry small scalar metadata only; bulky data (command output,
patches, reports) lives in artifacts and is referenced by name.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from agentenvforge.domain.status import RunStatus
from agentenvforge.domain.types import Metadata


class EventType(StrEnum):
    """All event types. Values use ``<subject>.<verb>`` naming and are stable."""

    RUN_CREATED = "run.created"
    RUN_STATUS_CHANGED = "run.status_changed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"

    TASK_VALIDATED = "task.validated"

    SOURCE_PREPARATION_STARTED = "source.preparation.started"
    SOURCE_PREPARATION_COMPLETED = "source.preparation.completed"

    SANDBOX_BUILD_STARTED = "sandbox.build.started"
    SANDBOX_BUILD_COMPLETED = "sandbox.build.completed"
    SANDBOX_STARTED = "sandbox.started"
    SANDBOX_CLEANUP_STARTED = "sandbox.cleanup.started"
    SANDBOX_CLEANUP_COMPLETED = "sandbox.cleanup.completed"
    SANDBOX_CLEANUP_FAILED = "sandbox.cleanup.failed"

    SETUP_STARTED = "setup.started"
    SETUP_COMPLETED = "setup.completed"

    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"

    COMMAND_STARTED = "command.started"
    COMMAND_COMPLETED = "command.completed"

    SOLUTION_CAPTURED = "solution.captured"

    GRADER_STARTED = "grader.started"
    GRADER_COMPLETED = "grader.completed"

    ARTIFACT_STORED = "artifact.stored"


class ExecutionEvent(BaseModel):
    """One immutable event in a run's history."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    run_id: str
    task_id: str
    sequence: int
    """Strictly increasing per run; gives a total order even when timestamps collide."""
    timestamp: datetime
    event_type: EventType
    status: RunStatus
    """The lifecycle state the run was in when the event was emitted."""
    duration_ms: int | None = None
    data: Metadata = {}
