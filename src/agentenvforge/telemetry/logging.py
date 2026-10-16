"""Structured logging.

Machine-readable logs (this module) are kept separate from human-readable CLI rendering
(:mod:`agentenvforge.cli.render`). Every record carries the current ``run_id``,
``task_id`` and ``sandbox`` from :func:`log_context`, so the lines of one run can be
isolated even when many runs share a process.
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

_CONTEXT_FIELDS = ("run_id", "task_id", "sandbox")
_log_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "agentenvforge_log_context",
    default={},  # noqa: B039 - never mutated, only replaced
)
ROOT_LOGGER = "agentenvforge"


@contextmanager
def log_context(**fields: str) -> Iterator[None]:
    """Attach ``fields`` to every log record emitted inside the ``with`` block."""
    token = _log_context.set({**_log_context.get(), **fields})
    try:
        yield
    finally:
        _log_context.reset(token)


def current_log_context() -> dict[str, str]:
    """Return the fields currently attached to log records."""
    return dict(_log_context.get())


class ContextFilter(logging.Filter):
    """Copies the log context onto each record (as attributes)."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Annotate ``record``; never drops records."""
        context = _log_context.get()
        for field in _CONTEXT_FIELDS:
            setattr(record, field, context.get(field, getattr(record, field, None)))
        return True


class RunFilter(logging.Filter):
    """Accepts only records belonging to one run (used for per-run log files)."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep records whose context ``run_id`` matches."""
        return _log_context.get().get("run_id") == self._run_id


class JsonFormatter(logging.Formatter):
    """One JSON object per line with UTC timestamps and context fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise ``record``."""
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _CONTEXT_FIELDS:
            value = _log_context.get().get(field)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Compact human-readable format that still includes the run context."""

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as ``time level logger [run task] message``."""
        context = _log_context.get()
        tags = " ".join(f"{key}={context[key]}" for key in _CONTEXT_FIELDS if key in context)
        timestamp = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        line = f"{timestamp} {record.levelname:<7} {record.name}: {record.getMessage()}"
        if tags:
            line += f" [{tags}]"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str, log_format: str, stream: IO[str]) -> None:
    """Configure the ``agentenvforge`` logger hierarchy (idempotent)."""
    logger = logging.getLogger(ROOT_LOGGER)
    for handler in list(logger.handlers):
        if getattr(handler, "_agentenvforge_console", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    handler._agentenvforge_console = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    handler.setLevel(level)
    logger.propagate = False


@contextmanager
def run_log_file(path: Path, run_id: str) -> Iterator[None]:
    """Write all DEBUG+ records of ``run_id`` as JSON lines to ``path`` while active."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunFilter(run_id))
    logger = logging.getLogger(ROOT_LOGGER)
    logger.addHandler(handler)
    # The logger must pass DEBUG records for the run's file handler to receive them. The
    # level is only ever raised, never restored: with concurrent runs, saving and restoring it
    # would let one run's exit lower the level while another run is still logging. Output
    # stays controlled by each handler's own level (console handlers are set separately).
    if logger.getEffectiveLevel() > logging.DEBUG:
        logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()
