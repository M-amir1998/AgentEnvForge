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

import hashlib
import itertools
import logging
import platform
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agentenvforge._version import __version__
from agentenvforge.agents.base import AgentAdapter, AgentContext, AgentTask, ArtifactWriter
from agentenvforge.artifacts import ArtifactStore
from agentenvforge.domain.clock import elapsed_ms, monotonic_ms
from agentenvforge.domain.events import EventType
from agentenvforge.domain.failures import (
    AgentEnvForgeError,
    AgentError,
    AgentTimeoutError,
    FailureInfo,
    GraderError,
    GraderTimeoutError,
    SandboxError,
    SandboxPathNotFoundError,
    SetupCommandError,
    TaskValidationError,
    ValidationIssue,
    terminal_status_for,
)
from agentenvforge.domain.ids import new_run_id
from agentenvforge.domain.manifest import ImageProvenance, RunManifest, SourceProvenance
from agentenvforge.domain.results import (
    AgentResult,
    CleanupReport,
    GradeResult,
    RunResult,
    StageTiming,
)
from agentenvforge.domain.status import RunStateMachine, RunStatus
from agentenvforge.events.recorder import EventSink, RunRecorder
from agentenvforge.execution.solution import build_grading_tree, collect_included_files
from agentenvforge.grading.base import GradingContext
from agentenvforge.grading.graders import build_grader
from agentenvforge.persistence.repository import NewRun, RunRepository
from agentenvforge.sandbox.base import (
    ImageSpec,
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
        run_id = new_run_id()
        task = request.task
        with log_context(run_id=run_id, task_id=task.spec.id):
            log_path = self._settings.work_dir / f"{run_id}.log"
            with run_log_file(log_path, run_id):
                run = self._create_run(run_id, request)
                self._drive(run)
                result = self._finalize(run)
            self._store_log(run, log_path)
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
        try:
            for status, stage in stages:
                self._enter(run, status)
                stage(run)
        # This is the one deliberate catch-all boundary: every failure, including
        # unexpected bugs and Ctrl+C, must become a classified, persisted run outcome,
        # and cleanup must still run. The traceback is logged, never discarded.
        except (Exception, KeyboardInterrupt) as error:
            run.failure = FailureInfo.from_exception(error, run.machine.current)
            logger.error(
                "run failed in %s: %s",
                run.machine.current.value,
                run.failure.message,
                exc_info=not isinstance(error, (AgentEnvForgeError, KeyboardInterrupt)),
            )
        finally:
            self._enter(run, RunStatus.CLEANING_UP)
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
        context_hash: str | None = None
        if environment.dockerfile is not None:
            dockerfile = run.task.host_path(environment.dockerfile)
            context = (
                run.task.host_path(environment.context)
                if environment.context
                else dockerfile.parent
            )
            context_hash = hash_directory(context)
            cache_key = hashlib.sha256(
                f"{context_hash}:{dockerfile.relative_to(context).as_posix()}".encode()
            ).hexdigest()
            spec = ImageSpec(
                task_id=run.task.spec.id,
                dockerfile=dockerfile,
                context=context,
                image=None,
                cache_key=cache_key,
                timeout_seconds=run.task.spec.timeouts.build_seconds,
            )
        else:
            spec = ImageSpec(
                task_id=run.task.spec.id,
                dockerfile=None,
                context=None,
                image=environment.image,
                cache_key="",
                timeout_seconds=run.task.spec.timeouts.build_seconds,
            )
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
        )
        run.recorder.emit(EventType.AGENT_STARTED, agent=agent.name, time_budget_seconds=budget)
        started = monotonic_ms()
        try:
            result = call_with_timeout(
                lambda: agent.run(context),
                budget + self._settings.adapter_grace_seconds,
                name="agent",
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
        except AgentEnvForgeError:
            raise
        except Exception as error:
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
            )
        except CallTimeoutError as error:
            session.close()
            self._destroy(run, sandbox)
            raise GraderTimeoutError(
                f"grader exceeded its {budget:g}s limit", details={"limit_seconds": budget}
            ) from error
        except AgentEnvForgeError:
            raise
        except Exception as error:
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
        sandbox = call_with_timeout(
            lambda: self._backend.create_sandbox(config),
            self._settings.sandbox_start_timeout_seconds,
            name=f"sandbox-start-{role.value}",
        )
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
        for sandbox in reversed(run.sandboxes):
            self._destroy(run, sandbox)
        if run.request.keep_workspace:
            logger.info("keeping work directory %s", run.work_dir)
            return
        try:
            remove_tree(run.work_dir)
        except OSError as error:
            run.cleanup_errors.append(f"could not remove work directory {run.work_dir}: {error}")

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
