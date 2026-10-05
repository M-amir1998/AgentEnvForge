"""The example Ollama agent, with a scripted fake model (no Ollama or Docker needed)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from ollama_agent import OllamaAgent, extract_tool_calls

from agentenvforge.agents.base import AgentContext, AgentTask
from agentenvforge.agents.spec import AgentEnvironment, resolve_agent
from agentenvforge.domain.failures import AgentError
from agentenvforge.domain.results import AgentOutcome
from agentenvforge.sandbox.session import SandboxSession
from agentenvforge.workspace.archive import Ownership
from tests.conftest import REPOSITORY_ROOT
from tests.fakes import FakeSandbox, make_execution

SPEC = REPOSITORY_ROOT / "examples" / "agents" / "ollama" / "qwen2.5-coder-7b.yaml"


def tool_message(name: str, **arguments: object) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
    }


# -------------------------------------------------------------- tool-call parsing


def test_native_tool_calls_are_extracted() -> None:
    (call,) = extract_tool_calls(tool_message("run", command="ls"))
    assert (call.name, call.arguments) == ("run", {"command": "ls"})


@pytest.mark.parametrize(
    "content",
    [
        '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>',
        'Let me look.\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```',
        '{"name": "read_file", "arguments": "{\\"path\\": \\"a.py\\"}"}',
        '{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}',
    ],
)
def test_tool_calls_written_as_text_are_recovered(content: str) -> None:
    (call,) = extract_tool_calls({"role": "assistant", "content": content})
    assert (call.name, call.arguments) == ("read_file", {"path": "a.py"})


@pytest.mark.parametrize(
    "content", ["I think the bug is in line 3.", '{"name": "rm_rf"}', "{not json"]
)
def test_prose_and_unknown_tools_are_not_tool_calls(content: str) -> None:
    assert extract_tool_calls({"role": "assistant", "content": content}) == []


# --------------------------------------------------------------------- the loop


class ScriptedModel:
    """Replaces OllamaAgent._chat with a fixed sequence of replies."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies: Iterator[dict[str, Any]] = iter(replies)
        self.requests: list[list[dict[str, Any]]] = []

    def __call__(
        self, messages: list[dict[str, Any]], timeout: float, stats: Any
    ) -> dict[str, Any]:
        self.requests.append(list(messages))
        stats.prompt_tokens += 10
        return next(self.replies)


@pytest.fixture
def workspace_sandbox(tmp_path: Path) -> FakeSandbox:
    root = tmp_path / "box"
    (root / "tmp").mkdir(parents=True)
    (root / "workspace").mkdir()
    (root / "workspace" / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    def handler(request: Any, sandbox: FakeSandbox) -> Any:
        source = sandbox.host_path("/workspace/calc.py").read_text()
        passed = "a + b" in source
        return make_execution(
            request, exit_code=0 if passed else 1, stdout="1 passed" if passed else "1 failed"
        )

    return FakeSandbox(root, "box", handler)


def make_context(sandbox: FakeSandbox, artifacts: dict[str, bytes]) -> AgentContext:
    session = SandboxSession(
        sandbox,
        workspace=PurePosixPath("/workspace"),
        owner=Ownership(1000, 1000),
        default_command_timeout_seconds=30,
        deadline=time.monotonic() + 60,
    )
    return AgentContext(
        run_id="run_x",
        task=AgentTask("t", "T", "Fix add().", PurePosixPath("/workspace")),
        session=session,
        time_budget_seconds=60,
        save_artifact=artifacts.__setitem__,
    )


def test_agent_explores_edits_tests_and_finishes(
    workspace_sandbox: FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel(
        [
            tool_message("read_file", path="calc.py"),
            tool_message("replace_in_file", path="calc.py", old="a - b", new="a + b"),
            tool_message("run", command="python -m pytest -q"),
            tool_message("finish", summary="fixed the operator"),
        ]
    )
    agent = OllamaAgent(model="fake")
    monkeypatch.setattr(agent, "_chat", model)
    artifacts: dict[str, bytes] = {}

    result = agent.run(make_context(workspace_sandbox, artifacts))

    assert result.outcome is AgentOutcome.COMPLETED
    assert result.summary == "fixed the operator"
    assert (
        workspace_sandbox.host_path("/workspace/calc.py").read_text()
        == "def add(a, b):\n    return a + b\n"
    )
    assert workspace_sandbox.requests[0].argv == ("sh", "-c", "python -m pytest -q")
    assert result.metadata["steps"] == 4
    assert json.loads(str(result.metadata["tools_used"])) == {
        "finish": 1,
        "read_file": 1,
        "replace_in_file": 1,
        "run": 1,
    }
    # The model saw the file content and the test output as tool results.
    tool_results = [m["content"] for m in model.requests[-1] if m["role"] == "tool"]
    assert "return a - b" in tool_results[0]
    assert "1 passed" in tool_results[2]
    transcript = artifacts["agent/transcript.jsonl"].decode().splitlines()
    assert json.loads(transcript[0])["role"] == "system"


def test_bad_edit_gets_actionable_feedback(
    workspace_sandbox: FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel(
        [
            tool_message("replace_in_file", path="calc.py", old="return a-b", new="return a + b"),
            tool_message("read_file", path="missing.py"),
            tool_message("finish", summary="gave up"),
        ]
    )
    agent = OllamaAgent(model="fake")
    monkeypatch.setattr(agent, "_chat", model)
    agent.run(make_context(workspace_sandbox, {}))
    feedback = [m["content"] for m in model.requests[-1] if m["role"] == "tool"]
    assert "matches 0 times" in feedback[0]
    assert "does not exist" in feedback[1]


def test_prose_replies_are_nudged_then_abandoned(
    workspace_sandbox: FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    prose = {"role": "assistant", "content": "The bug is the minus sign."}
    agent = OllamaAgent(model="fake")
    monkeypatch.setattr(agent, "_chat", ScriptedModel([prose] * 10))
    result = agent.run(make_context(workspace_sandbox, {}))
    assert result.outcome is AgentOutcome.FAILED
    assert "without calling a tool" in result.summary
    assert result.metadata["invalid_replies"] == 4


def test_step_limit_ends_the_run(
    workspace_sandbox: FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = OllamaAgent(model="fake", max_steps=3)
    monkeypatch.setattr(agent, "_chat", ScriptedModel([tool_message("run", command="ls")] * 5))
    result = agent.run(make_context(workspace_sandbox, {}))
    assert result.outcome is AgentOutcome.FAILED
    assert result.summary == "stopped: reached max_steps=3"


def test_unreachable_ollama_is_an_agent_error(workspace_sandbox: FakeSandbox) -> None:
    agent = OllamaAgent(model="fake", base_url="http://127.0.0.1:9")
    with pytest.raises(AgentError, match="cannot reach Ollama"):
        agent.run(make_context(workspace_sandbox, {}))


def test_spec_file_loads_the_adapter_from_its_own_directory() -> None:
    environment = AgentEnvironment(environ={}, write=lambda _: None, prompt=lambda _: "")
    agent = resolve_agent(str(SPEC), environment)
    assert agent.name == "ollama:qwen2.5-coder:7b"
