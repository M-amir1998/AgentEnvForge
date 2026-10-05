"""Built-in agent adapters: command, manual and noop."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agentenvforge.agents.base import AgentContext
from agentenvforge.domain.clock import elapsed_ms, monotonic_ms
from agentenvforge.domain.failures import AgentTimeoutError
from agentenvforge.domain.results import AgentOutcome, AgentResult

AGENT_FILES_MOUNT = PurePosixPath("/agent")
INSTRUCTIONS_PATH = PurePosixPath("/tmp/agentenv/instructions.md")  # noqa: S108 - in sandbox


@dataclass(frozen=True, slots=True)
class CommandAgent:
    """Runs an external program inside the agent sandbox as the agent.

    The program starts in the workspace with these environment variables:

    ``AGENTENV_RUN_ID``, ``AGENTENV_TASK_ID``, ``AGENTENV_WORKSPACE`` and
    ``AGENTENV_INSTRUCTIONS_FILE`` (the task instructions as Markdown).

    If ``files`` is set, that host directory is copied to ``/agent`` first, which is how
    an agent's own code gets into the sandbox. ``secrets`` are passed only to the agent
    process environment and are redacted from every recorded output.
    """

    label: str
    command: tuple[str, ...]
    files: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    secrets: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """``command:<label>``."""
        return f"command:{self.label}"

    def run(self, context: AgentContext) -> AgentResult:
        """Copy agent files, write instructions, then run the agent command."""
        started = monotonic_ms()
        session = context.session
        if self.files is not None:
            session.upload_directory(self.files, AGENT_FILES_MOUNT)
        session.write_file(INSTRUCTIONS_PATH, context.task.instructions.encode("utf-8"))

        environment = {
            **self.environment,
            "AGENTENV_RUN_ID": context.run_id,
            "AGENTENV_TASK_ID": context.task.task_id,
            "AGENTENV_WORKSPACE": str(context.task.workspace),
            "AGENTENV_INSTRUCTIONS_FILE": str(INSTRUCTIONS_PATH),
            **self.secrets,
        }
        execution = session.run(
            self.command,
            cwd=context.task.workspace,
            env=environment,
            timeout_seconds=session.remaining_seconds() or context.time_budget_seconds,
        )
        context.save_artifact("agent/stdout.log", execution.stdout.encode("utf-8"))
        context.save_artifact("agent/stderr.log", execution.stderr.encode("utf-8"))
        if execution.timed_out:
            raise AgentTimeoutError(
                f"agent command exceeded the {context.time_budget_seconds:g}s time budget",
                details={"limit_seconds": context.time_budget_seconds},
            )
        succeeded = execution.exit_code == 0
        return AgentResult(
            adapter=self.name,
            outcome=AgentOutcome.COMPLETED if succeeded else AgentOutcome.FAILED,
            exit_code=execution.exit_code,
            summary="agent command exited successfully"
            if succeeded
            else f"agent command exited with status {execution.exit_code}",
            duration_ms=elapsed_ms(started),
            command_count=session.command_count,
        )


@dataclass(frozen=True, slots=True)
class ManualAgent:
    """Lets a human act as the agent: prints how to enter the sandbox, waits for Enter.

    Useful for authoring and debugging tasks: explore the environment by hand, apply a
    fix, then let the grader judge it exactly as it would judge an AI agent.
    """

    write: Callable[[str], None]
    wait_for_confirmation: Callable[[str], str]

    @property
    def name(self) -> str:
        """``manual``."""
        return "manual"

    def run(self, context: AgentContext) -> AgentResult:
        """Show instructions and the attach command, then block until confirmed."""
        started = monotonic_ms()
        hint = context.session.attach_hint() or "(this sandbox backend has no interactive shell)"
        self.write(
            f"Task {context.task.task_id}: {context.task.name}\n\n{context.task.instructions}\n\n"
            f"Workspace: {context.task.workspace}\nOpen a shell with:\n  {hint}\n"
            f"Time budget: {context.time_budget_seconds:g}s\n"
        )
        self.wait_for_confirmation("Press Enter when you are done to start grading... ")
        return AgentResult(
            adapter=self.name,
            outcome=AgentOutcome.COMPLETED,
            summary="human operator finished",
            duration_ms=elapsed_ms(started),
        )


@dataclass(frozen=True, slots=True)
class NoopAgent:
    """Does nothing. Grading the untouched workspace is a sanity check for tasks:
    if the noop baseline passes, the task's grader cannot discriminate solutions."""

    @property
    def name(self) -> str:
        """``noop``."""
        return "noop"

    def run(self, context: AgentContext) -> AgentResult:
        """Return immediately without touching the workspace."""
        return AgentResult(adapter=self.name, outcome=AgentOutcome.COMPLETED, summary="no-op")
