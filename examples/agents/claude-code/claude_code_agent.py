"""Claude Code as an AgentEnvForge agent, acting only through the sandbox.

The `claude` CLI runs on the host in non-interactive mode (``claude -p``) with every
built-in tool disabled. Its only tools come from a small MCP server that this adapter
hosts for the duration of one run, bound to 127.0.0.1 under an unguessable URL::

    claude -p (model in the cloud) ──MCP over HTTP──► SandboxToolServer ──► context.session
                                                      run · read_file · write_file ·
                                                      replace_in_file
                                                                         │
                                                       offline Docker sandbox (/workspace)

So the sandbox keeps networking disabled, Claude Code cannot touch the host's files, and
every action is an observed, deadline-bound sandbox command recorded like any other run.
The adapter kills the CLI when the run's time budget ends or the run is cancelled, so an
abandoned agent never keeps spending tokens.

Authentication is whatever the CLI already uses (a claude.ai login or ANTHROPIC_API_KEY).
Only the Python standard library is used: AgentEnvForge's core stays provider-free.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agentenvforge.agents.base import AgentContext
from agentenvforge.domain.failures import (
    AgentEnvForgeError,
    AgentError,
    AgentTimeoutError,
    AgentUnavailableError,
    SandboxPathNotFoundError,
)
from agentenvforge.domain.results import AgentOutcome, AgentResult

SYSTEM_PROMPT = """\
You are a software engineer fixing a problem in a repository. You work only through the
tools, which act inside an isolated Linux sandbox without internet access. The repository
is at {workspace}, which is also the working directory of `run`.

Find the root cause of the problem described by the user and fix it in the source code.
Use `run` for shell commands (for example ls, grep, or `python -m pytest -q` to run the
tests), `read_file` to read a file, and `replace_in_file` or `write_file` to change one.
Do not edit the tests. Check your fix by running the tests. When you are done, reply with
a short summary of what you changed. Nobody can answer questions during the task.
"""

SERVER_NAME = "sandbox"
TOOLS: list[dict[str, Any]] = [
    {
        "name": "run",
        "description": "Run a shell command (sh -c) in the repository and return its exit "
        "code and output (stdout and stderr; long output is truncated from the start).",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "e.g. ls -R"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Return the content of a file (path relative to the repository).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "replace_in_file",
        "description": "Replace one exact occurrence of `old` with `new` in a file. Fails "
        "unless `old` occurs exactly once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string", "description": "exact text currently in the file"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "write_file",
        "description": "Replace the whole content of a file (creating it if needed).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
]
ALLOWED_TOOLS = tuple(f"mcp__{SERVER_NAME}__{tool['name']}" for tool in TOOLS)
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# Fields of the CLI's "init" message kept in the transcript: what the run used. Everything
# else (working directory, memory and plugin paths, ...) describes the host, not the run.
_INIT_FIELDS = (
    "type",
    "subtype",
    "model",
    "tools",
    "mcp_servers",
    "permissionMode",
    "claude_code_version",
    "apiKeySource",
)
_MAX_RUN_OUTPUT_CHARS = 10_000
_MAX_READ_CHARS = 40_000
_POLL_SECONDS = 0.25
# Variables that mark a *parent* Claude Code session (set when AgentEnvForge itself is
# started from Claude Code). The agent must be an independent session, so these are removed;
# configuration such as CLAUDE_CODE_USE_BEDROCK or ANTHROPIC_API_KEY is passed on.
_PARENT_SESSION_VARIABLES = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_PID",
        "CLAUDE_EFFORT",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_EMIT_STARTUP_TIMING",
        "CLAUDE_CODE_ENABLE_TASKS",
        "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
        "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT",
        "MCP_CONNECTION_NONBLOCKING",
    }
)
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    _CREATION_FLAGS = 0
# Messages of the CLI that say nothing about the agent: the service or the login failed.
_UNAVAILABLE_MARKERS = (
    "overloaded",
    "rate limit",
    "rate_limit",
    "usage limit",
    "api error: 5",
    "529",
    "please run /login",
    "invalid api key",
    "not logged in",
    "authentication",
    "connection error",
    "econnrefused",
    "fetch failed",
)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} characters omitted ...]\n" + text[-limit:]


# ---------------------------------------------------------------------- MCP server


@dataclass
class _ToolLog:
    calls: dict[str, int] = field(default_factory=dict)
    errors: int = 0


class SandboxToolServer:
    """A minimal MCP server (streamable HTTP transport, JSON responses) whose tools act on
    one run's sandbox session. One sandbox action at a time, like a single shell."""

    def __init__(self, context: AgentContext, *, command_timeout_seconds: int) -> None:
        self._context = context
        self._command_timeout = command_timeout_seconds
        self._lock = threading.Lock()
        self.log = _ToolLog()
        self._path = f"/mcp/{secrets.token_urlsafe(24)}"
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="sandbox-mcp", daemon=True
        )

    @property
    def url(self) -> str:
        """Where the MCP client connects (contains the secret path)."""
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}{self._path}"

    def __enter__(self) -> SandboxToolServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    # ------------------------------------------------------------------ protocol

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC message (``None`` for notifications)."""
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:  # a notification (e.g. notifications/initialized)
            return None
        params = message.get("params") or {}
        if method == "initialize":
            requested = str(params.get("protocolVersion", ""))
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            result: dict[str, Any] = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agentenvforge-sandbox", "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            text, is_error = self.call(str(params.get("name")), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Execute one tool; errors are returned to the model, which can correct itself."""
        tools = {
            "run": self._run,
            "read_file": self._read,
            "write_file": self._write,
            "replace_in_file": self._replace,
        }
        tool = tools.get(name)
        with self._lock:
            self.log.calls[name] = self.log.calls.get(name, 0) + 1
            if tool is None:
                self.log.errors += 1
                return f"error: unknown tool {name}", True
            try:
                return tool(arguments), False
            except SandboxPathNotFoundError:
                self.log.errors += 1
                return f"error: {arguments.get('path')} does not exist", True
            except ValueError as error:
                self.log.errors += 1
                return f"error: {error}", True
            except AgentEnvForgeError as error:  # e.g. the run's time is up or it was cancelled
                self.log.errors += 1
                return f"error: {error.message}", True

    def _run(self, arguments: dict[str, Any]) -> str:
        command = str(arguments.get("command", "")).strip()
        if not command:
            raise ValueError("`command` is required")
        result = self._context.session.run(
            ["sh", "-c", command], timeout_seconds=self._command_timeout
        )
        status = "timed out" if result.timed_out else f"exit code {result.exit_code}"
        return _truncate(f"[{status}]\n{result.stdout}{result.stderr}", _MAX_RUN_OUTPUT_CHARS)

    def _read(self, arguments: dict[str, Any]) -> str:
        content = self._context.session.read_file(str(arguments.get("path", "")))
        return _truncate(content.decode("utf-8", "replace"), _MAX_READ_CHARS)

    def _write(self, arguments: dict[str, Any]) -> str:
        content = str(arguments.get("content", "")).encode("utf-8")
        self._context.session.write_file(str(arguments.get("path", "")), content)
        return "file written"

    def _replace(self, arguments: dict[str, Any]) -> str:
        path = str(arguments.get("path", ""))
        old, new = str(arguments.get("old", "")), str(arguments.get("new", ""))
        content = self._context.session.read_file(path).decode("utf-8", "replace")
        occurrences = content.count(old) if old else 0
        if occurrences != 1:
            raise ValueError(
                f"`old` must match exactly once, but it matches {occurrences} times; read "
                "the file again and copy the text exactly, including indentation"
            )
        self._context.session.write_file(path, content.replace(old, new).encode("utf-8"))
        return "replaced 1 occurrence"

    # --------------------------------------------------------------------- HTTP

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return  # requests are not logged: tool calls become sandbox events

            def _reject(self, status: HTTPStatus) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                # No server-initiated stream (allowed by the transport specification).
                self._reject(HTTPStatus.METHOD_NOT_ALLOWED)

            def do_DELETE(self) -> None:
                self._reject(HTTPStatus.METHOD_NOT_ALLOWED)

            def do_POST(self) -> None:
                if self.path != server._path:
                    self._reject(HTTPStatus.NOT_FOUND)
                    return
                origin = self.headers.get("Origin")
                if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                    self._reject(HTTPStatus.FORBIDDEN)  # DNS-rebinding protection
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"null")
                except ValueError:
                    self._reject(HTTPStatus.BAD_REQUEST)
                    return
                messages = payload if isinstance(payload, list) else [payload]
                replies = [
                    reply
                    for message in messages
                    if isinstance(message, dict) and (reply := server.handle(message))
                ]
                if not replies:
                    self._reject(HTTPStatus.ACCEPTED)  # notifications only
                    return
                body = json.dumps(replies if isinstance(payload, list) else replies[0]).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


# ------------------------------------------------------------------------ the CLI


def find_claude_cli(explicit: str | None = None) -> str | None:
    """The `claude` executable: an explicit path, $AGENTENV_CLAUDE_CLI, the PATH, or the
    copy bundled with the VS Code extension (newest version first)."""
    for candidate in (explicit, os.environ.get("AGENTENV_CLAUDE_CLI")):
        if candidate:
            return candidate if Path(candidate).is_file() else shutil.which(candidate)
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    name = "claude.exe" if sys.platform == "win32" else "claude"
    extensions = Path.home() / ".vscode" / "extensions"
    bundled = list(extensions.glob(f"anthropic.claude-code-*/resources/native-binary/{name}"))
    if not bundled:
        return None
    return str(max(bundled, key=lambda path: path.stat().st_mtime))  # the newest install


def _clean_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _PARENT_SESSION_VARIABLES
    }


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, 9)  # the CLI was started in its own session
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)


@dataclass
class _Stream:
    """Lines read from the CLI's stdout (stream-json) and the tail of its stderr.

    Rate-limit events are kept out of ``lines`` (and so out of the saved transcript): they
    describe the user's account (usage level, reset time), not the run. Only the highest
    reported usage fraction is remembered, for the usage guard.
    """

    lines: list[str] = field(default_factory=list)
    stderr: str = ""
    usage_fraction: float | None = None

    def add(self, line: str) -> None:
        if '"init"' in line:
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            if isinstance(message, dict) and message.get("subtype") == "init":
                line = json.dumps({key: message[key] for key in _INIT_FIELDS if key in message})
        if '"rate_limit_event"' in line:
            try:
                info = json.loads(line).get("rate_limit_info") or {}
            except (ValueError, AttributeError):
                return
            utilization = info.get("utilization")
            if not isinstance(utilization, int | float):
                return
            fraction = float(utilization)
            self.usage_fraction = max(fraction, self.usage_fraction or 0.0)
            return
        self.lines.append(line)

    def result(self) -> dict[str, Any] | None:
        for line in reversed(self.lines):
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("type") == "result":
                return message
        return None


def _drain(stream: Any, sink: _Stream, *, stderr: bool) -> None:
    for raw in iter(stream.readline, b""):
        text = raw.decode("utf-8", "replace")
        if stderr:
            sink.stderr = (sink.stderr + text)[-4000:]
        elif text.strip():
            sink.add(text.rstrip("\n"))


# ---------------------------------------------------------------------- the agent


class ClaudeCodeAgent:
    """Runs one Claude Code session per run, with sandbox tools only."""

    def __init__(
        self,
        *,
        model: str,
        effort: str | None = None,
        claude_path: str | None = None,
        max_budget_usd: float | None = None,
        command_timeout_seconds: int = 120,
        isolated: bool = True,
        usage_guard: float | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.claude_path = claude_path
        self.max_budget_usd = max_budget_usd
        self.command_timeout_seconds = command_timeout_seconds
        self.isolated = isolated
        self.usage_guard = usage_guard
        """Stop when the CLI reports this fraction of the account's usage allowance (e.g.
        0.98), so a long benchmark cannot exhaust a subscription. ``None``: no guard."""

    @property
    def name(self) -> str:
        """``claude-code:<model>``."""
        return f"claude-code:{self.model}"

    def _cli(self) -> str:
        cli = find_claude_cli(self.claude_path)
        if cli is None:
            raise AgentUnavailableError(
                "the Claude Code CLI (`claude`) was not found; install it, or set "
                "AGENTENV_CLAUDE_CLI or the agent option `claude_path` to its location"
            )
        return cli

    def _cli_argv(self) -> list[str]:
        """The command that starts the CLI (tests replace it with a fake)."""
        return [self._cli()]

    def preflight(self) -> dict[str, str | int | float | bool | None]:
        """Check the CLI and its login without using any tokens; return provenance."""
        argv = self._cli_argv()
        env = _clean_environment()
        try:
            version = subprocess.run(
                [*argv, "--version"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
                check=False,
            ).stdout.strip()
            status = subprocess.run(
                [*argv, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AgentUnavailableError(f"the Claude Code CLI could not be run: {error}") from error
        try:
            auth = json.loads(status.stdout)
        except ValueError:
            auth = {}
        if not auth.get("loggedIn"):
            raise AgentError(
                "Claude Code is not logged in: run `claude auth login` (or set ANTHROPIC_API_KEY)"
            )
        # Deliberately not recorded: e-mail address, organisation and account IDs.
        return {
            "claude_code_version": version.split(" ")[0] or None,
            "model": self.model,
            "auth_method": str(auth.get("authMethod")) if auth.get("authMethod") else None,
            "effort": self.effort or "default",
            "isolated": self.isolated,
            "usage_guard": self.usage_guard,
            "max_budget_usd": self.max_budget_usd,
        }

    def _argv(self, mcp_config: Path, workspace: str) -> list[str]:
        argv = [
            *self._cli_argv(),
            "-p",
            "--model",
            self.model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--tools",
            "",
            "--mcp-config",
            str(mcp_config),
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(ALLOWED_TOOLS),
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--system-prompt",
            SYSTEM_PROMPT.format(workspace=workspace),
        ]
        if self.isolated:
            # Only project settings, from an empty directory: none. The user's own plugins,
            # hooks, skills and permissions do not apply, so results do not depend on them.
            # (--safe-mode would also disable the sandbox MCP server.)
            argv += ["--setting-sources", "project"]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.max_budget_usd is not None:
            argv += ["--max-budget-usd", str(self.max_budget_usd)]
        return argv

    def run(self, context: AgentContext) -> AgentResult:
        """One Claude Code session, until it answers, fails, or the run's time is up."""
        started = time.monotonic()
        stream = _Stream()
        with (
            SandboxToolServer(
                context, command_timeout_seconds=self.command_timeout_seconds
            ) as server,
            tempfile.TemporaryDirectory(prefix="agentenv-claude-") as scratch,
        ):
            config = Path(scratch) / "mcp.json"
            config.write_text(
                json.dumps({"mcpServers": {SERVER_NAME: {"type": "http", "url": server.url}}}),
                encoding="utf-8",
            )
            workdir = Path(scratch) / "cwd"  # empty: no project instructions or settings
            workdir.mkdir()
            try:
                process = subprocess.Popen(
                    self._argv(config, str(context.task.workspace)),
                    cwd=workdir,
                    env=_clean_environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=sys.platform != "win32",
                    creationflags=_CREATION_FLAGS,
                )
            except OSError as error:
                raise AgentUnavailableError(f"could not start Claude Code: {error}") from error
            readers = [
                threading.Thread(
                    target=_drain,
                    args=(process.stdout, stream),
                    kwargs={"stderr": False},
                    daemon=True,
                ),
                threading.Thread(
                    target=_drain,
                    args=(process.stderr, stream),
                    kwargs={"stderr": True},
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            if process.stdin is not None:
                # The instructions go through stdin: no command-line length or quoting limits.
                with contextlib.suppress(OSError):  # the CLI may already have exited
                    process.stdin.write(context.task.instructions.encode("utf-8"))
                    process.stdin.close()
            out_of_time = False
            try:
                while process.poll() is None:
                    remaining = context.session.remaining_seconds()
                    if remaining is not None and remaining <= 1:
                        out_of_time = True  # budget used up, or the run was cancelled
                        break
                    if self._over_usage_guard(stream):
                        break
                    time.sleep(_POLL_SECONDS)
            finally:
                _kill_tree(process)
                for reader in readers:
                    reader.join(timeout=10)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                context.save_artifact(
                    "agent/transcript.jsonl", "".join(f"{line}\n" for line in stream.lines).encode()
                )
        if self._over_usage_guard(stream):
            raise AgentUnavailableError(
                f"stopped: the account's usage allowance reached the guard of "
                f"{self.usage_guard:.0%}; the run is not scored, and the experiment's circuit "
                "breaker stops after a few such runs"
            )
        if out_of_time:
            raise AgentTimeoutError(
                f"Claude Code was stopped when the run's time budget ended "
                f"({context.time_budget_seconds:.0f}s)"
            )
        return self._result(context, stream, server.log, process.returncode, started)

    def _over_usage_guard(self, stream: _Stream) -> bool:
        return (
            self.usage_guard is not None
            and stream.usage_fraction is not None
            and stream.usage_fraction >= self.usage_guard
        )

    def _result(
        self,
        context: AgentContext,
        stream: _Stream,
        log: _ToolLog,
        exit_code: int | None,
        started: float,
    ) -> AgentResult:
        final = stream.result()
        if final is None:
            detail = (stream.stderr.strip() or "\n".join(stream.lines[-3:]))[-600:]
            failure = (
                AgentUnavailableError
                if any(marker in detail.lower() for marker in _UNAVAILABLE_MARKERS)
                else AgentError
            )
            raise failure(f"Claude Code exited with code {exit_code} without a result: {detail}")
        text = str(final.get("result") or "")
        if final.get("is_error") and any(m in text.lower() for m in _UNAVAILABLE_MARKERS):
            raise AgentUnavailableError(f"Claude Code could not reach the model: {text[:300]}")
        usage = final.get("usage") or {}
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        succeeded = final.get("subtype") == "success" and not final.get("is_error")
        return AgentResult(
            adapter=self.name,
            outcome=AgentOutcome.COMPLETED if succeeded else AgentOutcome.FAILED,
            summary=(text or str(final.get("subtype")))[:500],
            duration_ms=round((time.monotonic() - started) * 1000),
            command_count=context.session.command_count,
            metadata={
                "model": self.model,
                "models_used": ",".join(sorted((final.get("modelUsage") or {}).keys())) or None,
                "stop_reason": str(final.get("subtype")),
                "turns": final.get("num_turns"),
                "tool_calls": sum(log.calls.values()),
                "tool_errors": log.errors,
                "tools_used": json.dumps(log.calls, sort_keys=True),
                "prompt_tokens": int(usage.get("input_tokens") or 0) + cache_read + cache_write,
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "cost_usd_list_price": final.get("total_cost_usd"),
                "api_seconds": round(float(final.get("duration_api_ms") or 0) / 1000, 1),
                "effort": self.effort or "default",
                "seed": None,  # Claude Code does not take a sampling seed
            },
        )


def build(**options: Any) -> ClaudeCodeAgent:
    """Factory referenced by the agent spec files (``adapter: claude_code_agent:build``)."""
    return ClaudeCodeAgent(**options)
