# Experiment: small local models as coding agents on a CPU-only laptop

- Date: 2026-10-05
- Question: can small Ollama models act as real coding agents, and where do they fail?
- Agent: `examples/agents/ollama/ollama_agent.py` (host-side tool loop: `run`,
  `read_file`, `replace_in_file`, `write_file`, `finish`), `temperature=0`, `seed=42`,
  `num_ctx=12288`, `max_steps=30`
- Hardware: a laptop CPU, **no GPU**; Ollama 0.35.1, Q4 weights
- Tasks:
  - `examples/off-by-one`: **easy**. One-token bug; the visible test shows the symptom directly.
  - `examples/basic` (ledger): **hard**. Three interacting bugs in `Store.transaction()`
    (exception swallowed, wrong snapshot restored, shallow copy); the visible tests expose
    only the first.
- Time budget overridden with `--agent-timeout` (recorded in each manifest) because CPU
  inference is slow.

## Results

| Model | Task | Score | Passed | Steps | Agent time | Prompt / output tokens |
|---|---|---|---|---|---|---|
| qwen2.5-coder:1.5b | easy | 0.000 | no | 3 | 23 s | 2,172 / 77 |
| qwen2.5-coder:3b | easy | 0.071 | no | 30 (limit) | 432 s | 96,993 / 1,154 |
| qwen2.5-coder:7b | easy | **1.000** | **yes** | 4 | 109 s | 3,076 / 145 |
| qwen2.5-coder:7b | hard | 0.321 | no | 12 | 253 s | 19,979 / 478 |
| *noop baseline* | hard | 0.321 | no | - | - | - |
| *scripted reference* | both | 1.000 | yes | - | - | - |

Each cell is a **single run** (n = 1). These are illustrative observations, not
statistically meaningful pass rates; a proper comparison needs repetitions (planned
batch execution, v0.2).

## What each model did (from `agent/transcript.jsonl`)

**1.5B: destroyed the file, then claimed success.** It read `stats/window.py`, called
`write_file` with the literal content `<tool_response>`, then called `finish` with
"The tests passed." It never ran the tests. Its tool calls were written as JSON text
rather than native tool calls; the agent's fallback parser recovered them.

**3B: stuck in a loop on the wrong line.** It used the tools correctly, but alternated
the indentation of `averages.append(...)` 15 times, causing collection errors and then
test failures, and never touched the buggy `range(...)`. It hit the 30-step limit.

**7B on the easy task: textbook.** It read the file, replaced `len(values) - window` with
`len(values) - window + 1` (identical to the reference patch), ran the tests, saw them
pass and finished. It passed all 7 hidden tests it never saw.

**7B on the hard task: plausible process, wrong diagnosis, false success.** It listed
files, read the source and the tests, and ran the tests: a good start. It then attributed
the failure to `transfer()` (which already raises correctly) and inserted the same `raise`
line four times, ignoring the identical failing output after each attempt. It finished
with "The tests have passed" although the last test run reported `1 failed`. The final
score equals the no-op baseline.

## Conclusions

1. **Below ~7B, models are not usable as coding agents** in this setup. They fail at the
   protocol level: destructive edits, loops, hallucinated success.
2. **7B on CPU is a viable minimum for simple, well-localised bugs** with a clear failing
   test, at about 2 minutes per run.
3. **Multi-bug reasoning is beyond 7B here.** Typical failure: anchoring on where the symptom
   shows up rather than its root cause, and not updating after repeated identical feedback.
4. **Self-reported success is worthless as a signal.** Two of the four runs claimed success
   while failing. Independent grading with hidden tests in a separate sandbox is
   essential, which is the core design premise of AgentEnvForge (ADR-0007).

## Possible next steps

- Repetitions with different seeds to estimate pass rates (needs batch execution).
- Agent-scaffold experiments (each must be reported as a different agent):
  loop detection, a "verify before finish" check, smaller edit tools.
- The same tasks with larger hosted models for comparison.

## Reproduce

```bash
ollama pull qwen2.5-coder:7b
uv run agentenv run examples/off-by-one/task.yaml \
    --agent examples/agents/ollama/qwen2.5-coder-7b.yaml --agent-timeout 1500
uv run agentenv runs show <run-id>
```

Timings vary with hardware. With `temperature=0` and a fixed seed, transcripts should be
the same on the same machine and Ollama version, but this has not been verified across machines.
