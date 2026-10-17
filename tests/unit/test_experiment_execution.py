"""Scheduler and experiment executor against the fake sandbox backend (no Docker needed)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentenvforge.agents.base import AgentContext
from agentenvforge.agents.spec import AgentEnvironment
from agentenvforge.analysis.outcomes import Outcome
from agentenvforge.artifacts import LocalArtifactStore
from agentenvforge.cancellation import CancellationToken
from agentenvforge.domain.commands import CommandExecution, CommandRequest
from agentenvforge.domain.failures import AgentError, SandboxStartError
from agentenvforge.domain.results import AgentOutcome, AgentResult, RunResult
from agentenvforge.execution.experiments import ExperimentExecutor, ExperimentOutcome, Progress
from agentenvforge.execution.orchestrator import OrchestratorSettings, RunOrchestrator
from agentenvforge.execution.scheduler import BoundedScheduler, Completed
from agentenvforge.experiments.loader import load_experiment
from agentenvforge.experiments.matrix import RunCombination
from agentenvforge.experiments.status import ExperimentStatus
from agentenvforge.persistence.sqlite import SqliteRunRepository
from agentenvforge.persistence.sqlite_database import SqliteDatabase
from agentenvforge.persistence.sqlite_experiments import SqliteExperimentRepository
from agentenvforge.sandbox.base import SandboxBackend, SandboxRole
from tests.conftest import EXAMPLE_DIR
from tests.fakes import FakeBackend, FakeSandbox, make_execution

FIXED = "# fixed\n"

# ------------------------------------------------------------------------ scheduler


def test_scheduler_never_exceeds_its_worker_bound_and_processes_everything() -> None:
    active, peak, lock = 0, 0, threading.Lock()

    def work(item: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.005)
        with lock:
            active -= 1
        return item * 2

    done: list[Completed[int, int]] = []
    BoundedScheduler(4).run(
        range(120), work, cancellation=CancellationToken(), on_complete=done.append
    )
    assert peak <= 4
    assert sorted(c.result for c in done if c.result is not None) == [i * 2 for i in range(120)]


def test_scheduler_isolates_failures() -> None:
    def work(item: int) -> int:
        if item == 3:
            raise RuntimeError("boom")
        return item

    done: list[Completed[int, int]] = []
    BoundedScheduler(2).run(
        range(6), work, cancellation=CancellationToken(), on_complete=done.append
    )
    errors = [c for c in done if c.error is not None]
    assert len(done) == 6 and [c.item for c in errors] == [3]


def test_scheduler_stops_taking_items_after_cancellation() -> None:
    token = CancellationToken()
    done: list[Completed[int, int]] = []

    def on_complete(completed: Completed[int, int]) -> None:
        done.append(completed)
        if len(done) == 3:
            token.cancel("test")

    def work(item: int) -> int:
        time.sleep(0.01)  # items take time, like runs; cancellation stops *new* items only
        return item

    BoundedScheduler(1).run(range(100), work, cancellation=token, on_complete=on_complete)
    assert 3 <= len(done) <= 5  # at most the item in flight (plus one racing) after the 3rd


def test_scheduler_cancels_and_reraises_when_bookkeeping_fails() -> None:
    token = CancellationToken()

    def on_complete(_: Completed[int, int]) -> None:
        raise ValueError("observer bug")

    with pytest.raises(ValueError, match="observer bug"):
        BoundedScheduler(2).run(range(50), lambda i: i, cancellation=token, on_complete=on_complete)
    assert token.cancelled


# ------------------------------------------------------------------------- executor


@dataclass
class Gauge:
    """Shared between adapters to observe how many runs are active at once."""

    active: int = 0
    peak: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


GAUGE = Gauge()
CANCEL_AFTER: list[CancellationToken] = []  # set by a test before the experiment starts


class SolverAgent:
    """Fixes the example task in-process (through the session), tracking concurrency."""

    name = "solver"

    def run(self, context: AgentContext) -> AgentResult:
        with GAUGE.lock:
            GAUGE.active += 1
            GAUGE.peak = max(GAUGE.peak, GAUGE.active)
        time.sleep(0.02)
        source = context.session.read_file("ledger/store.py").decode()
        context.session.write_file("ledger/store.py", (source + FIXED).encode())
        with GAUGE.lock:
            GAUGE.active -= 1
        if CANCEL_AFTER:
            CANCEL_AFTER[0].cancel("test requested cancellation")
        return AgentResult(adapter=self.name, outcome=AgentOutcome.COMPLETED)


class CrashingAgent:
    name = "crasher"

    def run(self, context: AgentContext) -> AgentResult:
        raise RuntimeError("adapter bug")


class UnavailableAgent:
    """An agent whose service is down: preflight fails."""

    name = "unavailable"

    def preflight(self) -> dict[str, str | int | float | bool | None]:
        raise AgentError("model server down")

    def run(self, context: AgentContext) -> AgentResult:  # pragma: no cover - never reached
        raise AssertionError("must not run")


def build_solver() -> SolverAgent:
    return SolverAgent()


def build_crasher() -> CrashingAgent:
    return CrashingAgent()


def build_unavailable() -> UnavailableAgent:
    return UnavailableAgent()


def grade_by_fix(request: CommandRequest, sandbox: FakeSandbox) -> CommandExecution:
    """Fake pytest: passes when the workspace contains the fix, writing a JUnit report."""
    fixed = sandbox.host_path("/workspace/ledger/store.py").read_text().endswith(FIXED)
    for argument in request.argv:
        if argument.startswith("--junitxml="):
            report = sandbox.host_path(argument.removeprefix("--junitxml="))
            report.parent.mkdir(parents=True, exist_ok=True)
            failures = 0 if fixed else 1
            report.write_text(
                f'<testsuites><testsuite tests="1" failures="{failures}" errors="0" '
                'skipped="0"/></testsuites>'
            )
    return make_execution(request, exit_code=0 if fixed else 1)


AGENTS = {
    "solver": "adapter: tests.unit.test_experiment_execution:build_solver",
    "crasher": "adapter: tests.unit.test_experiment_execution:build_crasher",
    "unavailable": "adapter: tests.unit.test_experiment_execution:build_unavailable",
}


@dataclass
class Harness:
    tmp: Path
    backends: list[FakeBackend] = field(default_factory=list)
    start_error: Exception | None = None

    def __post_init__(self) -> None:
        self.database = SqliteDatabase(self.tmp / "db.sqlite")
        self.runs = SqliteRunRepository(self.database)
        self.experiments = SqliteExperimentRepository(self.database)
        self.artifacts = LocalArtifactStore(self.tmp / "artifacts")
        (self.tmp / "work").mkdir()

    def close(self) -> None:
        self.database.close()

    def backend(self) -> SandboxBackend:
        backend = FakeBackend(
            root=self.tmp / "sandboxes" / f"b{len(self.backends)}",
            handlers={SandboxRole.GRADER: grade_by_fix},
            start_error=self.start_error,
        )
        self.backends.append(backend)
        return backend

    def orchestrator(self, backend: SandboxBackend) -> RunOrchestrator:
        return RunOrchestrator(
            backend=backend,
            repository=self.runs,
            artifact_store=self.artifacts,
            settings=OrchestratorSettings(work_dir=self.tmp / "work", adapter_grace_seconds=0.2),
        )

    def experiment(self, agents: list[str], repetitions: int, workers: int) -> Path:
        shutil_copy = self.tmp / "example"
        if not shutil_copy.exists():
            import shutil

            shutil.copytree(EXAMPLE_DIR, shutil_copy)
        lines = [
            'schema_version: "1"',
            "id: test-experiment",
            "tasks: [example/task.yaml]",
            "agents:",
        ]
        for agent in agents:
            if agent == "noop":
                lines += ["  - id: noop", "    role: baseline", "    agent: {type: noop}"]
            else:
                lines += [
                    f"  - id: {agent}",
                    "    agent:",
                    "      type: python",
                    f"      {AGENTS[agent]}",
                ]
        lines += [f"repetitions: {repetitions}", "execution:", f"  workers: {workers}"]
        path = self.tmp / "experiment.yaml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def execute(
        self, path: Path, workers: int, token: CancellationToken | None = None
    ) -> tuple[ExperimentOutcome, RecordingObserver]:
        environment = AgentEnvironment(environ={}, write=lambda _: None, prompt=lambda _: "")
        loaded = load_experiment(path, environment)
        executor = ExperimentExecutor(
            experiments=self.experiments,
            runs=self.runs,
            backend_factory=self.backend,
            orchestrator_factory=self.orchestrator,
        )
        observer = RecordingObserver()
        outcome = executor.execute(
            loaded, workers=workers, cancellation=token or CancellationToken(), observer=observer
        )
        return outcome, observer

    def all_sandboxes(self) -> list[FakeSandbox]:
        return [s for b in self.backends for s in b.sandboxes]


class RecordingObserver:
    def __init__(self) -> None:
        self.finished: list[tuple[RunCombination, Outcome, Progress]] = []
        self.cancel_reasons: list[str] = []
        self.threads: set[str] = set()

    def preparing(self, message: str) -> None:
        self.threads.add(threading.current_thread().name)

    def run_finished(
        self,
        combination: RunCombination,
        run_id: str | None,
        outcome: Outcome,
        result: RunResult | None,
        progress: Progress,
    ) -> None:
        self.threads.add(threading.current_thread().name)
        self.finished.append((combination, outcome, progress))

    def cancelling(self, reason: str) -> None:
        self.cancel_reasons.append(reason)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    GAUGE.active = GAUGE.peak = 0
    CANCEL_AFTER.clear()
    created: list[Harness] = []

    def make(**kwargs: Any) -> Harness:
        created.append(Harness(tmp_path, **kwargs))
        return created[-1]

    yield make
    for harness in created:
        harness.close()


def test_experiment_runs_every_combination_with_bounded_concurrency(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    outcome, observer = h.execute(h.experiment(["solver", "noop", "crasher"], 4, 3), workers=3)
    assert outcome.status is ExperimentStatus.COMPLETED
    summary = outcome.summary
    assert summary.counts.planned == 12 and summary.invariant_violations == ()
    by_agent = {a.agent_id: a for a in summary.agents}
    assert (by_agent["solver"].success.successes, by_agent["solver"].success.trials) == (4, 4)
    assert by_agent["noop"].counts.failed == 4  # baseline: graded failure
    assert by_agent["crasher"].counts.agent_error == 4  # isolated: others unaffected
    assert summary.tasks[0].baseline_failed_all is True
    assert GAUGE.peak <= 3
    assert observer.threads == {threading.current_thread().name}  # bookkeeping on main thread
    assert all(sandbox.destroy_calls >= 1 for sandbox in h.all_sandboxes())

    stored = h.experiments.get_experiment(outcome.experiment_id)
    assert stored is not None and all(entry.run_id for entry in stored.entries)
    linked = h.experiments.find_entry_by_run(stored.entries[0].run_id or "")
    assert linked is not None and linked.experiment_id == outcome.experiment_id
    run = h.runs.get_run(stored.entries[0].run_id or "")
    assert run is not None and run.manifest.experiment is not None
    assert run.manifest.seed == stored.entries[0].seed
    assert stored.manifest.environment is not None
    assert stored.manifest.tasks[0].image_id == "sha256:fake"


def test_cancellation_stops_scheduling_and_keeps_completed_results(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    token = CancellationToken()
    CANCEL_AFTER.append(token)  # the first finished agent requests cancellation
    outcome, observer = h.execute(h.experiment(["solver"], 6, 1), workers=1, token=token)
    counts = outcome.summary.counts
    assert outcome.status is ExperimentStatus.CANCELLED
    assert counts.not_started >= 4 and counts.planned == 6
    assert counts.started + counts.not_started == counts.planned
    assert outcome.summary.partial and observer.cancel_reasons
    assert all(sandbox.destroy_calls >= 1 for sandbox in h.all_sandboxes())


def test_preflight_failure_fails_the_experiment_before_any_run(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    outcome, _ = h.execute(h.experiment(["solver", "unavailable"], 2, 1), workers=1)
    assert outcome.status is ExperimentStatus.FAILED
    assert outcome.failure is not None and "unavailable is not ready" in outcome.failure
    assert outcome.summary.counts.not_started == 4 and not h.all_sandboxes()


def test_circuit_breaker_stops_a_broken_infrastructure(harness: Callable[..., Harness]) -> None:
    h = harness(start_error=SandboxStartError("docker died"))
    outcome, _ = h.execute(h.experiment(["solver"], 20, 1), workers=1)
    counts = outcome.summary.counts
    assert outcome.status is ExperimentStatus.FAILED
    assert outcome.failure is not None and "consecutive runs" in outcome.failure
    assert counts.infrastructure_error == 5
    # A run already taken by the worker when the breaker tripped is cancelled, not executed.
    assert counts.cancelled <= 1 and counts.not_started == 15 - counts.cancelled
