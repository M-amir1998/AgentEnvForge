# Claude Code agent (hosted Claude models)

Runs [Claude Code](https://docs.claude.com/en/docs/claude-code) as an AgentEnvForge agent:
one `claude -p` session per run, with every built-in tool disabled. Its only tools come
from a small MCP server that the adapter hosts for the run, and they act inside the
sandbox:

```text
claude -p (model: Anthropic API) ──MCP over HTTP, 127.0.0.1──► SandboxToolServer ──► session
                                                               run · read_file ·      │
                                                               write_file ·           ▼
                                                               replace_in_file   offline sandbox
```

- **The sandbox stays offline.** The model runs at Anthropic; the CLI runs on the host.
  Only tool calls reach the container, as ordinary observed, deadline-bound commands.
- **Claude Code cannot touch the host's files**: `--tools ""` removes Bash, Read, Edit and
  the other built-in tools; `--strict-mcp-config` loads no other MCP server; the CLI starts
  in an empty directory with `--setting-sources project`, so your own plugins, hooks,
  skills and permissions do not apply.
- **No runaway spending**: when the run's time budget ends or the run is cancelled, the
  adapter kills the CLI. An optional `usage_guard` stops runs before a subscription's
  usage allowance is exhausted.

## Requirements

The Claude Code CLI, logged in (`claude auth status` shows `"loggedIn": true`), either
through a claude.ai subscription or with `ANTHROPIC_API_KEY`. The adapter looks for the
CLI in this order: the `claude_path` option, `$AGENTENV_CLAUDE_CLI`, `claude` on the PATH,
and the copy bundled with the Claude Code VS Code extension.

**Every run uses model capacity**: tokens on an API key, or part of a subscription's
usage allowance. On the three example tasks a run took 4-13 s of model time and cost
$0.001-0.03 at list price when measured (see the benchmark results).

## Use

```bash
uv run agentenv experiment validate benchmarks/claude-code/experiment.yaml --preflight
uv run agentenv run examples/off-by-one/task.yaml --agent examples/agents/claude-code/claude-haiku-5-5.yaml
```

Spec files: `claude-haiku-5-5.yaml`, `claude-sonnet-5-5.yaml`, `claude-opus-5-5.yaml`.

| Option | Default | Meaning |
|---|---|---|
| `model` | (required) | model ID passed to `--model`; full IDs are reproducible, aliases move |
| `effort` | CLI default | `--effort` (low ... max) |
| `max_budget_usd` | none | `--max-budget-usd` per run |
| `usage_guard` | none | stop when the CLI reports this fraction of the account's usage allowance (e.g. `0.98`); such runs are `agent_unavailable` (not scored) |
| `isolated` | `true` | ignore the user's Claude Code settings (see above) |
| `command_timeout_seconds` | 120 | limit for one `run` tool call (always clipped to the run's budget) |
| `claude_path` | discovered | path to the CLI |

## What is recorded

- `agent/transcript.jsonl`: the CLI's stream-json output (assistant messages, tool calls
  and results, the final result). Rate-limit events, which describe your account's usage
  level, are left out.
- Run metadata: model(s) used, turns, tool calls by name, tool errors, prompt / completion /
  cache tokens, Claude Code's list-price cost estimate, API time, stop reason.
- Preflight provenance (in the experiment manifest): CLI version, model, authentication
  method. Never the e-mail address, organisation or account IDs.

## Caveats

- **No seed.** Claude Code has no sampling seed, so repetitions are independent samples
  and an exact re-run is not possible. Hosted models also change over time; record the
  date with any result.
- **Scaffold.** This agent is Claude Code's agent loop with these four tools; the Ollama
  example uses its own simpler loop. Comparing the two families compares scaffold *and*
  model. Within this adapter, every model gets the same scaffold, tools and prompt.
- The cost figure is Claude Code's estimate at list prices, not a bill.
