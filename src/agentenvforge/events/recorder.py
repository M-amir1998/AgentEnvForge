"""The run recorder: the single write path for a run's observable history.

Everything observable about a run flows through :class:`RunRecorder`:

* :meth:`RunRecorder.emit` assigns the next sequence number, persists the event, then
  fans it out to sinks (log output, CLI progress display);
* :meth:`RunRecorder.save_artifact` redacts secrets, stores the content and records it;
* :meth:`RunRecorder.command_observer` turns sandbox commands into ``command.*`` events.

Persist-then-notify ordering means a sink can never show something that is not stored.
"""

from __future__ import annotations

import json
import logging
import shlex
import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from agentenvforge.artifacts import ArtifactRecord, ArtifactStore
from agentenvforge.domain.clock import utc_now
from agentenvforge.domain.commands import CommandExecution, CommandRequest
from agentenvforge.domain.events import EventType, ExecutionEvent
from agentenvforge.domain.ids import new_event_id
from agentenvforge.domain.status import RunStatus
from agentenvforge.domain.types import JSONScalar
from agentenvforge.persistence.repository import RunRepository
from agentenvforge.sandbox.session import CommandObserver
from agentenvforge.telemetry.redaction import Redactor

logger = logging.getLogger(__name__)


class EventSink(Protocol):
    """Receives every event after it has been persisted."""

    def handle(self, event: ExecutionEvent) -> None:
        """React to ``event``; must be fast and must not raise."""
        ...


class RecorderSealedError(RuntimeError):
    """A write was attempted after the run was finalized (e.g. by an abandoned thread)."""


class RunRecorder:
    """Writes events, artifacts and the command log of one run (thread-safe)."""

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        repository: RunRepository,
        artifact_store: ArtifactStore,
        redactor: Redactor,
        status: Callable[[], RunStatus],
        sinks: Sequence[EventSink] = (),
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self._repository = repository
        self._artifacts = artifact_store
        self._redactor = redactor
        self._status = status
        self._sinks = tuple(sinks)
        self._lock = threading.Lock()
        self._sequence = 0
        self._command_log: list[str] = []
        self._sealed = False

    def seal(self) -> None:
        """Reject every later write: the run's record is complete."""
        with self._lock:
            self._sealed = True

    def redact(self, text: str) -> str:
        """Apply the run's secret redaction to ``text``."""
        return self._redactor.redact_text(text)

    def _ensure_open(self) -> None:
        if self._sealed:
            raise RecorderSealedError(f"run {self.run_id} is finalized; late write rejected")

    def emit(
        self, event_type: EventType, *, duration_ms: int | None = None, **data: JSONScalar
    ) -> ExecutionEvent:
        """Record an event and notify sinks."""
        cleaned = {
            key: self._redactor.redact_text(value) if isinstance(value, str) else value
            for key, value in data.items()
        }
        with self._lock:
            self._ensure_open()
            self._sequence += 1
            event = ExecutionEvent(
                event_id=new_event_id(),
                run_id=self.run_id,
                task_id=self.task_id,
                sequence=self._sequence,
                timestamp=utc_now(),
                event_type=event_type,
                status=self._status(),
                duration_ms=duration_ms,
                data=cleaned,
            )
            self._repository.append_event(event)
        logger.debug("event %s %s", event.event_type.value, json.dumps(event.data, default=str))
        for sink in self._sinks:
            try:
                sink.handle(event)
            except Exception:  # presentation must never break a run
                logger.exception(
                    "event sink %s failed on %s", type(sink).__name__, event.event_type
                )
        return event

    def save_artifact(self, name: str, content: bytes) -> ArtifactRecord:
        """Redact, store and record an artifact."""
        self._ensure_open()
        record = self._artifacts.put(self.run_id, name, self._redactor.redact_bytes(content))
        self._repository.add_artifact(record)
        self.emit(EventType.ARTIFACT_STORED, name=record.name, size_bytes=record.size_bytes)
        return record

    def save_json_artifact(self, name: str, payload: object) -> ArtifactRecord:
        """Store ``payload`` as pretty-printed JSON."""
        return self.save_artifact(name, json.dumps(payload, indent=2, default=str).encode("utf-8"))

    def command_observer(self, role: str) -> CommandObserver:
        """Return an observer that records commands issued by ``role`` (agent/grader/setup)."""
        return _CommandRecorder(self, role)

    def command_log(self) -> bytes:
        """The JSON-lines log of every command executed so far (already redacted)."""
        with self._lock:
            return "".join(line + "\n" for line in self._command_log).encode("utf-8")

    def _append_command(self, entry: dict[str, object]) -> None:
        line = self._redactor.redact_text(json.dumps(entry, default=str))
        with self._lock:
            self._command_log.append(line)


class _CommandRecorder:
    def __init__(self, recorder: RunRecorder, role: str) -> None:
        self._recorder = recorder
        self._role = role

    def command_started(self, request: CommandRequest) -> None:
        self._recorder.emit(
            EventType.COMMAND_STARTED,
            role=self._role,
            command=shlex.join(request.argv),
            cwd=request.cwd,
            timeout_seconds=request.timeout_seconds,
        )

    def command_completed(self, execution: CommandExecution) -> None:
        self._recorder.emit(
            EventType.COMMAND_COMPLETED,
            duration_ms=execution.duration_ms,
            role=self._role,
            command=shlex.join(execution.argv),
            exit_code=execution.exit_code,
            timed_out=execution.timed_out,
            stdout_bytes=len(execution.stdout),
            stderr_bytes=len(execution.stderr),
            output_truncated=execution.stdout_truncated or execution.stderr_truncated,
        )
        self._recorder._append_command({"role": self._role, **execution.model_dump(mode="json")})
