"""The run orchestrator: the application service that executes one run end to end.

``RunOrchestrator.execute`` drives the lifecycle defined in
:mod:`agentenvforge.domain.status`. Each active state has exactly one ``_stage_*``
method; the loop in :meth:`RunOrchestrator.execute` is the only place that sequences
them, classifies failures and guarantees cleanup::

    for each stage:            enter state, run stage method
    on any exception:          classify into FailureInfo (root cause preserved)
    always (finally):          CLEANING_UP -> destroy sandboxes, remove work dirs
    then:                      terminal state, persist RunResult, final artifacts

The orchestrator depends only on protocols (``SandboxBackend``, ``AgentAdapter``,
``RunRepository``, ``ArtifactStore``, ``EventSink``); concrete infrastructure is injected
by the caller (the CLI today, an API or worker process later).
"""

from __future__ import annotations

import functools
import itertools
import logging
import platform
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agentenvforge._version import __version__
from agentenvforge.agents.base import AgentAdapter, AgentContext, AgentTask, ArtifactWriter
from agentenvforge.artifacts import ArtifactStore
from agentenvforge.cancellation import CancellationToken
from agentenvforge.domain.clock import elapsed_ms, monotonic_ms
from agentenvforge.domain.events import EventType
from agentenvforge.domain.failures import (
    AgentEnvForgeError,
    AgentError,
    AgentTimeoutError,
    FailureInfo,
    FailureReason,
    GraderError,
    GraderTimeoutError,
    RunCancelledError,
    SandboxError,
    SandboxPathNotFoundError,
    SandboxStartError,
    SetupCommandError,
    TaskValidationError,
    ValidationIssue,
    terminal_status_for,
)
from agentenvforge.domain.ids import new_run_id
from agentenvforge.domain.manifest import (
    ExperimentLink,
    ImageProvenance,
    RunManifest,
    SourceProvenance,
)
from agentenvforge.domain.results import (
    AgentResult,
    CleanupReport,
    GradeResult,
    RunResult,
    StageTiming,
)
from agentenvforge.domain.status import RunStateMachine, RunStatus
from agentenvforge.events.recorder import EventSink, RunRecorder
from agentenvforge.execution.images import task_image
from agentenvforge.execution.solution import build_grading_tree, collect_included_files
from agentenvforge.grading.base import GradingContext
from agentenvforge.grading.graders import build_grader
from agentenvforge.persistence.repository import NewRun, RunRepository
from agentenvforge.sandbox.base import (
    PreparedImage,
    Sandbox,
    SandboxBackend,
    SandboxConfig,
    SandboxRole,
)
from agentenvforge.sandbox.session import SandboxSession, SessionClosedError
from agentenvforge.sources import PreparedSource, prepare_source, remove_tree
from agentenvforge.tasks.loader import LoadedTask, load_task
from agentenvforge.telemetry.logging import log_context, run_log_file
from agentenvforge.telemetry.redaction import Redactor
from agentenvforge.timeouts import CallTimeoutError, call_with_timeout
from agentenvforge.workspace.archive import Ownership
from agentenvforge.workspace.diff import diff_trees
from agentenvforge.workspace.fingerprint import hash_directory

logger = logging.getLogger(__name__)

_RUN_ID_PATTERN = re.compile(r"^run_[0-9a-z]{26}$")


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Input to :meth:`RunOrchestrator.execute`."""

    task: LoadedTask
    agent: AgentAdapter
    secrets: tuple[str, ...] = ()
    """Secret values to redact from everything recorded about the run."""
    keep_workspace: bool = False
    agent_timeout_seconds: int | None = None
    """Replaces the task's ``timeouts.agent_seconds`` (e.g. for slow local models).
    Recorded in the manifest's ``overrides``, because it changes the evaluation conditions."""
    run_id: str | None = None
    """Pre-assigned run ID (an experiment records it before the run starts); generated if unset."""
    seed: int | None = None
    """Offered to the agent through ``AgentContext.seed`` and recorded in the manifest."""
    cancellation: CancellationToken | None = None
    """Lets another thread cancel the run; it then ends ``CANCELLED`` after normal cleanup."""
    experiment: ExperimentLink | None = None
    """The run's place in an experiment, recorded in its manifest."""
    agent_spec: dict[str, object] | None = None
    """The validated agent specification (recorded so runs identify their agent configuration,
    not only the adapter's name)."""


@dataclass(frozen=True, slots=True)
class OrchestratorSettings:
    """Infrastructure-level limits that are not part of the task definition."""

    work_dir: Path
    sandbox_start_timeout_seconds: float = 120.0
    cleanup_timeout_seconds: float = 60.0
    adapter_grace_seconds: float = 15.0
    """Extra time an in-process adapter (agent or grader) gets beyond its budget to return
    after its last command was cut off by the session deadline, before it is abandoned."""


@dataclass(slots=True)
class _ActiveRun:
    """Mutable state of one in-flight run. Private to the orchestrator."""

    run_id: str
    request: RunRequest
    machine: RunStateMachine
    recorder: RunRecorder
    work_dir: Path
    manifest: RunManifest
    owner: Ownership
    workspace: PurePosixPath
    source: PreparedSource | None = None
    baseline: Path | None = None
    final: Path | None = None
    image: PreparedImage | None = None
    agent_sandbox: Sandbox | None = None
    sandboxes: list[Sandbox] = field(default_factory=list)
    destroyed: set[str] = field(default_factory=set)
    cleanup_errors: list[str] = field(default_factory=list)
    agent_result: AgentResult | None = None
    grade: GradeResult | None = None
    failure: FailureInfo | None = None

    @property
    def task(self) -> LoadedTask:
        return self.request.task


class RunOrchestrator:
    """Executes runs. One instance can execute many runs, sequentially or concurrently."""

    def __init__(
        self,
        *,
        backend: SandboxBackend,
        repository: RunRepository,
        artifact_store: ArtifactStore,
        settings: OrchestratorSettings,
        event_sinks: Sequence[EventSink] = (),
        on_run_created: Callable[[str], None] | None = None,
    ) -> None:
        self._backend = backend
        self._repository = repository
        self._artifacts = artifact_store
        self._settings = settings
        self._sinks = tuple(event_sinks)
        self._on_run_created = on_run_created

    # ------------------------------------------------------------- lifecycle

    def execute(self, request: RunRequest) -> RunResult:
        """Execute one run to a terminal state and return its result.

        Never raises for run failures (they are reported in the result). Cancellation via
        ``KeyboardInterrupt`` is converted into a ``CANCELLED`` result after cleanup.
        """
        run_id = request.run_id or new_run_id()
        if not _RUN_ID_PATTERN.match(run_id):
            raise ValueError(f"invalid run id {run_id!r}")
        task = request.task
        with log_context(run_id=run_id, task_id=task.spec.id):
            log_path = self._settings.work_dir / f"{run_id}.log"
            with run_log_file(log_path, run_id):
                run = self._create_run(run_id, request)
                self._drive(run)
                result = self._finalize(run)
            self._store_log(run, log_path)
            # From here on the run is immutable: an abandoned adapter thread that is still
            # alive gets an error instead of silently adding artifacts to a finished run.
            run.recorder.seal()
        return result

    def _create_run(self, run_id: str, request: RunRequest) -> _ActiveRun:
        task = request.task
        machine = RunStateMachine()
        recorder = RunRecorder(
            run_id=run_id,
            task_id=task.spec.id,
            repository=self._repository,
            artifact_store=self._artifacts,
            redactor=Redactor(request.secrets),
            status=lambda: machine.current,
            sinks=self._sinks,
        )
        manifest = RunManifest(
            agentenvforge_version=__version__,
            python_version=platform.python_version(),
            platform=f"{sys.platform}-{platform.machine()}",
            task_id=task.spec.id,
            task_path=str(task.path),
            task_schema_version=task.spec.schema_version,
            task_definition_hash=task.definition_hash,
            task_definition=task.spec.model_dump(mode="json"),
            agent=request.agent.name,
            overrides=(
                {"timeouts.agent_seconds": request.agent_timeout_seconds}
                if request.agent_timeout_seconds is not None
                else {}
            ),
            seed=request.seed,
            experiment=request.experiment,
            agent_spec=request.agent_spec,
        )
        self._repository.create_run(
            NewRun(
                run_id=run_id,
                task_id=task.spec.id,
                task_path=str(task.path),
                agent=request.agent.name,
                created_at=machine.history[0].entered_at,
                manifest=manifest,
            )
        )
        run = _ActiveRun(
            run_id=run_id,
            request=request,
            machine=machine,
            recorder=recorder,
            work_dir=self._settings.work_dir / run_id,
            manifest=manifest,
            owner=Ownership.parse(task.spec.environment.user),
            workspace=PurePosixPath(task.spec.environment.workspace),
        )
        if self._on_run_created is not None:
            self._on_run_created(run_id)
        recorder.emit(EventType.RUN_CREATED, task_path=str(task.path), agent=request.agent.name)
        return run

    def _drive(self, run: _ActiveRun) -> None:
        stages: tuple[tuple[RunStatus, Callable[[_ActiveRun], None]], ...] = (
            (RunStatus.VALIDATING, self._stage_validate),
            (RunStatus.PREPARING_SOURCE, self._stage_prepare_source),
            (RunStatus.BUILDING_SANDBOX, self._stage_build_image),
            (RunStatus.STARTING_SANDBOX, self._stage_start_agent_sandbox),
            (RunStatus.RUNNING_AGENT, self._stage_run_agent),
            (RunStatus.CAPTURING_SOLUTION, self._stage_capture_solution),
            (RunStatus.RUNNING_GRADER, self._stage_run_grader),
            (RunStatus.COLLECTING_ARTIFACTS, self._stage_collect_artifacts),
        )
        cancellation = run.request.cancellation
        try:
            for status, stage in stages:
                # Once grading has produced a verdict the run is not interrupted any more:
                # cancelling would only throw away a valid result.
                if cancellation is not None and status is not RunStatus.COLLECTING_ARTIFACTS:
                    cancellation.raise_if_cancelled()
                self._enter(run, status)
                stage(run)
        # This is the one deliberate catch-all boundary: every failure, including
        # unexpected bugs and Ctrl+C, must become a classified, persisted run outcome,
        # and cleanup must still run. The traceback is logged, never discarded.
        except (Exception, KeyboardInterrupt) as error:
            run.failure = _classify_failure(run, error)
            logger.error(
                "run failed in %s: %s",
                run.machine.current.value,
                run.failure.message,
                exc_info=not isinstance(error, (AgentEnvForgeError, KeyboardInterrupt)),
            )
        finally:
            # Cleanup must happen even if recording the CLEANING_UP transition fails (e.g. the
            # database is unavailable), or the run's containers would outlive it.
            try:
                self._enter(run, RunStatus.CLEANING_UP)
            except Exception:  # logged; cleanup below must still run
                logger.exception("could not record the cleanup transition of %s", run.run_id)
            self._cleanup(run)

    def _enter(self, run: _ActiveRun, status: RunStatus) -> None:
        previous = run.machine.current
        change = run.machine.transition(status)
        self._repository.update_status(run.run_id, status, change.entered_at)
        run.recorder.emit(
            EventType.RUN_STATUS_CHANGED, previous=previous.value, current=status.value
        )

    def _update_manifest(self, run: _ActiveRun, **changes: object) -> None:
        run.manifest = run.manifest.model_copy(update=changes)
        self._repository.update_manifest(run.run_id, run.manifest)

    # ---------------------------------------------------------------- stages

    def _stage_validate(self, run: _ActiveRun) -> None:
        """Pre-flight: the task file is unchanged since loading, and the backend is up."""
        reloaded = load_task(run.task.path)
        if reloaded.definition_hash != run.task.definition_hash:
            raise TaskValidationError(
                str(run.task.path),
                [ValidationIssue(field="<file>", reason="task file changed after it was loaded")],
            )
        backend = self._backend.describe()
        self._update_manifest(run, backend=backend.name, backend_version=backend.version)
        run.recorder.emit(
            EventType.TASK_VALIDATED,
            definition_hash=run.task.definition_hash,
            warnings=len(run.task.warnings),
            backend=backend.name,
            backend_version=backend.version,
        )

    def _stage_prepare_source(self, run: _ActiveRun) -> None:
        spec = run.task.spec
        run.recorder.emit(EventType.SOURCE_PREPARATION_STARTED, source_type=spec.source.type)
        started = monotonic_ms()
        run.work_dir.mkdir(parents=True, exist_ok=False)
        source = prepare_source(
            run.task, run.work_dir / "source", timeout_seconds=spec.timeouts.source_seconds
        )
        run.source = source
        run.baseline = source.path
        assets_hash = (
            hash_directory(run.task.host_path(spec.grading.assets)) if spec.grading.assets else None
        )
        self._update_manifest(
            run,
            source=SourceProvenance(
                source_type=source.source_type,
                tree_hash=source.tree_hash,
                repository=source.repository,
                requested_revision=source.requested_revision,
                resolved_commit=source.resolved_commit,
            ),
            grader_assets_hash=assets_hash,
        )
        run.recorder.emit(
            EventType.SOURCE_PREPARATION_COMPLETED,
            duration_ms=elapsed_ms(started),
            tree_hash=source.tree_hash,
            resolved_commit=source.resolved_commit,
        )

    def _stage_build_image(self, run: _ActiveRun) -> None:
        environment = run.task.spec.environment
        image_request = task_image(run.task)
        spec, context_hash = image_request.spec, image_request.build_context_hash
        run.recorder.emit(
            EventType.SANDBOX_BUILD_STARTED,
            dockerfile=environment.dockerfile,
            image=environment.image,
        )
        started = monotonic_ms()
        image = self._backend.prepare_image(spec)
        run.image = image
        if image.build_log:
            run.recorder.save_artifact("sandbox/build.log", image.build_log.encode("utf-8"))
        self._update_manifest(
            run,
            image=ImageProvenance(
                reference=image.reference,
                image_id=image.image_id,
                cached=image.cached,
                build_context_hash=context_hash,
                repo_digests=image.repo_digests,
            ),
        )
        run.recorder.emit(
            EventType.SANDBOX_BUILD_COMPLETED,
            duration_ms=elapsed_ms(started),
            image_id=image.image_id,
            cached=image.cached,
        )

    def _stage_start_agent_sandbox(self, run: _ActiveRun) -> None:
        if run.source is None:
            raise RuntimeError("source must be prepared before starting the sandbox")
        sandbox = self._create_sandbox(run, SandboxRole.AGENT)
        run.agent_sandbox = sandbox
        uploader = self._session(run, sandbox, timeout_seconds=None, role=None)
        uploader.upload_directory(run.source.path, run.workspace)

        setup = run.task.spec.setup
        if not setup:
            return
        timeouts = run.task.spec.timeouts
        session = self._session(
            run, sandbox, timeout_seconds=timeouts.setup_seconds, role="setup",
            timeout_error=SetupCommandError,
        )  # fmt: skip
        run.recorder.emit(EventType.SETUP_STARTED, commands=len(setup))
        started = monotonic_ms()
        for command in setup:
            execution = session.run(command.command, timeout_seconds=command.timeout_seconds)
            if not execution.succeeded:
                run.recorder.save_artifact(
                    "setup/failed-command.log",
                    (execution.stdout + "\n--- stderr ---\n" + execution.stderr).encode("utf-8"),
                )
                raise SetupCommandError(
                    f"setup command {' '.join(command.command)!r} "
                    + ("timed out" if execution.timed_out else f"exited {execution.exit_code}"),
                    details={"exit_code": execution.exit_code, "timed_out": execution.timed_out},
                )
        # Setup may change the workspace (generated files, git init ...). The agent starts
        # from the post-setup state, so that state is the baseline for diffs and restores.
        baseline = run.work_dir / "baseline"
        session.download_directory(run.workspace, baseline)
        run.baseline = baseline
        run.recorder.emit(EventType.SETUP_COMPLETED, duration_ms=elapsed_ms(started))

    def _stage_run_agent(self, run: _ActiveRun) -> None:
        if run.agent_sandbox is None:
            raise RuntimeError("agent sandbox must be running before the agent starts")
        budget = float(run.request.agent_timeout_seconds or run.task.spec.timeouts.agent_seconds)
        session = self._session(run, run.agent_sandbox, timeout_seconds=budget, role="agent")
        agent = run.request.agent
        context = AgentContext(
            run_id=run.run_id,
            task=AgentTask(
                task_id=run.task.spec.id,
                name=run.task.spec.name,
                instructions=run.task.spec.instructions,
                workspace=run.workspace,
            ),
            session=session,
            time_budget_seconds=budget,
            save_artifact=_artifact_writer(run.recorder),
            seed=run.request.seed,
        )
        run.recorder.emit(EventType.AGENT_STARTED, agent=agent.name, time_budget_seconds=budget)
        started = monotonic_ms()
        try:
            result = call_with_timeout(
                lambda: agent.run(context),
                budget + self._settings.adapter_grace_seconds,
                name="agent",
                cancellation=run.request.cancellation,
            )
        except CallTimeoutError as error:
            # The adapter thread cannot be killed; revoke its session and destroy its
            # sandbox so that any further action it attempts fails immediately.
            session.close()
            self._destroy(run, run.agent_sandbox)
            raise AgentTimeoutError(
                f"agent exceeded its {budget:g}s time budget",
                details={"limit_seconds": budget, "agent": agent.name},
            ) from error
        except (SandboxPathNotFoundError, SessionClosedError) as error:
            raise AgentError(f"agent {agent.name} failed: {error}") from error
        except (AgentEnvForgeError, KeyboardInterrupt):
            raise
        # BaseException: an adapter calling sys.exit() must not escape the run lifecycle.
        except BaseException as error:
            raise AgentError(
                f"agent adapter {agent.name} raised {type(error).__name__}: {error}"
            ) from error
        finally:
            session.close()
        run.agent_result = result
        run.recorder.emit(
            EventType.AGENT_COMPLETED,
            duration_ms=elapsed_ms(started),
            outcome=result.outcome.value,
            exit_code=result.exit_code,
            commands=result.command_count,
        )

    def _stage_capture_solution(self, run: _ActiveRun) -> None:
        if run.agent_sandbox is None or run.baseline is None:
            raise RuntimeError("an agent sandbox and baseline are required to capture a solution")
        final = run.work_dir / "final"
        session = self._session(run, run.agent_sandbox, timeout_seconds=None, role=None)
        skipped = session.download_directory(run.workspace, final)
        run.final = final
        diff = diff_trees(run.baseline, final)
        run.recorder.save_artifact("solution.patch", diff.patch.encode("utf-8"))
        run.recorder.save_json_artifact(
            "changed_files.json",
            {"added": diff.added, "modified": diff.modified, "deleted": diff.deleted,
             "skipped_unsafe_entries": skipped},
        )  # fmt: skip
        included, too_large = collect_included_files(final, run.task.spec.artifacts.include)
        for item in included:
            run.recorder.save_artifact(f"workspace/{item.name}", item.content)
        # The agent sandbox is no longer needed; release its resources before grading.
        self._destroy(run, run.agent_sandbox)
        run.recorder.emit(
            EventType.SOLUTION_CAPTURED,
            files_changed=len(diff.changed_files),
            added=len(diff.added),
            modified=len(diff.modified),
            deleted=len(diff.deleted),
            patch_bytes=len(diff.patch),
            unsafe_entries_skipped=len(skipped),
            included_files_skipped=len(too_large),
        )

    def _stage_run_grader(self, run: _ActiveRun) -> None:
        if run.final is None or run.baseline is None:
            raise RuntimeError("a captured solution is required before grading")
        spec = run.task.spec
        grading_tree = run.work_dir / "grading"
        restored = build_grading_tree(
            run.final, run.baseline, grading_tree, spec.grading.protected_paths
        )
        sandbox = self._create_sandbox(run, SandboxRole.GRADER)
        budget = float(spec.timeouts.grader_seconds)
        session = self._session(
            run, sandbox, timeout_seconds=budget, role="grader", timeout_error=GraderTimeoutError
        )
        session.upload_directory(grading_tree, run.workspace)
        assets_dir: PurePosixPath | None = None
        if spec.grading.assets is not None:
            assets_dir = PurePosixPath(spec.grading.assets_mount)
            session.upload_directory(run.task.host_path(spec.grading.assets), assets_dir)

        grader = build_grader(spec.grading)
        context = GradingContext(
            run_id=run.run_id,
            task_id=spec.id,
            session=session,
            workspace=run.workspace,
            assets_dir=assets_dir,
            save_artifact=_artifact_writer(run.recorder),
        )
        run.recorder.emit(
            EventType.GRADER_STARTED,
            grader=grader.name,
            grader_type=grader.grader_type,
            protected_paths_restored=len(restored),
        )
        started = monotonic_ms()
        try:
            grade = call_with_timeout(
                lambda: grader.grade(context),
                budget + self._settings.adapter_grace_seconds,
                name="grader",
                cancellation=run.request.cancellation,
            )
        except CallTimeoutError as error:
            session.close()
            self._destroy(run, sandbox)
            raise GraderTimeoutError(
                f"grader exceeded its {budget:g}s limit", details={"limit_seconds": budget}
            ) from error
        except (AgentEnvForgeError, KeyboardInterrupt):
            raise
        except BaseException as error:
            raise GraderError(
                f"grader {grader.name} raised {type(error).__name__}: {error}"
            ) from error
        finally:
            session.close()
        run.grade = grade
        run.recorder.save_json_artifact("grade.json", grade.model_dump(mode="json"))
        run.recorder.emit(
            EventType.GRADER_COMPLETED,
            duration_ms=elapsed_ms(started),
            passed=grade.passed,
            score=grade.score,
            summary=grade.summary,
        )

    def _stage_collect_artifacts(self, run: _ActiveRun) -> None:
        run.recorder.save_artifact("commands.jsonl", run.recorder.command_log())
        run.recorder.save_json_artifact("manifest.json", run.manifest.model_dump(mode="json"))

    # --------------------------------------------------------------- helpers

    def _session(
        self,
        run: _ActiveRun,
        sandbox: Sandbox,
        *,
        timeout_seconds: float | None,
        role: str | None,
        timeout_error: type[AgentEnvForgeError] = AgentTimeoutError,
    ) -> SandboxSession:
        command_timeout = float(run.task.spec.timeouts.command_seconds)
        if role in {"grader", "setup"} and timeout_seconds is not None:
            command_timeout = timeout_seconds
        return SandboxSession(
            sandbox,
            workspace=run.workspace,
            owner=run.owner,
            default_command_timeout_seconds=command_timeout,
            deadline=time.monotonic() + timeout_seconds if timeout_seconds is not None else None,
            observer=run.recorder.command_observer(role) if role is not None else None,
            timeout_error=timeout_error,
            cancellation=run.request.cancellation,
        )

    def _create_sandbox(self, run: _ActiveRun, role: SandboxRole) -> Sandbox:
        if run.image is None:
            raise RuntimeError("an image is required before creating sandboxes")
        spec = run.task.spec
        config = SandboxConfig(
            run_id=run.run_id,
            task_id=spec.id,
            role=role,
            image=run.image,
            cpus=spec.resources.cpus,
            memory_mb=spec.resources.memory_mb,
            pids_limit=spec.resources.pids_limit,
            network_enabled=spec.network.enabled,
            owner=run.owner,
            environment=dict(spec.environment.variables),
        )
        started = monotonic_ms()
        start_timeout = self._settings.sandbox_start_timeout_seconds
        try:
            sandbox = call_with_timeout(
                lambda: self._backend.create_sandbox(config),
                start_timeout,
                name=f"sandbox-start-{role.value}",
                # Not cancellable: a start abandoned half-way is exactly what leaks containers.
                on_abandoned=_destroy_late_sandbox,
            )
        except CallTimeoutError as error:
            # If the abandoned start completes later, _destroy_late_sandbox removes the
            # container; the label-based sweep at cleanup covers the remaining window.
            raise SandboxStartError(
                f"{role.value} sandbox did not start within {start_timeout:g}s",
                details={"timeout_seconds": start_timeout},
            ) from error
        run.sandboxes.append(sandbox)
        run.recorder.emit(
            EventType.SANDBOX_STARTED,
            duration_ms=elapsed_ms(started),
            role=role.value,
            sandbox=sandbox.name,
            sandbox_id=sandbox.sandbox_id,
            cpus=config.cpus,
            memory_mb=config.memory_mb,
            pids_limit=config.pids_limit,
            network_enabled=config.network_enabled,
        )
        return sandbox

    def _destroy(self, run: _ActiveRun, sandbox: Sandbox) -> None:
        if sandbox.name in run.destroyed:
            return
        run.recorder.emit(EventType.SANDBOX_CLEANUP_STARTED, sandbox=sandbox.name)
        started = monotonic_ms()
        try:
            call_with_timeout(
                sandbox.destroy, self._settings.cleanup_timeout_seconds, name="sandbox-destroy"
            )
        except (SandboxError, CallTimeoutError) as error:
            # Cleanup failures are recorded but never replace the run's primary failure.
            message = f"could not remove sandbox {sandbox.name}: {error}"
            run.cleanup_errors.append(message)
            logger.warning(message)
            run.recorder.emit(
                EventType.SANDBOX_CLEANUP_FAILED, sandbox=sandbox.name, error=str(error)
            )
            return
        run.destroyed.add(sandbox.name)
        run.recorder.emit(
            EventType.SANDBOX_CLEANUP_COMPLETED,
            duration_ms=elapsed_ms(started),
            sandbox=sandbox.name,
        )

    def _cleanup(self, run: _ActiveRun) -> None:
        # Each step is isolated: a failure removing one sandbox (or recording that it was
        # removed) must not prevent the remaining sandboxes from being removed.
        for sandbox in reversed(run.sandboxes):
            _guarded(
                run,
                f"removing sandbox {sandbox.name}",
                functools.partial(self._destroy, run, sandbox),
            )
        _guarded(run, "sweeping untracked sandboxes", lambda: self._sweep_untracked_sandboxes(run))
        if run.request.keep_workspace:
            logger.info("keeping work directory %s", run.work_dir)
            return
        try:
            remove_tree(run.work_dir)
        except OSError as error:
            run.cleanup_errors.append(f"could not remove work directory {run.work_dir}: {error}")

    def _sweep_untracked_sandboxes(self, run: _ActiveRun) -> None:
        """Remove sandboxes of this run that were never handed to the orchestrator.

        Normally there are none. They appear only when sandbox creation was abandoned after a
        timeout or cancellation but completed later, so this is a cheap safety net that makes
        "no container outlives its run" hold on those paths too.
        """
        try:
            swept = self._backend.remove_orphaned_sandboxes(run.run_id)
        except SandboxError as error:
            run.cleanup_errors.append(f"could not sweep sandboxes of {run.run_id}: {error}")
            return
        for name in swept:
            if name not in run.destroyed:
                run.destroyed.add(name)
                logger.warning("removed untracked sandbox %s", name)
                run.recorder.emit(EventType.SANDBOX_CLEANUP_COMPLETED, sandbox=name, untracked=True)

    def _finalize(self, run: _ActiveRun) -> RunResult:
        terminal = (
            RunStatus.COMPLETED if run.failure is None else terminal_status_for(run.failure.reason)
        )
        self._enter(run, terminal)
        created_at = run.machine.history[0].entered_at
        finished_at = run.machine.history[-1].entered_at
        result = RunResult(
            run_id=run.run_id,
            task_id=run.task.spec.id,
            status=terminal,
            passed=run.grade.passed if run.grade else None,
            score=run.grade.score if run.grade else None,
            failure=run.failure,
            agent=run.agent_result,
            grade=run.grade,
            stage_timings=_stage_timings(run.machine),
            cleanup=CleanupReport(
                succeeded=not run.cleanup_errors,
                sandboxes_removed=tuple(sorted(run.destroyed)),
                errors=tuple(run.cleanup_errors),
            ),
            created_at=created_at,
            finished_at=finished_at,
            duration_ms=round((finished_at - created_at).total_seconds() * 1000),
        )
        # Secrets can appear in failure messages; the stored result is redacted like every
        # other record of the run.
        result = RunResult.model_validate_json(run.recorder.redact(result.model_dump_json()))
        self._repository.complete_run(result)
        if run.failure is None:
            run.recorder.emit(EventType.RUN_COMPLETED, passed=result.passed, score=result.score)
        else:
            run.recorder.emit(
                EventType.RUN_FAILED,
                reason=run.failure.reason.value,
                stage=run.failure.stage.value,
                message=run.failure.message,
            )
        if run.failure is not None:
            # Earlier stages that normally write these were skipped.
            run.recorder.save_artifact("commands.jsonl", run.recorder.command_log())
            run.recorder.save_json_artifact("manifest.json", run.manifest.model_dump(mode="json"))
        run.recorder.save_json_artifact("result.json", result.model_dump(mode="json"))
        events = self._repository.list_events(run.run_id)
        run.recorder.save_artifact(
            "events.jsonl",
            "".join(event.model_dump_json() + "\n" for event in events).encode("utf-8"),
        )
        return result

    def _store_log(self, run: _ActiveRun, log_path: Path) -> None:
        try:
            content = log_path.read_bytes()
        except FileNotFoundError:
            return
        run.recorder.save_artifact("logs/agentenv.log", content)
        log_path.unlink(missing_ok=True)


def _classify_failure(run: _ActiveRun, error: BaseException) -> FailureInfo:
    stage = run.machine.current
    cancellation = run.request.cancellation
    if (
        cancellation is not None
        and cancellation.cancelled
        and not isinstance(error, RunCancelledError)
    ):
        # Carrying out a cancellation closes sessions and removes containers, which makes
        # in-flight work fail with secondary errors. Those are consequences, not causes.
        return FailureInfo(
            reason=FailureReason.CANCELLED,
            stage=stage,
            message=f"cancelled: {cancellation.reason}",
            error_type=type(error).__name__,
            details={"consequential_error": str(error)[:300]},
        )
    return FailureInfo.from_exception(error, stage)


def _destroy_late_sandbox(sandbox: Sandbox) -> None:
    try:
        sandbox.destroy()
    except SandboxError:
        logger.exception("could not remove late-created sandbox %s", sandbox.name)


def _guarded(run: _ActiveRun, what: str, action: Callable[[], object]) -> None:
    try:
        action()
    except Exception as error:  # cleanup boundary: record and continue
        message = f"{what} failed: {type(error).__name__}: {error}"
        run.cleanup_errors.append(message)
        logger.exception(message)


def _artifact_writer(recorder: RunRecorder) -> ArtifactWriter:
    """Expose ``recorder.save_artifact`` to adapters without leaking the recorder itself."""

    def write(name: str, content: bytes) -> None:
        recorder.save_artifact(name, content)

    return write


def _stage_timings(machine: RunStateMachine) -> tuple[StageTiming, ...]:
    history = machine.history
    return tuple(
        StageTiming(
            status=change.status,
            started_at=change.entered_at,
            duration_ms=round((following.entered_at - change.entered_at).total_seconds() * 1000),
        )
        for change, following in itertools.pairwise(history)
    )
