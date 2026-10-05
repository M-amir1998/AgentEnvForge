"""A real LLM coding agent backed by a local Ollama model.

The agent loop runs on the host; only the model's *actions* reach the sandbox, through
``context.session``. The sandbox therefore keeps networking disabled, and every action
is bounded by the run's time budget and recorded as an event.

Loop::

    instructions -> model -> tool call(s) -> sandbox -> tool results -> model -> ... -> finish

Small models are unreliable tool callers, so the agent:

* accepts native Ollama tool calls *and* tool calls written as JSON in the message text;
* nudges the model when it answers without calling a tool;
* truncates tool output to keep the context (and CPU prompt processing) small;
* uses ``temperature=0`` and a fixed seed, so runs are as repeatable as the hardware allows.

Only the Python standard library is used: AgentEnvForge's core stays provider-free.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from agentenvforge.agents.base import AgentContext
from agentenvforge.domain.failures import AgentError, SandboxPathNotFoundError
from agentenvforge.domain.results import AgentOutcome, AgentResult

SYSTEM_PROMPT = """\
You are a careful software engineer working in a Linux sandbox. The repository is in the
current directory ({workspace}). There is no internet access.

Work in small steps using the tools:
1. Explore: list files and read the relevant source and tests.
2. Find the root cause of the problem described by the user.
3. Edit the code with replace_in_file (preferred) or write_file.
4. Run the tests with: run(command="python -m pytest -q")
5. When the tests pass, call finish with a short summary.

Rules: call exactly one tool per reply. Do not edit tests. Do not guess file contents;
read files before editing them.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run",
            "description": "Run a shell command in the repository and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "e.g. ls -R"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Return the content of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace one exact occurrence of `old` with `new` in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string", "description": "exact text currently in the file"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Replace the whole content of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Stop working. Call this when the task is done.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]
TOOL_NAMES = frozenset(tool["function"]["name"] for tool in TOOLS)

_MAX_TOOL_OUTPUT_CHARS = 2500
_MAX_NUDGES = 3
_TEXT_TOOL_CALL = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>|```(?:json)?\s*(\{.*?\})\s*```", re.S
)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One action requested by the model."""

    name: str
    arguments: dict[str, Any]


def _as_arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return _as_arguments(parsed)
    return {}


def _from_json_object(text: str) -> ToolCall | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name") or data.get("tool") or data.get("function")
    if isinstance(name, dict):  # {"function": {"name": ..., "arguments": ...}}
        data, name = name, name.get("name")
    if not isinstance(name, str) or name not in TOOL_NAMES:
        return None
    return ToolCall(name, _as_arguments(data.get("arguments") or data.get("parameters") or {}))


def extract_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    """Return tool calls from a chat message: native ones first, else ones written as text."""
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        name = function.get("name")
        if name in TOOL_NAMES:
            calls.append(ToolCall(name, _as_arguments(function.get("arguments"))))
    if calls:
        return calls
    content = message.get("content") or ""
    for match in _TEXT_TOOL_CALL.finditer(content):
        call = _from_json_object(match.group(1) or match.group(2))
        if call is not None:
            calls.append(call)
    if not calls:
        stripped = content.strip()
        if stripped.startswith("{"):
            call = _from_json_object(stripped)
            if call is not None:
                calls.append(call)
    return calls


def _truncate(text: str, limit: int = _MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} characters omitted ...]\n" + text[-limit:]


@dataclass
class _Stats:
    steps: int = 0
    tool_calls: int = 0
    invalid_replies: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_seconds: float = 0.0
    tools_used: dict[str, int] = field(default_factory=dict)


class OllamaAgent:
    """Tool-using coding agent driven by an Ollama chat model."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        max_steps: int = 30,
        num_ctx: int = 12288,
        temperature: float = 0.0,
        seed: int = 42,
        command_timeout_seconds: int = 120,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_steps = max_steps
        self.options = {"num_ctx": num_ctx, "temperature": temperature, "seed": seed}
        self.command_timeout_seconds = command_timeout_seconds

    @property
    def name(self) -> str:
        """``ollama:<model>``."""
        return f"ollama:{self.model}"

    # ------------------------------------------------------------------ model

    def _chat(
        self, messages: list[dict[str, Any]], timeout: float, stats: _Stats
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "tools": TOOLS,
                "stream": False,
                "think": False,
                "options": self.options,
                "keep_alive": "15m",
            }
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - URL comes from the trusted agent spec
            f"{self.base_url}/api/chat", data=body, headers={"Content-Type": "application/json"}
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                reply: dict[str, Any] = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:300]
            raise AgentError(f"Ollama rejected the request ({error.code}): {detail}") from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise AgentError(
                f"cannot reach Ollama at {self.base_url} ({error}); is `ollama serve` running?"
            ) from error
        stats.llm_seconds += time.monotonic() - started
        stats.prompt_tokens += int(reply.get("prompt_eval_count") or 0)
        stats.completion_tokens += int(reply.get("eval_count") or 0)
        message = reply.get("message")
        if not isinstance(message, dict):
            raise AgentError(f"unexpected Ollama response: {str(reply)[:300]}")
        return message

    # ------------------------------------------------------------------ tools

    def _execute(self, call: ToolCall, context: AgentContext) -> str:
        tools = {
            "run": self._run,
            "read_file": self._read,
            "write_file": self._write,
            "replace_in_file": self._replace,
        }
        tool = tools.get(call.name)
        if tool is None:
            return f"error: unknown tool {call.name}"
        try:
            return tool(context, call.arguments)
        except SandboxPathNotFoundError:
            # A wrong path is the model's mistake: report it so it can correct itself.
            return f"error: {call.arguments.get('path')} does not exist"

    def _run(self, context: AgentContext, args: dict[str, Any]) -> str:
        command = str(args.get("command", "")).strip()
        if not command:
            return "error: `command` is required"
        # The shell is explicit: the model writes shell syntax (pipes, &&).
        result = context.session.run(
            ["sh", "-c", command], timeout_seconds=self.command_timeout_seconds
        )
        status = "timed out" if result.timed_out else f"exit code {result.exit_code}"
        return _truncate(f"[{status}]\n{result.stdout}{result.stderr}")

    @staticmethod
    def _read(context: AgentContext, args: dict[str, Any]) -> str:
        content = context.session.read_file(str(args.get("path", "")))
        return _truncate(content.decode("utf-8", "replace"), 6000)

    @staticmethod
    def _write(context: AgentContext, args: dict[str, Any]) -> str:
        content = str(args.get("content", "")).encode("utf-8")
        context.session.write_file(str(args.get("path", "")), content)
        return "file written"

    @staticmethod
    def _replace(context: AgentContext, args: dict[str, Any]) -> str:
        path, old, new = (
            str(args.get("path", "")),
            str(args.get("old", "")),
            str(args.get("new", "")),
        )
        content = context.session.read_file(path).decode("utf-8", "replace")
        occurrences = content.count(old) if old else 0
        if occurrences != 1:
            return (
                f"error: `old` must match exactly once, but it matches {occurrences} times. "
                "Read the file again and copy the text exactly, including indentation."
            )
        context.session.write_file(path, content.replace(old, new).encode("utf-8"))
        return "replaced 1 occurrence"

    # ------------------------------------------------------------------- loop

    def run(self, context: AgentContext) -> AgentResult:
        """Drive the model until it calls ``finish``, runs out of steps, or of time."""
        started = time.monotonic()
        stats = _Stats()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(workspace=context.task.workspace)},
            {"role": "user", "content": context.task.instructions},
        ]
        finished, summary, nudges = False, "", 0
        try:
            while stats.steps < self.max_steps and not finished:
                remaining = context.session.remaining_seconds()
                if remaining is not None and remaining < 5:
                    summary = "stopped: time budget nearly exhausted"
                    break
                stats.steps += 1
                message = self._chat(messages, timeout=remaining or 600, stats=stats)
                messages.append(
                    {
                        key: message[key]
                        for key in ("role", "content", "tool_calls")
                        if key in message
                    }
                )
                calls = extract_tool_calls(message)
                if not calls:
                    stats.invalid_replies += 1
                    nudges += 1
                    if nudges > _MAX_NUDGES:
                        summary = "stopped: the model repeatedly answered without calling a tool"
                        break
                    messages.append(
                        {"role": "user", "content": "Call one of the tools (run, read_file, "
                         "replace_in_file, write_file, finish). Do not answer in prose."}
                    )  # fmt: skip
                    continue
                for call in calls:
                    stats.tool_calls += 1
                    stats.tools_used[call.name] = stats.tools_used.get(call.name, 0) + 1
                    if call.name == "finish":
                        finished, summary = True, str(call.arguments.get("summary", ""))
                        break
                    output = self._execute(call, context)
                    messages.append({"role": "tool", "tool_name": call.name, "content": output})
            if not finished and not summary:
                summary = f"stopped: reached max_steps={self.max_steps}"
        finally:
            context.save_artifact(
                "agent/transcript.jsonl",
                "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode(),
            )
        return AgentResult(
            adapter=self.name,
            outcome=AgentOutcome.COMPLETED if finished else AgentOutcome.FAILED,
            summary=summary[:500],
            duration_ms=round((time.monotonic() - started) * 1000),
            command_count=context.session.command_count,
            metadata={
                "model": self.model,
                "steps": stats.steps,
                "tool_calls": stats.tool_calls,
                "invalid_replies": stats.invalid_replies,
                "prompt_tokens": stats.prompt_tokens,
                "completion_tokens": stats.completion_tokens,
                "llm_seconds": round(stats.llm_seconds, 1),
                "tools_used": json.dumps(stats.tools_used, sort_keys=True),
            },
        )


def build(**options: Any) -> OllamaAgent:
    """Factory referenced by the agent spec files (``adapter: ollama_agent:build``)."""
    return OllamaAgent(**options)
