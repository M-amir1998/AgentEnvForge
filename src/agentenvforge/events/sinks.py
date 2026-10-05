"""Event sinks that ship with AgentEnvForge."""

from __future__ import annotations

import logging

from agentenvforge.domain.events import ExecutionEvent

logger = logging.getLogger("agentenvforge.events")


class LoggingEventSink:
    """Mirrors events into the structured log at INFO level."""

    def handle(self, event: ExecutionEvent) -> None:
        """Log ``event`` with its metadata."""
        details = " ".join(f"{key}={value}" for key, value in event.data.items())
        logger.info("%s %s", event.event_type.value, details)
