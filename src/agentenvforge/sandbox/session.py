"""The sandbox facade given to agents and graders.

Agents and graders never receive the raw :class:`~.base.Sandbox`. They receive a
:class:`SandboxSession`, which adds the policies the orchestrator needs to enforce
uniformly, whatever the adapter does:

* a **deadline** - every command's timeout is clipped to the remaining time budget, and
  no command may start after the budget is spent;
* **observation** - every command is reported to a :class:`CommandObserver`, which is how
  the orchestrator emits ``command.*`` events and the commands log;
* **workspace-relative paths** - relative paths resolve against the task workspace;
* **revocation** - :meth:`close` makes all further calls fail, so an abandoned (timed
  out) adapter thread cannot keep acting on a sandbox.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Protocol

from agentenvforge.cancellation import CancellationToken
from agentenvforge.domain.commands import (
    DEFAULT_OUTPUT_LIMIT_BYTES,
    CommandExecution,
    CommandRequest,
)
from agentenvforge.domain.failures import AgentEnvForgeError, AgentTimeoutError, SandboxError
from agentenvforge.sandbox import transfer
from agentenvforge.sandbox.base import Sandbox
from agentenvforge.workspace.archive import Ownership


class CommandObserver(Protocol):
    """Receives notifications about commands executed through a session."""

    def command_started(self, request: CommandRequest) -> None:
        """Called before the command is sent to the sandbox."""
        ...

    def command_completed(self, execution: CommandExecution) -> None:
        """Called after the command finished (including timeouts)."""
        ...


class SessionClosedError(SandboxError):
    """The session was revoked (its run timed out, was cancelled, or finished)."""


class SandboxSession:
    """Deadline-aware, observed access to one sandbox."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        workspace: PurePosixPath,
        owner: Ownership,
        default_command_timeout_seconds: float,
        deadline: float | None = None,
        observer: CommandObserver | None = None,
        timeout_error: type[AgentEnvForgeError] = AgentTimeoutError,
        cancellation: CancellationToken | None = None,
    ) -> None:
        """Create a session.

        ``deadline`` is a :func:`time.monotonic` value after which no command may run;
        ``timeout_error`` is raised when a caller tries to run a command past it.
        Once ``cancellation`` is requested, no further command may start.
        """
        self._sandbox = sandbox
        self.workspace = workspace
        self._owner = owner
        self._default_timeout = default_command_timeout_seconds
        self._deadline = deadline
        self._observer = observer
        self._timeout_error = timeout_error
        self._cancellation = cancellation
        self._closed = threading.Event()
        self._command_count = 0

    @property
    def sandbox_name(self) -> str:
        """Name of the underlying sandbox (for logs and messages)."""
        return self._sandbox.name

    @property
    def command_count(self) -> int:
        """Number of commands executed so far."""
        return self._command_count

    def remaining_seconds(self) -> float | None:
        """Seconds left before the deadline, or ``None`` without a deadline.

        A closed session has no time left, so cooperative adapters stop promptly.
        """
        if self._closed.is_set():
            return 0.0
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    def resolve(self, path: str | PurePosixPath) -> PurePosixPath:
        """Resolve ``path`` against the workspace unless it is absolute."""
        candidate = PurePosixPath(path)
        return candidate if candidate.is_absolute() else self.workspace / candidate

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | PurePosixPath | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    ) -> CommandExecution:
        """Execute ``argv`` (an argument vector; no shell is involved unless you add one)."""
        self._ensure_open()
        if self._cancellation is not None:
            self._cancellation.raise_if_cancelled()
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout
        remaining = self.remaining_seconds()
        if remaining is not None:
            if remaining <= 0:
                raise self._timeout_error(
                    "time budget exhausted; no further commands may run",
                    details={"sandbox": self.sandbox_name},
                )
            timeout = min(timeout, remaining)
        request = CommandRequest(
            argv=tuple(argv),
            cwd=str(self.resolve(cwd)) if cwd is not None else str(self.workspace),
            env=dict(env or {}),
            timeout_seconds=max(timeout, 0.001),
            output_limit_bytes=output_limit_bytes,
        )
        if self._observer is not None:
            self._observer.command_started(request)
        execution = self._sandbox.execute(request)
        self._command_count += 1
        if self._observer is not None:
            self._observer.command_completed(execution)
        return execution

    def read_file(self, path: str | PurePosixPath, *, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        """Return a file's content from the sandbox."""
        self._ensure_open()
        return transfer.read_file(self._sandbox, self.resolve(path), max_bytes=max_bytes)

    def write_file(self, path: str | PurePosixPath, content: bytes, *, mode: int = 0o644) -> None:
        """Create or replace a file in the sandbox; its parent directory must exist."""
        self._ensure_open()
        target = self.resolve(path)
        transfer.upload_files(
            self._sandbox, {target.name: content}, target.parent, owner=self._owner, mode=mode
        )

    def upload_directory(self, source: Path, destination: str | PurePosixPath) -> None:
        """Copy a host directory into the sandbox as ``destination``."""
        self._ensure_open()
        transfer.upload_directory(
            self._sandbox, source, self.resolve(destination), owner=self._owner
        )

    def download_directory(self, source: str | PurePosixPath, destination: Path) -> tuple[str, ...]:
        """Copy a sandbox directory to the host; returns skipped unsafe entry names."""
        self._ensure_open()
        return transfer.download_directory(self._sandbox, self.resolve(source), destination)

    def attach_hint(self) -> str | None:
        """How a human can open a shell in the sandbox, if the backend supports it."""
        return self._sandbox.attach_hint()

    def close(self) -> None:
        """Revoke the session; subsequent calls raise :class:`SessionClosedError`."""
        self._closed.set()

    def _ensure_open(self) -> None:
        if self._closed.is_set():
            raise SessionClosedError(f"session for sandbox {self.sandbox_name} is closed")
