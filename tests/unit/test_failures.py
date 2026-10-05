"""Failure classification."""

from __future__ import annotations

import pytest

from agentenvforge.domain.failures import (
    AgentTimeoutError,
    FailureInfo,
    FailureReason,
    GraderTimeoutError,
    SandboxBuildTimeoutError,
    TaskValidationError,
    ValidationIssue,
    terminal_status_for,
)
from agentenvforge.domain.status import RunStatus


@pytest.mark.parametrize("reason", list(FailureReason))
def test_every_reason_maps_to_a_failure_terminal_state(reason: FailureReason) -> None:
    status = terminal_status_for(reason)
    assert status.is_terminal and status.is_failure


def test_classified_error_keeps_reason_and_details() -> None:
    error = AgentTimeoutError("too slow", details={"limit_seconds": 5})
    info = FailureInfo.from_exception(error, RunStatus.RUNNING_AGENT)
    assert info.reason is FailureReason.AGENT_TIMEOUT
    assert info.stage is RunStatus.RUNNING_AGENT
    assert info.details == {"limit_seconds": 5}
    assert terminal_status_for(info.reason) is RunStatus.TIMED_OUT


def test_subclass_reasons_are_specific() -> None:
    assert SandboxBuildTimeoutError.reason is FailureReason.SANDBOX_BUILD_TIMEOUT
    assert GraderTimeoutError.reason is FailureReason.GRADER_TIMEOUT


def test_unexpected_exception_is_internal_and_named() -> None:
    info = FailureInfo.from_exception(
        ZeroDivisionError("division by zero"), RunStatus.RUNNING_GRADER
    )
    assert info.reason is FailureReason.INTERNAL
    assert info.error_type == "ZeroDivisionError"
    assert "division by zero" in info.message


def test_keyboard_interrupt_is_cancellation() -> None:
    info = FailureInfo.from_exception(KeyboardInterrupt(), RunStatus.RUNNING_AGENT)
    assert info.reason is FailureReason.CANCELLED


def test_validation_error_message_lists_every_issue() -> None:
    error = TaskValidationError(
        "task.yaml",
        [
            ValidationIssue(field="resources.memory_mb", reason="too small", received="-20"),
            ValidationIssue(field="id", reason="missing"),
        ],
    )
    assert "field: resources.memory_mb" in str(error)
    assert "received: -20" in str(error)
    assert "2 validation issue(s)" in str(error)
