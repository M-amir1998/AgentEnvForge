# Ollama agent: real LLM agents on a laptop

A tool-using coding agent driven by a local [Ollama](https://ollama.com) model. It runs
entirely offline and needs no API keys or GPU.

```bash
ollama pull qwen2.5-coder:7b
uv run agentenv run examples/off-by-one/task.yaml \
    --agent examples/agents/ollama/qwen2.5-coder-7b.yaml --agent-timeout 1500
```

`--agent-timeout` raises the task's agent time budget, because CPU inference is slow. The
override is recorded in the run manifest (`overrides`), so results run with it are not
mistaken for results under the task's own budget.

## How it works

- The loop (`ollama_agent.py`) runs **on the host**. Only the model's actions (`run`,
  `read_file`, `replace_in_file`, `write_file`) reach the sandbox, through the session,
  so the sandbox keeps networking disabled and every action is recorded and time-bounded.
- Native Ollama tool calls are used when the model emits them. Small models often write
  tool calls as JSON text instead (```` ```json {...} ``` ```` or `<tool_call>`), and the agent
  recovers those too.
- Tool output is truncated to about 2,500 characters to keep the context small. That matters
  on a CPU, where reading the prompt costs time on every step.
- `temperature: 0` and `seed: 42` make runs as repeatable as CPU inference allows.
- Each run stores `agent/transcript.jsonl` (every message) and token/timing statistics in
  `result.json` → `agent.metadata`.

## Specs

| File | Model | Size |
|---|---|---|
| `qwen2.5-coder-1.5b.yaml` | `qwen2.5-coder:1.5b` | 1.0 GB |
| `qwen2.5-coder-3b.yaml` | `qwen2.5-coder:3b` | 1.9 GB |
| `qwen2.5-coder-7b.yaml` | `qwen2.5-coder:7b` | 4.7 GB |
| `qwen2.5-1.5b.yaml` | `qwen2.5:1.5b` (general, not code-tuned) | 1.0 GB |

To try another model, copy a spec and change `model`. Options: `base_url`, `max_steps`,
`num_ctx`, `temperature`, `seed`, `command_timeout_seconds`.

Measured results on a CPU-only laptop are in
[docs/experiments/2026-10-small-local-models.md](../../../docs/experiments/2026-10-small-local-models.md).
