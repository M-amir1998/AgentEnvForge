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
from agentenvforge.execution.experiments import ExperimentExecutor
from agentenvforge.execution.orchestrator import OrchestratorSettings, RunOrchestrator
from agentenvforge.persistence.sqlite import SqliteRunRepository
from agentenvforge.persistence.sqlite_database import SqliteDatabase
from agentenvforge.persistence.sqlite_experiments import SqliteExperimentRepository
from agentenvforge.sandbox.base import SandboxBackend


def create_backend(settings: Settings) -> SandboxBackend:
    """Return the configured sandbox backend (Docker is the only one today)."""
    from agentenvforge.sandbox.docker_backend import DockerSandboxBackend  # noqa: PLC0415

    return DockerSandboxBackend(docker_host=settings.docker_host)


@dataclass(slots=True)
class Services:
    """Long-lived services for one CLI invocation."""

    settings: Settings
    database: SqliteDatabase
    repository: SqliteRunRepository
    experiments: SqliteExperimentRepository
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

    def experiment_executor(self) -> ExperimentExecutor:
        """Build the experiment executor: one backend (Docker client) and one orchestrator
        per worker thread, sharing this database and artifact store."""
        return ExperimentExecutor(
            experiments=self.experiments,
            runs=self.repository,
            backend_factory=lambda: create_backend(self.settings),
            orchestrator_factory=self.orchestrator,
        )

    def close(self) -> None:
        """Release the database connection."""
        self.database.close()


def open_services(settings: Settings) -> Services:
    """Open the database and artifact store below ``settings.home``."""
    settings.home.mkdir(parents=True, exist_ok=True)
    database = SqliteDatabase(settings.database_path)
    return Services(
        settings=settings,
        database=database,
        repository=SqliteRunRepository(database),
        experiments=SqliteExperimentRepository(database),
        artifacts=LocalArtifactStore(settings.artifacts_dir),
    )
