# Benchmarks

Four benchmarks, each answering one question. Results in `results/` are committed exactly
as the tools wrote them (sanitised: no user names, host paths or account details). They
were measured on one laptop, so treat them as an example of what to expect, not as a
property of AgentEnvForge. Re-run them on your machine.

| Benchmark | Question | Agents | Results |
|---|---|---|---|
| [infrastructure](infrastructure/experiment.yaml) + [`run_scaling.py`](run_scaling.py) | How much overhead does a run have, and how does throughput scale with workers? | oracle, noop (deterministic) | [scaling](results/scaling/scaling.md) |
| [failure-isolation](failure-isolation/experiment.yaml) + [`check_failure_isolation.py`](check_failure_isolation.py) | Do crashes, timeouts and failures stay inside their own run under concurrency? | five deterministic agents with known outcomes | [report](results/failure-isolation/report.md) |
| [claude-code](claude-code/experiment.yaml) | How do hosted Claude models do as coding agents on easy, medium and hard bug fixes? | Claude Haiku 5.5 / Sonnet 5.5 / Opus 5.5 via [Claude Code](../examples/agents/claude-code/README.md), plus oracle and noop controls | [report](results/claude-code/report.md) |
| [local-ollama](local-ollama/experiment.yaml) | The same question for tiny local models on a CPU-only laptop | qwen2.5-coder 0.5b / 1.5b / 3b, plus oracle and noop controls | [partial report](results/local-ollama/report.md) (stopped after 8 of 45 runs) |

**Baselines are not AI agents.** `oracle` applies the maintainers' reference fix and must
pass; `noop` changes nothing and must fail. They validate the tasks and the pipeline.
The failure-isolation agents (`failing`, `sleeper`, `crasher`) are scripted to fail in a
known way. Only the Claude and Ollama models are AI agents.

**The local-model run was stopped early, on purpose.** On the development laptop each
model run took 2-5 minutes, and all four finished model runs failed without changing the
code meaningfully: every one used up its 20 model calls (0.5b re-ran the tests 19 times
without editing; 3b edited the wrong line). The full run would have taken hours to confirm
that, so it was cancelled after 8 runs (`cancelled` in the report; completed runs kept).
The hosted-model benchmark answers the same question with capable models.

## Reproduce

Requirements: Docker running, `uv sync` done (installs matplotlib for the plots).

```bash
# 1. Scaling: 3 interleaved trials x workers 1, 2, 4 of a 30-run experiment (~15-20 min)
uv run python benchmarks/run_scaling.py

# 2. Failure isolation: 20 runs, 2 workers (~1 min)
uv run agentenv experiment run benchmarks/failure-isolation/experiment.yaml --overwrite
uv run python benchmarks/check_failure_isolation.py benchmarks/results/failure-isolation

# 3. Claude models: 45 runs, 3 workers; needs the Claude Code CLI, logged in. Uses model
#    capacity (API tokens or part of a subscription's allowance).
uv run agentenv experiment validate benchmarks/claude-code/experiment.yaml --preflight
uv run agentenv experiment run benchmarks/claude-code/experiment.yaml --overwrite

# 4. Local models: 45 runs, 1 worker (hours on CPU; needs Ollama)
ollama pull qwen2.5-coder:0.5b
ollama pull qwen2.5-coder:1.5b
ollama pull qwen2.5-coder:3b
uv run agentenv experiment validate benchmarks/local-ollama/experiment.yaml --preflight
uv run agentenv experiment run benchmarks/local-ollama/experiment.yaml --overwrite
```

`--overwrite` replaces the committed results. Without it, AgentEnvForge refuses to write
into a directory holding another experiment's results. Use `--output DIR` to keep both.

## Method notes

- **Scaling** measures the experiment's RUNNING phase (first run started to last run
  finished). Image preparation happens before it and is excluded. Each trial is a separate
  `agentenv experiment run` process. Trials are interleaved across worker counts, with the
  order shuffled per trial, so drift on the machine does not favour one setting. Speedups
  are computed within each trial and summarised by median and range.
- **Failure isolation** passes only if every run ends with its agent's expected outcome,
  every cleanup succeeds and no container of its runs is left. CI runs it on Linux.
- **Claude models** use Claude Code's agent loop with four sandbox tools and the same
  prompt for every model, at the CLI's default effort. Claude Code takes no sampling seed,
  so the three repetitions are independent samples; hosted models change over time, so
  results are tied to their date. 600 s budget per run; 3 workers, because the models
  run remotely and each agent loop is a mostly idle local process. A usage guard stops
  runs at 98% of the account's usage allowance (such runs would be excluded, not scored).
- **Local models** run with `workers: 1`: the agent loop and the model share the CPU, so
  concurrency would change timings and, through the time budget, results. Each model gets
  three seeded repetitions per task at temperature 0.2, at most 20 model calls, an 8k
  context and a 600 s budget per run.
- With 3 repetitions on 3 tasks, every interval is wide; the reports say which statistics
  the sample size supports. Comparing the Claude and Ollama results compares scaffold *and*
  model: the two adapters use different agent loops.
