"""Composition root: the only place concrete infrastructure is chosen and connected.

Everything else receives its collaborators through constructor arguments, which is what
lets tests substitute fakes and lets future entry points (an HTTP API, a queue worker)
reuse the same application services with different wiring.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agentenvforge.artifacts import LocalArtifactStore
from agentenvforge.config.settings import Settings
from agentenvforge.events.recorder import EventSink
from agentenvforge.events.sinks import LoggingEventSink
from agentenvforge.execution.orchestrator import OrchestratorSettings, RunOrchestrator
from agentenvforge.persistence.sqlite import SqliteRunRepository
from agentenvforge.sandbox.base import SandboxBackend


def create_backend(settings: Settings) -> SandboxBackend:
    """Return the configured sandbox backend (Docker is the only one today)."""
    from agentenvforge.sandbox.docker_backend import DockerSandboxBackend  # noqa: PLC0415

    return DockerSandboxBackend(docker_host=settings.docker_host)


@dataclass(slots=True)
class Services:
    """Long-lived services for one CLI invocation."""

    settings: Settings
    repository: SqliteRunRepository
    artifacts: LocalArtifactStore

    def orchestrator(
        self, backend: SandboxBackend, sinks: Sequence[EventSink] = ()
    ) -> RunOrchestrator:
        """Build an orchestrator wired to these services."""
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        return RunOrchestrator(
            backend=backend,
            repository=self.repository,
            artifact_store=self.artifacts,
            settings=OrchestratorSettings(
                work_dir=self.settings.work_dir,
                sandbox_start_timeout_seconds=self.settings.sandbox_start_timeout_seconds,
                cleanup_timeout_seconds=self.settings.cleanup_timeout_seconds,
            ),
            event_sinks=(LoggingEventSink(), *sinks),
        )

    def close(self) -> None:
        """Release the database connection."""
        self.repository.close()


def open_services(settings: Settings) -> Services:
    """Open the database and artifact store below ``settings.home``."""
    return Services(
        settings=settings,
        repository=SqliteRunRepository(settings.database_path),
        artifacts=LocalArtifactStore(settings.artifacts_dir),
    )
