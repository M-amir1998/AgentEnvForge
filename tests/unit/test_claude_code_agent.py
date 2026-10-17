"""The example Claude Code agent against a fake CLI (no network, no Docker, no tokens)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from claude_code_agent import (
    ALLOWED_TOOLS,
    ClaudeCodeAgent,
    SandboxToolServer,
    _clean_environment,
)

from agentenvforge.agents.base import AgentContext, AgentTask
from agentenvforge.domain.failures import AgentError, AgentTimeoutError, AgentUnavailableError
from agentenvforge.domain.results import AgentOutcome
from agentenvforge.sandbox.session import SandboxSession
from agentenvforge.workspace.archive import Ownership
from tests.fakes import FakeSandbox, make_execution

FAKE_CLI = Path(__file__).with_name("fake_claude_cli.py")


@pytest.fixture
def sandbox(tmp_path: Path) -> FakeSandbox:
    root = tmp_path / "box"
    (root / "tmp").mkdir(parents=True)
    (root / "workspace").mkdir()
    (root / "workspace" / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    def handler(request: Any, box: FakeSandbox) -> Any:
        passed = "a + b" in box.host_path("/workspace/calc.py").read_text()
        return make_execution(
            request, exit_code=0 if passed else 1, stdout="1 passed\n" if passed else "1 failed\n"
        )

    return FakeSandbox(root, "box", handler)


def make_context(box: FakeSandbox, artifacts: dict[str, bytes], budget: float = 60) -> AgentContext:
    session = SandboxSession(
        box,
        workspace=PurePosixPath("/workspace"),
        owner=Ownership(1000, 1000),
        default_command_timeout_seconds=30,
        deadline=time.monotonic() + budget,
    )
    return AgentContext(
        run_id="run_x",
        task=AgentTask("t", "T", "Fix add().", PurePosixPath("/workspace")),
        session=session,
        time_budget_seconds=budget,
        save_artifact=artifacts.__setitem__,
    )


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ClaudeCodeAgent:
    built = ClaudeCodeAgent(model="fake-model")
    monkeypatch.setattr(built, "_cli_argv", lambda: [sys.executable, str(FAKE_CLI)])
    return built


def test_agent_fixes_the_bug_through_the_sandbox_tools(
    agent: ClaudeCodeAgent, sandbox: FakeSandbox
) -> None:
    artifacts: dict[str, bytes] = {}
    result = agent.run(make_context(sandbox, artifacts))

    assert result.outcome is AgentOutcome.COMPLETED, result.summary
    assert sandbox.host_path("/workspace/calc.py").read_text().endswith("return a + b\n")
    assert sandbox.requests[-1].argv == ("sh", "-c", "python -m pytest -q")
    assert "replaced 1 occurrence" in result.summary and "1 passed" in result.summary
    meta = result.metadata
    assert json.loads(str(meta["tools_used"])) == {"read_file": 1, "replace_in_file": 1, "run": 1}
    assert (meta["prompt_tokens"], meta["completion_tokens"]) == (400, 42)
    assert meta["cost_usd_list_price"] == 0.0125 and meta["models_used"] == "fake-model"
    lines = artifacts["agent/transcript.jsonl"].decode().splitlines()
    init, debug = json.loads(lines[0]), json.loads(lines[1])
    assert init == {  # host details (cwd, memory paths) are not kept
        "type": "system",
        "subtype": "init",
        "model": "fake-model",
        "tools": ["run", "read_file", "replace_in_file", "write_file"],
    }
    assert debug["protocol"] == "2025-06-18" and debug["prompt"] == "Fix add()."


def test_cli_is_isolated_and_limited_to_the_sandbox_tools(
    agent: ClaudeCodeAgent, sandbox: FakeSandbox
) -> None:
    artifacts: dict[str, bytes] = {}
    agent.run(make_context(sandbox, artifacts))
    argv = json.loads(artifacts["agent/transcript.jsonl"].decode().splitlines()[1])["argv"]
    assert argv[argv.index("--tools") + 1] == ""  # no built-in tool (no host file access)
    assert argv[argv.index("--allowedTools") + 1] == ",".join(ALLOWED_TOOLS)
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert {"--strict-mcp-config", "--no-session-persistence", "-p"} <= set(argv)
    assert "--safe-mode" not in argv  # it would also disable the sandbox MCP server


def test_cli_is_stopped_when_the_time_budget_ends(
    agent: ClaudeCodeAgent, sandbox: FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    started = time.monotonic()
    with pytest.raises(AgentTimeoutError, match="time budget"):
        agent.run(make_context(sandbox, {}, budget=3))
    assert time.monotonic() - started < 20  # killed, not waited for (it sleeps 600 s)


def test_usage_guard_stops_the_run_and_account_usage_stays_out_of_the_transcript(
    sandbox: FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded = ClaudeCodeAgent(model="fake-model", usage_guard=0.98)
    monkeypatch.setattr(guarded, "_cli_argv", lambda: [sys.executable, str(FAKE_CLI)])
    artifacts: dict[str, bytes] = {}
    guarded.run(make_context(sandbox, artifacts))  # usage 50%: below the guard
    assert "rate_limit" not in artifacts["agent/transcript.jsonl"].decode()

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "usage-high")  # 99%, then the CLI would hang
    started = time.monotonic()
    with pytest.raises(AgentUnavailableError, match="usage allowance"):
        guarded.run(make_context(sandbox, {}))
    assert time.monotonic() - started < 20


def test_unavailable_model_service_is_not_an_agent_failure(
    agent: ClaudeCodeAgent, sandbox: FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "unavailable")
    with pytest.raises(AgentUnavailableError, match="529"):
        agent.run(make_context(sandbox, {}))


def test_cli_crash_without_a_result_is_an_agent_error(
    agent: ClaudeCodeAgent, sandbox: FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    with pytest.raises(AgentError, match="boom"):
        agent.run(make_context(sandbox, {}))


def test_preflight_records_provenance_but_no_account_details(
    agent: ClaudeCodeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    provenance = agent.preflight()
    assert provenance["claude_code_version"] == "9.9.9"
    assert provenance["auth_method"] == "claude.ai" and provenance["model"] == "fake-model"
    assert "x@y.z" not in json.dumps(provenance)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "logged-out")
    with pytest.raises(AgentError, match="not logged in"):
        agent.preflight()


def test_tool_server_protocol(sandbox: FakeSandbox) -> None:
    with SandboxToolServer(make_context(sandbox, {}), command_timeout_seconds=30) as server:
        initialized = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999"},
            }
        )
        assert initialized is not None
        assert initialized["result"]["protocolVersion"] == "2025-06-18"
        assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        unknown = server.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        assert unknown is not None and unknown["error"]["code"] == -32601

        text, is_error = server.call("replace_in_file", {"path": "calc.py", "old": "b", "new": "c"})
        assert is_error and "matches 2 times" in text
        text, is_error = server.call("read_file", {"path": "missing.py"})
        assert is_error and "does not exist" in text
        text, is_error = server.call("rm_rf", {})
        assert is_error and server.log.errors == 3


def test_parent_session_variables_are_removed_but_configuration_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    environment = _clean_environment()
    assert "CLAUDECODE" not in environment and "CLAUDE_CODE_SESSION_ID" not in environment
    assert environment["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert environment["ANTHROPIC_API_KEY"] == "test-key"
