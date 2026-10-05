"""Failure taxonomy.

Every failure that can end a run is classified by a :class:`FailureReason`. Code raises a
subclass of :class:`AgentEnvForgeError`; the orchestrator converts it into a
:class:`FailureInfo` record and picks the matching terminal :class:`RunStatus`.

Exceptions are *chained* (``raise ... from exc``) rather than replaced, so the original
root cause and traceback stay available for debugging.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from agentenvforge.domain.status import RunStatus
from agentenvforge.domain.types import JSONScalar, Metadata


class FailureReason(StrEnum):
    """Why a run ended unsuccessfully. Values are stable and persisted."""

    TASK_VALIDATION = "task_validation"
    SOURCE_PREPARATION = "source_preparation"
    SANDBOX_BUILD = "sandbox_build"
    SANDBOX_BUILD_TIMEOUT = "sandbox_build_timeout"
    SANDBOX_START = "sandbox_start"
    SETUP_COMMAND = "setup_command"
    AGENT_ERROR = "agent_error"
    AGENT_TIMEOUT = "agent_timeout"
    GRADER_ERROR = "grader_error"
    GRADER_TIMEOUT = "grader_timeout"
    ARTIFACT_COLLECTION = "artifact_collection"
    SANDBOX_INFRASTRUCTURE = "sandbox_infrastructure"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


_TERMINAL_STATUS_BY_REASON: dict[FailureReason, RunStatus] = {
    FailureReason.TASK_VALIDATION: RunStatus.FAILED_VALIDATION,
    FailureReason.SOURCE_PREPARATION: RunStatus.FAILED_SOURCE,
    FailureReason.SANDBOX_BUILD: RunStatus.FAILED_BUILD,
    FailureReason.SANDBOX_BUILD_TIMEOUT: RunStatus.FAILED_BUILD,
    FailureReason.SANDBOX_START: RunStatus.FAILED_SANDBOX,
    FailureReason.SETUP_COMMAND: RunStatus.FAILED_SANDBOX,
    FailureReason.SANDBOX_INFRASTRUCTURE: RunStatus.FAILED_SANDBOX,
    FailureReason.AGENT_ERROR: RunStatus.FAILED_AGENT,
    FailureReason.AGENT_TIMEOUT: RunStatus.TIMED_OUT,
    FailureReason.GRADER_ERROR: RunStatus.FAILED_GRADER,
    FailureReason.GRADER_TIMEOUT: RunStatus.TIMED_OUT,
    FailureReason.ARTIFACT_COLLECTION: RunStatus.INTERNAL_ERROR,
    FailureReason.CANCELLED: RunStatus.CANCELLED,
    FailureReason.INTERNAL: RunStatus.INTERNAL_ERROR,
}


def terminal_status_for(reason: FailureReason) -> RunStatus:
    """Return the terminal run status that represents ``reason``."""
    return _TERMINAL_STATUS_BY_REASON[reason]


class AgentEnvForgeError(Exception):
    """Base class for all expected, classified AgentEnvForge failures.

    Subclasses set :attr:`reason`. ``details`` carries small structured context (limits,
    IDs, exit codes) that is persisted alongside the failure and shown to users.
    """

    reason: ClassVar[FailureReason] = FailureReason.INTERNAL

    def __init__(self, message: str, *, details: Mapping[str, JSONScalar] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: Metadata = dict(details or {})


class ValidationIssue(BaseModel):
    """One problem found while validating a task or agent specification."""

    model_config = ConfigDict(frozen=True)

    field: str
    reason: str
    received: str | None = None

    def render(self) -> str:
        """Human-readable multi-line rendering used by the CLI and exception messages."""
        lines = [f"field: {self.field}", f"reason: {self.reason}"]
        if self.received is not None:
            lines.append(f"received: {self.received}")
        return "\n".join(lines)


class TaskValidationError(AgentEnvForgeError):
    """A task (or agent) specification is malformed. Raised before any expensive work."""

    reason = FailureReason.TASK_VALIDATION

    def __init__(self, source: str, issues: list[ValidationIssue]) -> None:
        self.source = source
        self.issues = tuple(issues)
        rendered = "\n\n".join(issue.render() for issue in self.issues)
        super().__init__(
            f"{source}: {len(self.issues)} validation issue(s)\n\n{rendered}",
            details={"source": source, "issue_count": len(self.issues)},
        )


class ConfigurationError(TaskValidationError):
    """AgentEnvForge's own settings (config file, environment variables) are invalid."""


class SourcePreparationError(AgentEnvForgeError):
    """The task source (local directory or git checkout) could not be prepared."""

    reason = FailureReason.SOURCE_PREPARATION


class SandboxError(AgentEnvForgeError):
    """Generic sandbox backend failure (daemon unreachable, API error, copy failure)."""

    reason = FailureReason.SANDBOX_INFRASTRUCTURE


class SandboxPathNotFoundError(SandboxError):
    """A path requested from a sandbox does not exist."""


class SandboxBuildError(SandboxError):
    """The sandbox image could not be built or pulled."""

    reason = FailureReason.SANDBOX_BUILD


class SandboxBuildTimeoutError(SandboxBuildError):
    """The sandbox image build exceeded its time limit."""

    reason = FailureReason.SANDBOX_BUILD_TIMEOUT


class SandboxStartError(SandboxError):
    """A sandbox container could not be created or started."""

    reason = FailureReason.SANDBOX_START


class SetupCommandError(AgentEnvForgeError):
    """A task setup command failed before the agent started."""

    reason = FailureReason.SETUP_COMMAND


class AgentError(AgentEnvForgeError):
    """The agent adapter itself failed (crashed, misconfigured, could not start)."""

    reason = FailureReason.AGENT_ERROR


class AgentTimeoutError(AgentEnvForgeError):
    """The agent exceeded its total time budget."""

    reason = FailureReason.AGENT_TIMEOUT


class GraderError(AgentEnvForgeError):
    """The grader could not produce a verdict (as opposed to producing a failing one)."""

    reason = FailureReason.GRADER_ERROR


class GraderTimeoutError(GraderError):
    """The grader exceeded its time limit."""

    reason = FailureReason.GRADER_TIMEOUT


class ArtifactError(AgentEnvForgeError):
    """An artifact could not be stored or collected."""

    reason = FailureReason.ARTIFACT_COLLECTION


class RunCancelledError(AgentEnvForgeError):
    """The run was cancelled by the user (Ctrl+C) or a termination signal."""

    reason = FailureReason.CANCELLED


class FailureInfo(BaseModel):
    """Persisted description of why a run failed."""

    model_config = ConfigDict(frozen=True)

    reason: FailureReason
    stage: RunStatus
    message: str
    error_type: str
    details: Metadata = {}

    @classmethod
    def from_exception(cls, error: BaseException, stage: RunStatus) -> FailureInfo:
        """Classify ``error`` raised while the run was in ``stage``."""
        if isinstance(error, AgentEnvForgeError):
            return cls(
                reason=error.reason,
                stage=stage,
                message=error.message,
                error_type=type(error).__name__,
                details=error.details,
            )
        if isinstance(error, KeyboardInterrupt):
            return cls(
                reason=FailureReason.CANCELLED,
                stage=stage,
                message="run interrupted by user",
                error_type="KeyboardInterrupt",
            )
        return cls(
            reason=FailureReason.INTERNAL,
            stage=stage,
            message=f"unexpected {type(error).__name__}: {error}",
            error_type=type(error).__name__,
        )
