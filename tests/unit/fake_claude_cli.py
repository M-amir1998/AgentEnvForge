"""A stand-in for the `claude` CLI, used by test_claude_code_agent (no network, no tokens).

It speaks the same MCP-over-HTTP protocol as Claude Code to the adapter's sandbox server and
prints stream-json lines like `claude -p --output-format stream-json`. FAKE_CLAUDE_MODE picks
the behaviour: solve (default), hang, usage-high, unavailable, crash, logged-out.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

MODE = os.environ.get("FAKE_CLAUDE_MODE", "solve")


def emit(message: dict[str, Any]) -> None:
    print(json.dumps(message), flush=True)


def rpc(url: str, method: str, params: dict[str, Any] | None = None, *, rid: int | None = 1) -> Any:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if rid is not None:
        message["id"] = rid
    request = urllib.request.Request(  # noqa: S310 - the adapter's local test server
        url,
        data=json.dumps(message).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        body = response.read()
    return json.loads(body)["result"] if body else None


def call(url: str, name: str, **arguments: Any) -> str:
    result = rpc(url, "tools/call", {"name": name, "arguments": arguments}, rid=2)
    return str(result["content"][0]["text"])


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print("9.9.9 (Claude Code)")
        return 0
    if argv[:2] == ["auth", "status"]:
        logged_in = MODE != "logged-out"
        print(json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai", "email": "x@y.z"}))
        return 0
    prompt = sys.stdin.read()
    config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    url = config["mcpServers"]["sandbox"]["url"]
    initialized = rpc(url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    rpc(url, "notifications/initialized", rid=None)
    tools = [tool["name"] for tool in rpc(url, "tools/list")["tools"]]
    emit(
        {
            "type": "system",
            "subtype": "init",
            "tools": tools,
            "protocol": initialized["protocolVersion"],
            "prompt": prompt[:40],
            "argv": argv,
        }
    )
    usage = 0.99 if MODE == "usage-high" else 0.5
    emit({"type": "rate_limit_event", "rate_limit_info": {"utilization": usage, "resetsAt": 1}})
    if MODE in {"hang", "usage-high"}:
        time.sleep(600)
    if MODE == "crash":
        sys.stderr.write("boom: something went wrong\n")
        return 1
    if MODE == "unavailable":
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "API Error: 529 Overloaded",
            }
        )
        return 1
    source = call(url, "read_file", path="calc.py")
    edit = call(url, "replace_in_file", path="calc.py", old="a - b", new="a + b")
    tests = call(url, "run", command="python -m pytest -q")
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": source[:20]}]}})
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": f"fixed the operator ({edit}; {' '.join(tests.split())})",
            "num_turns": 4,
            "total_cost_usd": 0.0125,
            "duration_api_ms": 2500,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 90,
                "output_tokens": 42,
            },
            "modelUsage": {"fake-model": {}},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
