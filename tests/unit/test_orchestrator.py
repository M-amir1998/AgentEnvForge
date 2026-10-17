"""The run orchestrator, end to end against the fake sandbox backend.

These tests pin down the lifecycle guarantees: classification of every failure, cleanup
on every path, isolation of hidden assets, restoration of protected paths and redaction.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentenvforge.agents.base import AgentAdapter, AgentContext
from agentenvforge.agents.builtin import CommandAgent, NoopAgent
from agentenvforge.artifacts import LocalArtifactStore
from agentenvforge.domain.commands import CommandExecution, CommandRequest
from agentenvforge.domain.events import EventType
from agentenvforge.domain.failures import FailureReason, SandboxBuildError
from agentenvforge.domain.results import AgentOutcome, AgentResult, RunResult
from agentenvforge.domain.status import RunStatus
from agentenvforge.execution.orchestrator import OrchestratorSettings, RunOrchestrator, RunRequest
from agentenvforge.persistence.sqlite import SqliteRunRepository
from agentenvforge.sandbox.base import SandboxRole
from agentenvforge.tasks.loader import load_task
from agentenvforge.telemetry.redaction import REDACTED
from tests.conftest import EXAMPLE_TASK
from tests.fakes import FakeBackend, FakeSandbox, make_execution, pytest_handler

FIXED_MARKER = "# fixed by the fake agent\n"


def fixing_agent_handler(request: CommandRequest, sandbox: FakeSandbox) -> CommandExecution:
    """Simulates an agent: edits the source file when its command runs."""
    store = sandbox.host_path("/workspace/ledger/store.py")
    store.write_text(store.read_text() + FIXED_MARKER)
    return make_execution(request, stdout="patched")


@dataclass
class Harness:
    backend: FakeBackend
    repository: SqliteRunRepository
    artifacts: LocalArtifactStore
    orchestrator: RunOrchestrator
    work_dir: Path

    def run(
        self, agent: AgentAdapter, task_path: Path = EXAMPLE_TASK, secrets: tuple[str, ...] = ()
    ) -> RunResult:
        request = RunRequest(task=load_task(task_path), agent=agent, secrets=secrets)
        return self.orchestrator.execute(request)

    def artifact(self, result: RunResult, name: str) -> bytes:
        return self.artifacts.get(result.run_id, name)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    backend = FakeBackend(
        root=tmp_path / "sandboxes",
        handlers={
            SandboxRole.AGENT: fixing_agent_handler,
            SandboxRole.GRADER: pytest_handler(tests=3),
        },
    )
    repository = SqliteRunRepository(tmp_path / "db.sqlite")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    orchestrator = RunOrchestrator(
        backend=backend,
        repository=repository,
        artifact_store=artifacts,
        settings=OrchestratorSettings(work_dir=work_dir, adapter_grace_seconds=0.2),
    )
    yield Harness(backend, repository, artifacts, orchestrator, work_dir)
    repository.close()


def solver() -> CommandAgent:
    return CommandAgent(label="fake", command=("solve",))


# ------------------------------------------------------------------ happy path


def test_successful_run_records_everything_and_cleans_up(harness: Harness) -> None:
    result = harness.run(solver())

    assert result.status is RunStatus.COMPLETED
    assert result.succeeded and result.score == 1.0
    assert result.agent is not None and result.agent.outcome is AgentOutcome.COMPLETED
    assert result.grade is not None
    assert [component.grader for component in result.grade.components] == [
        "hidden-tests",
        "visible-tests",
    ]
    assert result.cleanup.succeeded

    # Two sandboxes - agent and grader - each destroyed exactly once.
    assert [sandbox.destroy_calls for sandbox in harness.backend.sandboxes] == [1, 1]
    assert list(harness.work_dir.iterdir()) == []

    patch = harness.artifact(result, "solution.patch").decode()
    assert "+# fixed by the fake agent" in patch
    assert json.loads(harness.artifact(result, "changed_files.json"))["modified"] == [
        "ledger/store.py"
    ]
    for name in (
        "result.json",
        "manifest.json",
        "events.jsonl",
        "commands.jsonl",
        "logs/agentenv.log",
    ):
        assert harness.artifact(result, name)

    stored = harness.repository.get_run(result.run_id)
    assert stored is not None and stored.result == result
    assert stored.manifest.source is not None and stored.manifest.image is not None
    sequences = [event.sequence for event in harness.repository.list_events(result.run_id)]
    assert sequences == list(range(1, len(sequences) + 1))


def test_lifecycle_passes_through_every_stage_in_order(harness: Harness) -> None:
    result = harness.run(solver())
    statuses = [timing.status for timing in result.stage_timings]
    assert statuses == [
        RunStatus.CREATED,
        RunStatus.VALIDATING,
        RunStatus.PREPARING_SOURCE,
        RunStatus.BUILDING_SANDBOX,
        RunStatus.STARTING_SANDBOX,
        RunStatus.RUNNING_AGENT,
        RunStatus.CAPTURING_SOLUTION,
        RunStatus.RUNNING_GRADER,
        RunStatus.COLLECTING_ARTIFACTS,
        RunStatus.CLEANING_UP,
    ]


def test_hidden_assets_reach_only_the_grading_sandbox(harness: Harness) -> None:
    harness.run(solver())
    agent_box = harness.backend.sandbox(SandboxRole.AGENT)
    grader_box = harness.backend.sandbox(SandboxRole.GRADER)
    hidden = "/grader/tests/test_hidden_transactions.py"
    assert not agent_box.host_path("/grader").exists()
    assert grader_box.host_path(hidden).is_file()
    # The reference solution is never copied anywhere.
    for box in (agent_box, grader_box):
        assert not list(box.root.rglob("solution.patch"))


def test_grader_sees_the_agents_changes(harness: Harness) -> None:
    harness.run(solver())
    graded = harness.backend.sandbox(SandboxRole.GRADER).host_path("/workspace/ledger/store.py")
    assert graded.read_text().endswith(FIXED_MARKER)


def test_protected_paths_are_restored_before_grading(harness: Harness) -> None:
    def tampering_agent(request: CommandRequest, sandbox: FakeSandbox) -> CommandExecution:
        sandbox.host_path("/workspace/tests/test_store.py").write_text("def test_ok(): pass\n")
        sandbox.host_path("/workspace/tests/test_extra.py").write_text("def test_more(): pass\n")
        return make_execution(request)

    harness.backend.handlers[SandboxRole.AGENT] = tampering_agent
    result = harness.run(solver())

    tests_dir = harness.backend.sandbox(SandboxRole.GRADER).host_path("/workspace/tests")
    original = (EXAMPLE_TASK.parent / "project" / "tests" / "test_store.py").read_text()
    assert (tests_dir / "test_store.py").read_text() == original
    assert not (tests_dir / "test_extra.py").exists()
    # The tampering is still visible in the recorded patch, for later analysis.
    assert "def test_ok(): pass" in harness.artifact(result, "solution.patch").decode()


# ---------------------------------------------------------- graded outcomes


def test_failing_solution_completes_with_failed_grade(harness: Harness) -> None:
    harness.backend.handlers[SandboxRole.GRADER] = pytest_handler(tests=4, failures=3)
    result = harness.run(NoopAgent())
    assert result.status is RunStatus.COMPLETED
    assert result.passed is False
    assert result.failure is None
    assert 0 < (result.score or 0) < 1


def test_agent_nonzero_exit_is_still_graded(harness: Harness) -> None:
    harness.backend.handlers[SandboxRole.AGENT] = lambda request, _: make_execution(
        request, exit_code=1
    )
    result = harness.run(solver())
    assert result.status is RunStatus.COMPLETED
    assert result.agent is not None and result.agent.outcome is AgentOutcome.FAILED
    assert result.grade is not None


# ------------------------------------------------------------------- failures


@dataclass
class ScriptedAgent:
    """In-process adapter running arbitrary behaviour."""

    behaviour: Callable[[AgentContext], AgentResult]

    @property
    def name(self) -> str:
        return "scripted"

    def run(self, context: AgentContext) -> AgentResult:
        return self.behaviour(context)


def _crash(_context: AgentContext) -> AgentResult:
    raise RuntimeError("adapter bug")


def _interrupt(_context: AgentContext) -> AgentResult:
    raise KeyboardInterrupt


def _assert_cleaned_up(harness: Harness) -> None:
    assert all(sandbox.destroy_calls == 1 for sandbox in harness.backend.sandboxes)
    assert list(harness.work_dir.iterdir()) == []


def test_adapter_crash_fails_the_agent_stage(harness: Harness) -> None:
    result = harness.run(ScriptedAgent(_crash))
    assert result.status is RunStatus.FAILED_AGENT
    assert result.failure is not None
    assert result.failure.stage is RunStatus.RUNNING_AGENT
    assert "adapter bug" in result.failure.message
    assert len(harness.backend.sandboxes) == 1  # never reached grading
    _assert_cleaned_up(harness)


def test_agent_exceeding_budget_times_out_and_is_cleaned_up(
    harness: Harness, example_copy: Callable[[Callable[[str], str] | None], Path]
) -> None:
    task = example_copy(lambda text: text.replace("agent_seconds: 300", "agent_seconds: 1"))

    def slow(_context: AgentContext) -> AgentResult:
        time.sleep(5)
        raise AssertionError("unreachable: the run should have timed out")

    started = time.monotonic()
    result = harness.run(ScriptedAgent(slow), task_path=task)
    assert time.monotonic() - started < 4
    assert result.status is RunStatus.TIMED_OUT
    assert result.failure is not None
    assert result.failure.reason is FailureReason.AGENT_TIMEOUT
    assert result.failure.details["limit_seconds"] == 1
    _assert_cleaned_up(harness)


def test_ctrl_c_cancels_and_still_cleans_up(harness: Harness) -> None:
    result = harness.run(ScriptedAgent(_interrupt))
    assert result.status is RunStatus.CANCELLED
    _assert_cleaned_up(harness)


def test_build_failure_is_classified_before_any_sandbox_starts(harness: Harness) -> None:
    harness.backend.build_error = SandboxBuildError("pip install failed", details={"step": 2})
    result = harness.run(solver())
    assert result.status is RunStatus.FAILED_BUILD
    assert result.failure is not None
    assert result.failure.stage is RunStatus.BUILDING_SANDBOX
    assert result.failure.details == {"step": 2}
    assert harness.backend.sandboxes == []
    assert harness.artifact(result, "result.json")


def test_task_changed_after_loading_fails_validation(
    harness: Harness, example_copy: Callable[[Callable[[str], str] | None], Path]
) -> None:
    task_path = example_copy(None)
    loaded = load_task(task_path)
    task_path.write_text(task_path.read_text().replace("memory_mb: 512", "memory_mb: 1024"))
    result = harness.orchestrator.execute(RunRequest(task=loaded, agent=NoopAgent()))
    assert result.status is RunStatus.FAILED_VALIDATION


def test_failed_setup_command_fails_the_sandbox_stage(
    harness: Harness, example_copy: Callable[[Callable[[str], str] | None], Path]
) -> None:
    task = example_copy(
        lambda text: text.replace(
            "resources:", 'setup:\n  - command: ["make", "init"]\n\nresources:'
        )
    )
    harness.backend.handlers[SandboxRole.AGENT] = lambda request, _: make_execution(
        request, exit_code=2
    )
    result = harness.run(solver(), task_path=task)
    assert result.status is RunStatus.FAILED_SANDBOX
    assert result.failure is not None and result.failure.reason is FailureReason.SETUP_COMMAND
    _assert_cleaned_up(harness)


def test_cleanup_failure_is_reported_without_masking_the_result(harness: Harness) -> None:
    def agent_then_break_destroy(request: CommandRequest, sandbox: FakeSandbox) -> CommandExecution:
        sandbox.fail_destroy = True
        return fixing_agent_handler(request, sandbox)

    harness.backend.handlers[SandboxRole.AGENT] = agent_then_break_destroy
    result = harness.run(solver())
    assert result.status is RunStatus.COMPLETED and result.passed
    assert not result.cleanup.succeeded
    assert "cannot remove" in result.cleanup.errors[0]
    events = [event.event_type for event in harness.repository.list_events(result.run_id)]
    assert EventType.SANDBOX_CLEANUP_FAILED in events


# ---------------------------------------------------------------- redaction


def test_secrets_are_redacted_from_every_record(harness: Harness) -> None:
    secret = "sk-test-0123456789"

    def leaky_agent(request: CommandRequest, sandbox: FakeSandbox) -> CommandExecution:
        assert request.env["API_KEY"] == secret  # the agent itself does get the secret
        return make_execution(request, stdout=f"using key {secret}")

    harness.backend.handlers[SandboxRole.AGENT] = leaky_agent
    agent = CommandAgent(label="leaky", command=("solve",), secrets={"API_KEY": secret})
    result = harness.run(agent, secrets=(secret,))

    for name in ("agent/stdout.log", "commands.jsonl", "events.jsonl", "logs/agentenv.log"):
        content = harness.artifact(result, name).decode()
        assert secret not in content, name
    assert REDACTED in harness.artifact(result, "agent/stdout.log").decode()
