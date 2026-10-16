"""Executing an experiment: the application service behind ``agentenv experiment run``.

The executor owns the experiment *lifecycle* and nothing else::

    CREATED     persist the experiment, its manifest and every planned run
    PREPARING   capture the environment, preflight each agent, prepare each task's image
    RUNNING     BoundedScheduler -> one RunOrchestrator.execute() per planned run
    CANCELLING  (on Ctrl+C / SIGTERM / circuit breaker) no new runs; active runs cancel
    FINALIZING  aggregate the persisted results into a summary
    COMPLETED | CANCELLED | FAILED

Every run is executed by the existing single-run orchestrator, unchanged: batch execution
adds scheduling and bookkeeping around runs, never a second way of executing one (ADR-0008).
Each worker thread gets its own orchestrator and sandbox backend (and so its own Docker
client); the databases and artifact store are shared and thread-safe.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from agentenvforge._version import __version__
from agentenvforge.agents.base import AgentAdapter, SupportsPreflight
from agentenvforge.agents.spec import adapter_secrets
from agentenvforge.analysis.models import ExperimentSummary
from agentenvforge.analysis.outcomes import Outcome, classify
from agentenvforge.cancellation import CancellationToken
from agentenvforge.domain.clock import utc_now
from agentenvforge.domain.failures import AgentEnvForgeError
from agentenvforge.domain.ids import new_id, new_run_id
from agentenvforge.domain.manifest import ExperimentLink
from agentenvforge.domain.results import RunResult
from agentenvforge.environment import capture_host_environment
from agentenvforge.execution.experiment_results import summarize_experiment
from agentenvforge.execution.images import task_image
from agentenvforge.execution.orchestrator import RunOrchestrator, RunRequest
from agentenvforge.execution.scheduler import BoundedScheduler, Completed
from agentenvforge.experiments.loader import LoadedExperiment
from agentenvforge.experiments.manifest import AgentReference, ExperimentManifest, TaskReference
from agentenvforge.experiments.matrix import RunCombination, expand_matrix
from agentenvforge.experiments.status import ExperimentStateMachine, ExperimentStatus
from agentenvforge.persistence.experiment_repository import ExperimentRepository, NewExperiment
from agentenvforge.persistence.repository import RunRepository
from agentenvforge.processes import Owner
from agentenvforge.sandbox.base import SandboxBackend

logger = logging.getLogger(__name__)

CIRCUIT_BREAKER_THRESHOLD = 5
"""Consecutive runs failing for reasons outside the agents' control (infrastructure, model
server unavailable, lost runs) after which scheduling stops: when Docker or the model server
is down, every remaining run would fail the same way, and the experiment should say so
instead of burning through the whole plan."""

_UNHEALTHY = frozenset(
    {Outcome.INFRASTRUCTURE_ERROR, Outcome.AGENT_UNAVAILABLE, Outcome.INCOMPLETE}
)


@dataclass(frozen=True, slots=True)
class Progress:
    """Counts shown while an experiment runs."""

    planned: int
    started: int
    finished: int
    passed: int
    failed: int
    """Scored runs that did not pass."""
    excluded: int
    """Runs that do not count (infrastructure errors, cancellations, lost runs)."""
    elapsed_seconds: float

    @property
    def active(self) -> int:
        """Runs currently executing."""
        return self.started - self.finished


class ExperimentObserver(Protocol):
    """Receives progress notifications, always on the calling (main) thread."""

    def preparing(self, message: str) -> None:
        """A preparation step started."""
        ...

    def run_finished(
        self,
        combination: RunCombination,
        run_id: str | None,
        outcome: Outcome,
        result: RunResult | None,
        progress: Progress,
    ) -> None:
        """A planned run reached its end."""
        ...

    def cancelling(self, reason: str) -> None:
        """Cancellation was requested; active runs are being stopped."""
        ...


@dataclass(frozen=True, slots=True)
class ExperimentOutcome:
    """What :meth:`ExperimentExecutor.execute` returns."""

    experiment_id: str
    status: ExperimentStatus
    summary: ExperimentSummary
    failure: str | None
    """Why the experiment FAILED (or the circuit breaker stopped it), if it did."""


@dataclass
class _Execution:
    """Mutable state of one experiment execution (private to the executor)."""

    experiment_id: str
    loaded: LoadedExperiment
    workers: int
    machine: ExperimentStateMachine
    manifest: ExperimentManifest
    combinations: list[RunCombination]
    token: CancellationToken
    probes: dict[str, AgentAdapter]
    started_monotonic: float = field(default_factory=time.monotonic)
    started: int = 0
    finished: int = 0
    passed: int = 0
    failed: int = 0
    excluded: int = 0
    consecutive_unhealthy: int = 0
    failure: str | None = None
    run_ids: dict[int, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def progress(self) -> Progress:
        return Progress(
            planned=len(self.combinations),
            started=self.started,
            finished=self.finished,
            passed=self.passed,
            failed=self.failed,
            excluded=self.excluded,
            elapsed_seconds=time.monotonic() - self.started_monotonic,
        )


class ExperimentExecutor:
    """Runs experiments with bounded local concurrency on top of :class:`RunOrchestrator`."""

    def __init__(
        self,
        *,
        experiments: ExperimentRepository,
        runs: RunRepository,
        backend_factory: Callable[[], SandboxBackend],
        orchestrator_factory: Callable[[SandboxBackend], RunOrchestrator],
    ) -> None:
        self._experiments = experiments
        self._runs = runs
        self._backend_factory = backend_factory
        self._orchestrator_factory = orchestrator_factory
        self._local = threading.local()

    def execute(
        self,
        loaded: LoadedExperiment,
        *,
        workers: int,
        cancellation: CancellationToken,
        observer: ExperimentObserver,
        output_dir: str | None = None,
    ) -> ExperimentOutcome:
        """Execute every planned run of ``loaded`` and return the aggregated outcome."""
        state = self._create(loaded, workers, cancellation, output_dir)
        try:
            self._enter(state, ExperimentStatus.PREPARING)
            self._prepare(state, observer)
            if state.failure is None and not cancellation.cancelled:
                self._enter(state, ExperimentStatus.RUNNING)
                self._run_matrix(state, observer)
        # The experiment-level boundary: whatever goes wrong, active runs are cancelled (the
        # scheduler guarantees that) and the experiment still reaches a terminal state with
        # the results that exist.
        except (Exception, KeyboardInterrupt) as error:
            cancellation.cancel(f"internal error: {type(error).__name__}")
            state.failure = f"{type(error).__name__}: {error}"
            logger.exception("experiment %s failed", state.experiment_id)
        return self._finalize(state)

    # ------------------------------------------------------------------- lifecycle

    def _create(
        self,
        loaded: LoadedExperiment,
        workers: int,
        cancellation: CancellationToken,
        output_dir: str | None,
    ) -> _Execution:
        spec = loaded.spec
        experiment_id = new_id("exp")
        combinations = list(
            expand_matrix(
                loaded.task_ids,
                loaded.agent_ids,
                spec.repetitions,
                base_seed=spec.execution.base_seed,
            )
        )
        probes = {agent.agent_id: agent.build() for agent in loaded.agents}
        manifest = ExperimentManifest(
            agentenvforge_version=__version__,
            experiment_schema_version=spec.schema_version,
            spec_id=spec.id,
            name=spec.name,
            description=spec.description,
            definition_hash=loaded.definition_hash,
            definition=spec.model_dump(mode="json"),
            tasks=tuple(
                TaskReference(
                    task_id=task.spec.id,
                    path=relative,
                    schema_version=task.spec.schema_version,
                    definition_hash=task.definition_hash,
                )
                for task, relative in zip(loaded.tasks, spec.tasks, strict=True)
            ),
            agents=tuple(
                AgentReference(
                    agent_id=agent.agent_id,
                    role=agent.role,
                    adapter=probes[agent.agent_id].name,
                    source=agent.declared_as,
                    spec=agent.canonical_spec,
                    spec_hash=agent.spec_hash,
                    code_hash=agent.code_hash,
                    agent_timeout_seconds=agent.timeout_override,
                )
                for agent in loaded.agents
            ),
            repetitions=spec.repetitions,
            workers=workers,
            base_seed=spec.execution.base_seed,
            planned_runs=len(combinations),
        )
        machine = ExperimentStateMachine()
        self._experiments.create_experiment(
            NewExperiment(
                experiment_id=experiment_id,
                spec_id=spec.id,
                name=spec.name,
                created_at=machine.history[0].entered_at,
                workers=workers,
                definition_hash=loaded.definition_hash,
                owner=Owner.current().token(),
                manifest=manifest,
                output_dir=output_dir,
                combinations=combinations,
            )
        )
        return _Execution(
            experiment_id=experiment_id,
            loaded=loaded,
            workers=workers,
            machine=machine,
            manifest=manifest,
            combinations=combinations,
            token=cancellation,
            probes=probes,
        )

    def _enter(self, state: _Execution, status: ExperimentStatus) -> None:
        state.machine.transition(status)
        self._experiments.record_status(state.experiment_id, state.machine.history)

    def _prepare(self, state: _Execution, observer: ExperimentObserver) -> None:
        """Check everything that would make *every* run fail, before any run starts."""
        observer.preparing("checking the sandbox backend")
        try:
            backend = self._backend_factory()
            environment = capture_host_environment(backend.describe())
        except AgentEnvForgeError as error:
            state.failure = f"sandbox backend unavailable: {error.message}"
            return

        agents = []
        for reference in state.manifest.agents:
            adapter = state.probes[reference.agent_id]
            provenance = reference.provenance
            if isinstance(adapter, SupportsPreflight):
                observer.preparing(f"checking agent {reference.agent_id}")
                try:
                    provenance = adapter.preflight()
                except AgentEnvForgeError as error:
                    state.failure = f"agent {reference.agent_id} is not ready: {error.message}"
                    return
            agents.append(reference.model_copy(update={"provenance": provenance}))

        tasks: list[TaskReference] = []
        for task_reference, task in zip(state.manifest.tasks, state.loaded.tasks, strict=True):
            if state.token.cancelled:
                return
            observer.preparing(f"preparing the image of {task.spec.id}")
            started = time.monotonic()
            try:
                image = backend.prepare_image(task_image(task).spec)
            except AgentEnvForgeError as error:
                state.failure = (
                    f"image of task {task.spec.id} could not be prepared: {error.message}"
                )
                return
            tasks.append(
                task_reference.model_copy(
                    update={
                        "image_reference": image.reference,
                        "image_id": image.image_id,
                        "image_cached": image.cached,
                        "image_prepare_seconds": round(time.monotonic() - started, 3),
                    }
                )
            )
        state.manifest = state.manifest.model_copy(
            update={"environment": environment, "agents": tuple(agents), "tasks": tuple(tasks)}
        )
        self._experiments.update_manifest(state.experiment_id, state.manifest)

    def _run_matrix(self, state: _Execution, observer: ExperimentObserver) -> None:
        state.started_monotonic = time.monotonic()

        def mark_started(_: RunCombination) -> None:
            with state.lock:
                state.started += 1

        BoundedScheduler(state.workers).run(
            state.combinations,
            lambda combination: self._execute_run(state, combination),
            cancellation=state.token,
            on_start=mark_started,
            on_complete=lambda done: self._record(state, done, observer),
            on_tick=lambda: self._observe_cancellation(state, observer),
        )

    def _execute_run(self, state: _Execution, combination: RunCombination) -> RunResult:
        """Runs in a worker thread: one planned run through the single-run orchestrator."""
        orchestrator = getattr(self._local, "orchestrator", None)
        if orchestrator is None:
            orchestrator = self._orchestrator_factory(self._backend_factory())
            self._local.orchestrator = orchestrator
        resolved = state.loaded.agent(combination.agent_id)
        run_id = new_run_id()
        self._experiments.link_run(state.experiment_id, combination.position, run_id)
        state.run_ids[combination.position] = run_id
        adapter = resolved.build()  # fresh per run: no state leaks between repetitions
        return orchestrator.execute(
            RunRequest(
                task=state.loaded.task(combination.task_id),
                agent=adapter,
                secrets=adapter_secrets(adapter),
                agent_timeout_seconds=resolved.timeout_override,
                run_id=run_id,
                seed=combination.seed,
                cancellation=state.token,
                experiment=ExperimentLink(
                    experiment_id=state.experiment_id,
                    position=combination.position,
                    agent_id=combination.agent_id,
                    repetition=combination.repetition,
                ),
                agent_spec=resolved.canonical_spec,
            )
        )

    def _record(
        self,
        state: _Execution,
        done: Completed[RunCombination, RunResult],
        observer: ExperimentObserver,
    ) -> None:
        """Main thread: account for a finished planned run."""
        combination = done.item
        run_id = state.run_ids.get(combination.position)
        if done.error is not None:
            logger.error(
                "planned run %s (%s x %s) raised %s: %s",
                combination.position,
                combination.task_id,
                combination.agent_id,
                type(done.error).__name__,
                done.error,
                exc_info=done.error,
            )
        outcome = classify(done.result, started=run_id is not None)
        state.finished += 1
        if outcome is Outcome.PASSED:
            state.passed += 1
        elif outcome.is_scored:
            state.failed += 1
        else:
            state.excluded += 1
        if outcome in _UNHEALTHY:
            state.consecutive_unhealthy += 1
        elif outcome.is_scored:
            state.consecutive_unhealthy = 0
        if state.consecutive_unhealthy >= CIRCUIT_BREAKER_THRESHOLD and state.failure is None:
            state.failure = (
                f"stopped after {CIRCUIT_BREAKER_THRESHOLD} consecutive runs failed for reasons "
                "outside the agents' control (see the failure breakdown)"
            )
            state.token.cancel("circuit breaker")
        observer.run_finished(combination, run_id, outcome, done.result, state.progress())

    def _observe_cancellation(self, state: _Execution, observer: ExperimentObserver) -> None:
        if state.token.cancelled and state.machine.current is ExperimentStatus.RUNNING:
            self._enter(state, ExperimentStatus.CANCELLING)
            observer.cancelling(state.token.reason)

    def _finalize(self, state: _Execution) -> ExperimentOutcome:
        if state.token.cancelled and state.machine.current is ExperimentStatus.RUNNING:
            self._enter(state, ExperimentStatus.CANCELLING)
        self._enter(state, ExperimentStatus.FINALIZING)
        if state.failure is not None:
            terminal = ExperimentStatus.FAILED
        elif state.token.cancelled:
            terminal = ExperimentStatus.CANCELLED
        else:
            terminal = ExperimentStatus.COMPLETED
        stored = self._experiments.get_experiment(state.experiment_id)
        if stored is None:  # pragma: no cover - the experiment row was created above
            raise RuntimeError(f"experiment {state.experiment_id} disappeared")
        summary = summarize_experiment(
            stored, self._runs, generated_at=utc_now(), status=terminal.value
        )
        if state.failure is not None:
            summary = summary.model_copy(
                update={"warnings": (f"experiment failed: {state.failure}", *summary.warnings),
                        "results_valid": False}
            )  # fmt: skip
        self._experiments.store_summary(state.experiment_id, summary, stored.record.output_dir)
        self._enter(state, terminal)
        return ExperimentOutcome(
            experiment_id=state.experiment_id,
            status=terminal,
            summary=summary,
            failure=state.failure,
        )
