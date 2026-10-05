"""Command execution records.

Commands are always argument vectors (``argv``), never shell strings. When a task really
needs shell features it must say so explicitly with ``["sh", "-c", "..."]``, which makes
the shell interpretation visible in the task file and in every recorded event.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_OUTPUT_LIMIT_BYTES = 1024 * 1024
"""Maximum bytes of stdout/stderr kept per stream. Output beyond this is discarded so a
runaway process cannot exhaust host memory; truncation is recorded explicitly."""


class CommandRequest(BaseModel):
    """A command to run inside a sandbox."""

    model_config = ConfigDict(frozen=True)

    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = {}
    timeout_seconds: float = Field(gt=0)
    output_limit_bytes: int = Field(default=DEFAULT_OUTPUT_LIMIT_BYTES, gt=0)


class CommandExecution(BaseModel):
    """The observed outcome of one :class:`CommandRequest`."""

    model_config = ConfigDict(frozen=True)

    argv: tuple[str, ...]
    cwd: str | None
    exit_code: int | None
    """``None`` when the process never reported an exit status (e.g. sandbox killed)."""
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    started_at: datetime
    duration_ms: int
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        """True when the command exited with status 0 within its time limit."""
        return self.exit_code == 0 and not self.timed_out
