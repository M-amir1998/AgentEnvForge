"""Sandbox sessions, graders and agent adapters (against the fake sandbox)."""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath

import pytest

from agentenvforge.agents.base import AgentContext, AgentTask
from agentenvforge.agents.builtin import INSTRUCTIONS_PATH, CommandAgent, ManualAgent, NoopAgent
from agentenvforge.agents.spec import AgentEnvironment, command_agent_from_string, resolve_agent
from agentenvforge.domain.commands import CommandExecution, CommandRequest
from agentenvforge.domain.failures import (
    AgentTimeoutError,
    GraderError,
    GraderTimeoutError,
    TaskValidationError,
)
from agentenvforge.domain.results import AgentOutcome, GradeResult
from agentenvforge.grading.base import GradingContext
from agentenvforge.grading.graders import (
    CommandGrader,
    CompositeGrader,
    PytestGrader,
    WeightedGrader,
    build_grader,
)
from agentenvforge.grading.junit import parse_junit_counts
from agentenvforge.sandbox.session import SandboxSession, SessionClosedError
from agentenvforge.tasks.schema import GradingSpec
from agentenvforge.workspace.archive import Ownership
from tests.fakes import CommandHandler, FakeSandbox, make_execution, pytest_handler, succeed

WORKSPACE = PurePosixPath("/workspace")


def make_sandbox(tmp_path: Path, handler: CommandHandler = succeed) -> FakeSandbox:
    (tmp_path / "box" / "tmp").mkdir(parents=True)
    (tmp_path / "box" / "workspace").mkdir()
    return FakeSandbox(tmp_path / "box", "box", handler)


def make_session(sandbox: FakeSandbox, **kwargs: object) -> SandboxSession:
    return SandboxSession(
        sandbox,
        workspace=WORKSPACE,
        owner=Ownership(1000, 1000),
        default_command_timeout_seconds=30,
        **kwargs,  # type: ignore[arg-type]
    )


class RecordingObserver:
    def __init__(self) -> None:
        self.started: list[CommandRequest] = []
        self.completed: list[CommandExecution] = []

    def command_started(self, request: CommandRequest) -> None:
        self.started.append(request)

    def command_completed(self, execution: CommandExecution) -> None:
        self.completed.append(execution)


# ------------------------------------------------------------------------ session


def test_session_resolves_paths_and_notifies_observer(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path)
    observer = RecordingObserver()
    session = make_session(sandbox, observer=observer)

    session.run(["ls"], cwd="src")
    session.write_file("notes.txt", b"hello")

    assert sandbox.requests[0].cwd == "/workspace/src"
    assert session.read_file("notes.txt") == b"hello"
    assert len(observer.started) == len(observer.completed) == session.command_count == 1


def test_session_clips_timeouts_to_the_deadline(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path)
    session = make_session(sandbox, deadline=time.monotonic() + 2)
    session.run(["sleep", "100"], timeout_seconds=100)
    assert sandbox.requests[0].timeout_seconds <= 2


def test_session_refuses_commands_after_deadline(tmp_path: Path) -> None:
    session = make_session(make_sandbox(tmp_path), deadline=time.monotonic() - 1)
    with pytest.raises(AgentTimeoutError, match="time budget exhausted"):
        session.run(["true"])


def test_closed_session_rejects_every_operation(tmp_path: Path) -> None:
    session = make_session(make_sandbox(tmp_path))
    session.close()
    with pytest.raises(SessionClosedError):
        session.run(["true"])
    with pytest.raises(SessionClosedError):
        session.read_file("x")


# ------------------------------------------------------------------------ graders


def grading_context(
    sandbox: FakeSandbox, artifacts: dict[str, bytes], **kwargs: object
) -> GradingContext:
    return GradingContext(
        run_id="run_x",
        task_id="t",
        session=make_session(sandbox, timeout_error=GraderTimeoutError, **kwargs),
        workspace=WORKSPACE,
        assets_dir=None,
        save_artifact=artifacts.__setitem__,
    )


def test_pytest_grader_scores_from_junit_report(tmp_path: Path) -> None:
    artifacts: dict[str, bytes] = {}
    sandbox = make_sandbox(tmp_path, pytest_handler(tests=4, failures=1))
    grade = PytestGrader(name="hidden", paths=("/grader/tests",)).grade(
        grading_context(sandbox, artifacts)
    )

    assert not grade.passed
    assert grade.score == 0.75
    assert (grade.tests_passed, grade.tests_failed) == (3, 1)
    assert set(artifacts) == {
        "grader/hidden/stdout.log",
        "grader/hidden/stderr.log",
        "grader/hidden/junit.xml",
    }
    argv = sandbox.requests[0].argv
    assert argv[:4] == ("python", "-m", "pytest", "/grader/tests")
    assert "no:cacheprovider" in argv


def test_pytest_grader_passes_only_when_everything_passes(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path, pytest_handler(tests=3))
    grade = PytestGrader(name="t", paths=()).grade(grading_context(sandbox, {}))
    assert grade.passed and grade.score == 1.0


def test_collection_errors_are_a_failed_grade_not_a_grader_error(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path, pytest_handler(tests=0, errors=1, exit_code=2))
    grade = PytestGrader(name="t", paths=()).grade(grading_context(sandbox, {}))
    assert not grade.passed
    assert grade.tests_errored == 1


@pytest.mark.parametrize("exit_code", [3, 4, 5])
def test_pytest_that_cannot_judge_is_a_grader_error(tmp_path: Path, exit_code: int) -> None:
    sandbox = make_sandbox(tmp_path, pytest_handler(tests=1, exit_code=exit_code))
    with pytest.raises(GraderError):
        PytestGrader(name="t", paths=()).grade(grading_context(sandbox, {}))


def test_missing_report_is_a_grader_error(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path, lambda request, _: make_execution(request, exit_code=0))
    with pytest.raises(GraderError, match="no JUnit report"):
        PytestGrader(name="t", paths=()).grade(grading_context(sandbox, {}))


def test_grader_timeout_is_classified(tmp_path: Path) -> None:
    sandbox = make_sandbox(
        tmp_path, lambda request, _: make_execution(request, exit_code=124, timed_out=True)
    )
    with pytest.raises(GraderTimeoutError):
        CommandGrader(name="c", command=("make", "check")).grade(grading_context(sandbox, {}))


def test_command_grader_uses_success_exit_codes(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path, lambda request, _: make_execution(request, exit_code=3))
    context = grading_context(sandbox, {})
    assert not CommandGrader(name="c", command=("x",)).grade(context).passed
    assert CommandGrader(name="c", command=("x",), success_exit_codes=(0, 3)).grade(context).passed


class FixedGrader:
    def __init__(self, name: str, passed: bool, score: float) -> None:
        self.name, self.grader_type = name, "fixed"
        self._result = GradeResult(grader=name, grader_type="fixed", passed=passed, score=score)

    def grade(self, context: GradingContext) -> GradeResult:
        return self._result


@pytest.mark.parametrize(
    ("aggregation", "threshold", "expected_pass"),
    [("all", 1.0, False), ("weighted", 0.7, True), ("weighted", 0.8, False)],
)
def test_composite_aggregation(
    tmp_path: Path, aggregation: str, threshold: float, expected_pass: bool
) -> None:
    composite = CompositeGrader(
        components=(
            WeightedGrader(FixedGrader("a", True, 1.0), weight=3),
            WeightedGrader(FixedGrader("b", False, 0.0), weight=1),
        ),
        aggregation=aggregation,
        pass_threshold=threshold,
    )
    grade = composite.grade(grading_context(make_sandbox(tmp_path), {}))
    assert grade.score == 0.75
    assert grade.passed is expected_pass
    assert [component.grader for component in grade.components] == ["a", "b"]


def test_build_grader_returns_single_grader_or_composite() -> None:
    single = GradingSpec.model_validate({"graders": [{"type": "command", "command": ["true"]}]})
    assert isinstance(build_grader(single), CommandGrader)
    double = GradingSpec.model_validate(
        {"graders": [{"type": "pytest"}, {"type": "command", "command": ["true"]}]}
    )
    assert isinstance(build_grader(double), CompositeGrader)


def test_junit_parsing_sums_suites_and_rejects_garbage() -> None:
    document = (
        b'<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1"/>'
        b'<testsuite tests="2" failures="0" errors="1" skipped="0"/></testsuites>'
    )
    counts = parse_junit_counts(document)
    assert (counts.total, counts.passed, counts.executed) == (5, 2, 4)
    with pytest.raises(GraderError):
        parse_junit_counts(b"<not-xml")
    with pytest.raises(GraderError):
        parse_junit_counts(b"<html/>")


# ------------------------------------------------------------------------- agents


def agent_context(
    sandbox: FakeSandbox, artifacts: dict[str, bytes], budget: float = 60
) -> AgentContext:
    return AgentContext(
        run_id="run_x",
        task=AgentTask(task_id="t", name="T", instructions="Fix it.", workspace=WORKSPACE),
        session=make_session(sandbox, deadline=time.monotonic() + budget),
        time_budget_seconds=budget,
        save_artifact=artifacts.__setitem__,
    )


def test_command_agent_uploads_files_instructions_and_environment(tmp_path: Path) -> None:
    agent_files = tmp_path / "agent-files"
    agent_files.mkdir()
    (agent_files / "solve.py").write_text("print('solving')")
    sandbox = make_sandbox(tmp_path, lambda request, _: make_execution(request, stdout="done"))
    artifacts: dict[str, bytes] = {}

    agent = CommandAgent(
        label="solver",
        command=("python", "/agent/solve.py"),
        files=agent_files,
        secrets={"API_KEY": "s3cret"},
    )
    result = agent.run(agent_context(sandbox, artifacts))

    assert result.outcome is AgentOutcome.COMPLETED
    assert agent.name == "command:solver"
    assert sandbox.host_path("/agent/solve.py").read_text() == "print('solving')"
    assert sandbox.host_path(INSTRUCTIONS_PATH).read_text() == "Fix it."
    request = sandbox.requests[0]
    assert request.cwd == "/workspace"
    assert request.env["AGENTENV_TASK_ID"] == "t"
    assert request.env["API_KEY"] == "s3cret"
    assert artifacts["agent/stdout.log"] == b"done"


def test_command_agent_nonzero_exit_is_a_failed_outcome_not_an_error(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path, lambda request, _: make_execution(request, exit_code=2))
    result = CommandAgent(label="x", command=("false",)).run(agent_context(sandbox, {}))
    assert result.outcome is AgentOutcome.FAILED
    assert result.exit_code == 2


def test_command_agent_timeout_raises(tmp_path: Path) -> None:
    sandbox = make_sandbox(
        tmp_path, lambda request, _: make_execution(request, exit_code=124, timed_out=True)
    )
    with pytest.raises(AgentTimeoutError):
        CommandAgent(label="x", command=("sleep", "9")).run(agent_context(sandbox, {}))


def test_manual_agent_shows_attach_hint_and_waits(tmp_path: Path) -> None:
    output: list[str] = []
    prompts: list[str] = []

    def confirm(text: str) -> str:
        prompts.append(text)
        return ""

    agent = ManualAgent(write=output.append, wait_for_confirmation=confirm)
    result = agent.run(agent_context(make_sandbox(tmp_path), {}))
    assert result.outcome is AgentOutcome.COMPLETED
    assert "fake-attach box" in output[0]
    assert len(prompts) == 1


def _environment(environ: dict[str, str] | None = None) -> AgentEnvironment:
    return AgentEnvironment(environ=environ or {}, write=lambda _: None, prompt=lambda _: "")


def test_resolve_builtin_and_inline_agents() -> None:
    assert isinstance(resolve_agent("noop", _environment()), NoopAgent)
    assert isinstance(resolve_agent("manual", _environment()), ManualAgent)
    assert command_agent_from_string("python solve.py --fast").command == (
        "python",
        "solve.py",
        "--fast",
    )


def test_command_agent_spec_resolves_files_and_secrets(tmp_path: Path) -> None:
    (tmp_path / "code").mkdir()
    spec = tmp_path / "agent.yaml"
    spec.write_text(
        'type: command\nname: solver\ncommand: ["run"]\nfiles: code\nsecrets: [API_KEY]\n'
    )
    agent = resolve_agent(str(spec), _environment({"API_KEY": "value"}))
    assert isinstance(agent, CommandAgent)
    assert agent.files == (tmp_path / "code").resolve()
    assert agent.secrets == {"API_KEY": "value"}


def test_agent_spec_errors_are_actionable(tmp_path: Path) -> None:
    spec = tmp_path / "agent.yaml"
    spec.write_text(
        'type: command\nname: solver\ncommand: ["run"]\nfiles: absent\nsecrets: [MISSING]\n'
    )
    with pytest.raises(TaskValidationError) as caught:
        resolve_agent(str(spec), _environment())
    assert {issue.field for issue in caught.value.issues} == {"files", "secrets"}

    # Schema-level problems are reported (with clean field names) before filesystem checks.
    spec.write_text('type: command\nname: solver\ncommand: ["run"]\nfiles: ../x\n')
    with pytest.raises(TaskValidationError) as caught:
        resolve_agent(str(spec), _environment())
    assert [issue.field for issue in caught.value.issues] == ["files"]

    spec.write_text("type: telepathy\nname: x\n")
    with pytest.raises(TaskValidationError) as caught:
        resolve_agent(str(spec), _environment())
    (issue,) = caught.value.issues
    assert (issue.field, issue.received) == ("type", "'telepathy'")


def test_python_agent_spec_imports_custom_adapter(tmp_path: Path) -> None:
    spec = tmp_path / "agent.yaml"
    spec.write_text(
        "type: python\nname: custom\nadapter: tests.unit.test_adapters:build_custom\n"
        "options: {label: hello}\n"
    )
    agent = resolve_agent(str(spec), _environment())
    assert agent.name == "custom:hello"

    spec.write_text(
        "type: python\nname: custom\nadapter: tests.unit.test_adapters:not_an_adapter\n"
    )
    with pytest.raises(TaskValidationError, match="did not return an AgentAdapter"):
        resolve_agent(str(spec), _environment())


class CustomAgent:
    def __init__(self, label: str) -> None:
        self.label = label

    @property
    def name(self) -> str:
        return f"custom:{self.label}"

    def run(self, context: AgentContext) -> object:  # pragma: no cover - not executed
        raise NotImplementedError


def build_custom(label: str) -> CustomAgent:
    return CustomAgent(label)


def not_an_adapter() -> object:
    return object()
