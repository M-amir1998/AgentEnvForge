# AgentEnvForge

[![CI](https://github.com/M-amir1998/AgentEnvForge/actions/workflows/ci.yml/badge.svg)](https://github.com/M-amir1998/AgentEnvForge/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**AgentEnvForge is an open-source framework for defining, executing, grading and
reproducing isolated software-engineering environments for training and evaluating AI
agents.**

You describe a task once (a repository in a known state, an environment and a way to
grade a solution) in a versioned YAML file. AgentEnvForge then runs any agent against it
in a disposable, resource-limited Docker sandbox, grades the result in a *separate*
sandbox with hidden tests, and records everything needed to reproduce and audit the run.

> Status: **pre-release (0.1.0.dev0)**. Single runs and batch experiments (tasks x agents x
> repetitions, bounded concurrency, statistics and reports) are implemented and tested end
> to end. See [what is supported today](#status) and the [roadmap](docs/design/roadmap.md).

## Highlights

Measured 2026-10-16 on one laptop (CPU only;
Windows 11, Docker Desktop 29.8.1). Raw data and method: [benchmarks/](benchmarks/README.md).

- **Hosted Claude models as coding agents** (via Claude Code, acting only through the
  offline sandbox): on three bug-fix tasks x 3 repetitions, Haiku 5.5 passed **8/9**,
  Sonnet 5.5 **9/9** and Opus 5.5 **9/9**; the 45 runs took 2 min 24 s at 3 workers and
  about $0.44 at list price. With 9 runs per model, the intervals overlap: no ranking.
- **Hidden tests matter**: Haiku's one failure fixed 2 of 3 bugs, passed the visible tests
  and said it was done; the hidden tests failed it (5/7).
- **270 of 270** scaling-benchmark runs at 1, 2 and 4 workers gave the expected result,
  every cleanup succeeded and **0 containers** were left. Running 4 at once was **2.32x**
  faster than 1 (median of within-trial speedups, range 1.47-2.51x).
- **20 of 20** runs ended with their expected outcome while a crashing adapter, a hanging
  agent and failing agents ran side by side ([failure isolation](benchmarks/results/failure-isolation/report.md)).
- Tiny local models (qwen2.5-coder 0.5b-3b on CPU) passed **0 of 4** finished runs; the
  benchmark was stopped early because each run took 2-5 minutes.

## Why

Evaluating a coding agent on one task by hand is easy: clone, install, break, prompt,
test, clean up. Doing it reproducibly for hundreds of tasks, several agents and
repeated trials is an infrastructure problem: isolation, resource limits, hidden
tests, timeouts, cleanup, provenance and result storage. AgentEnvForge is that
infrastructure, so benchmark authors and agent builders can stop rebuilding it.

It is for people who **build coding agents**, **create agent benchmarks**, **generate RL
rollouts** or **study failure modes and reward hacking**. It is provider-agnostic: no
LLM SDK is a dependency, and agents plug in through a small adapter interface.

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker (Desktop or Engine), git.

```bash
git clone https://github.com/M-amir1998/AgentEnvForge.git
cd AgentEnvForge
make setup                      # or: uv sync

uv run agentenv doctor          # checks Python, git, Docker daemon, disk, database
uv run agentenv task validate examples/basic/task.yaml
uv run agentenv run examples/basic/task.yaml --agent examples/basic/agents/reference.yaml
```

```text
AgentEnvForge  run run_01m44csm77277gf42f064mej0t  task ledger-transaction-rollback  agent command:reference-solver

  Validation           PASS     0.07s
  Source preparation   PASS     0.02s
  Sandbox build        PASS     9.02s      (0.03s on later runs: the image is cached)
  Sandbox start        PASS     0.37s
  Agent execution      PASS     1.25s
  Solution capture     PASS     0.23s
  Grader               PASS     2.18s
  Artifacts            PASS     0.01s
  Cleanup              PASS     0.12s

Result: SUCCESS  (2/2 graders passed)
Score:  1.000
  - hidden-tests: pass 7/7 tests passed
  - visible-tests: pass 2/2 tests passed
```

Run a **real LLM agent** on your laptop with a local Ollama model (CPU is enough; see
[examples/agents/ollama](examples/agents/ollama/README.md)):

```bash
ollama pull qwen2.5-coder:7b
uv run agentenv run examples/off-by-one/task.yaml     --agent examples/agents/ollama/qwen2.5-coder-7b.yaml --agent-timeout 1500
```

Or a hosted Claude model through your existing Claude Code login. The CLI's own tools are
disabled; it acts only through the sandbox ([details](examples/agents/claude-code/README.md)).
Every run uses model capacity:

```bash
uv run agentenv run examples/off-by-one/task.yaml --agent examples/agents/claude-code/claude-haiku-5-5.yaml
```

The same task with an agent that does nothing **fails** grading (`--agent noop`, exit
code 1), which shows the task really discriminates. Then inspect what happened:

```bash
uv run agentenv runs list
uv run agentenv runs show <run-id> --events     # timings, provenance, event log
uv run agentenv runs artifacts <run-id>         # solution.patch, grader reports, logs, ...
```

### Batch experiments

Describe tasks x agents x repetitions once, then run the whole matrix with bounded
concurrency and get a statistical report:

```bash
uv run agentenv experiment validate benchmarks/failure-isolation/experiment.yaml
uv run agentenv experiment run benchmarks/failure-isolation/experiment.yaml --output results/demo
```

```text
  [ 1/20] passed                  4.3s  oracle / moving-average-off-by-one #1
  [ 2/20] agent timeout           7.2s  sleeper / moving-average-off-by-one #1
      agent command exceeded the 5s time budget (run_01m53bj41efhf1pmn2ra09k0q1)
  [ 3/20] agent error             0.6s  crasher / moving-average-off-by-one #1
      agent adapter crasher raised RuntimeError: deliberate adapter crash in run run_01m53bjb3r09awdhr1gkb (run_01m53bjb3r09awdhr1gkbmgv87)
  ...
results valid (no validity warnings)
results: /path/to/AgentEnvForge/results/demo
  report: /path/to/AgentEnvForge/results/demo/report.md
```

The results directory holds `report.md`, `summary.json`, `runs.csv`, `failures.csv`,
plots, and a manifest with everything needed to reproduce the experiment.

Ctrl+C stops scheduling, cancels and cleans up the active runs, and still reports the
completed ones. See the [batch experiments tutorial](docs/tutorials/batch-experiments.md).

Next: [write your first task](docs/tutorials/first-task.md) ·
[connect your own agent](docs/tutorials/custom-agent-adapter.md) ·
[custom grading](docs/tutorials/custom-grader.md) ·
[batch experiments](docs/tutorials/batch-experiments.md)

## How it works

```text
 task.yaml ──► validate ──► prepare source ──► build image (cached by content hash)
                                                     │
            ┌────────────── agent sandbox ───────────┴───┐
            │ workspace copied in (no host mounts)        │
            │ non-root · no capabilities · no network     │
            │ CPU / memory / PID / time limits            │
            │ agent acts through an observed session      │
            └───────────────┬─────────────────────────────┘
                            │ final workspace copied out → solution.patch
            ┌───────────────▼──────────── grader sandbox ──┐
            │ fresh container · agent's files + protected   │
            │ paths restored · hidden tests added only here │
            └───────────────┬───────────────────────────────┘
                            ▼
      result + events (SQLite) · artifacts (filesystem) · manifest · cleanup
```

Every run moves through an explicit state machine and **always** passes through cleanup
before reaching exactly one terminal state (`completed`, `failed_build`, `timed_out`, ...).
Infrastructure failure and agent failure are kept apart. `completed` with
`passed=false` means the agent was wrong. `failed_grader` means we could not judge.

Read more: [architecture overview](docs/architecture/overview.md) ·
[execution lifecycle](docs/architecture/execution-lifecycle.md) ·
[security model](docs/architecture/security.md) · [design decisions (ADRs)](docs/adr/)

## Experimental results

| Benchmark | What ran | Result |
|---|---|---|
| [Claude models](benchmarks/results/claude-code/report.md) | Haiku 5.5, Sonnet 5.5, Opus 5.5 x 3 tasks (easy, medium, hard) x 3 repetitions, plus oracle/noop controls; 3 workers | 8/9, 9/9, 9/9 passed (95% CI for Haiku 57-98%, Opus 70-100%; Sonnet's patches were identical in every repetition, so it counts as one observation per task); median run 11.8 / 11.6 / 18.7 s; controls valid |
| [Scaling](benchmarks/results/scaling/scaling.md) | 30-run experiment (oracle/noop x 3 tasks x 5) at 1, 2, 4 workers, 3 interleaved trials | median running time 108.8 / 66.5 / 43.3 s; within-trial speedup 1.64x (2 workers), 2.32x (4 workers); all valid, 0 leftovers |
| [Failure isolation](benchmarks/results/failure-isolation/report.md) | 5 deterministic agents with known outcomes x 2 tasks x 2, 2 workers | 20/20 expected outcomes, all cleanups succeeded, 0 leftovers |
| [Tiny local models](benchmarks/results/local-ollama/report.md) | qwen2.5-coder 0.5b / 1.5b / 3b on CPU (Ollama) | stopped after 8 of 45 runs: 0/4 model runs passed, each used all 20 model calls |

The oracle, noop and failure-isolation agents are scripts, not AI agents: they validate
the tasks and the infrastructure. The Claude and Ollama agents use different agent loops,
so comparing those two families compares scaffold *and* model. Three example tasks are
far too few to characterise a model: these results show what the framework measures,
not how good the models are in general.

## Status

| Capability | State |
|---|---|
| Versioned task schema (v1), validation with actionable errors, JSON Schema | **implemented** |
| Local-directory and git sources (pinned commit, resolved SHA recorded) | **implemented** |
| Docker sandbox: CPU, memory (no swap), PIDs, network off by default, non-root, caps dropped | **implemented** |
| Per-command and total-agent timeouts; build/start/grader/cleanup timeouts | **implemented** |
| Agents: `command` (in-sandbox program), `python` (host-side adapter), `manual`, `noop` | **implemented** |
| Real LLM agent example on local Ollama models, no GPU or API key ([results](docs/experiments/2026-10-small-local-models.md)) | **experimental** |
| Claude Code agent example (Haiku, Sonnet, Opus), acting only through the sandbox via MCP ([README](examples/agents/claude-code/README.md)) | **experimental** |
| Graders: `pytest` (JUnit-parsed), `command`, composite (`all` / `weighted`) | **implemented** |
| Separate grading sandbox, hidden assets, protected-path restoration | **implemented** |
| Run state machine, failure taxonomy, structured events, JSON logs, secret redaction | **implemented** |
| SQLite persistence, local artifact store, reproducibility manifest, patch capture | **implemented** |
| `doctor`, `cleanup` of orphaned sandboxes | **implemented** |
| Batch experiments: tasks x agents x repetitions, bounded workers, backpressure, cancellation, circuit breaker | **implemented** |
| Experiment statistics (Wilson intervals, gated pass@k, latency, failure breakdown) and JSON/CSV/Markdown/plot reports | **implemented** |
| Benchmarks: scaling, failure isolation, tiny local models ([benchmarks/](benchmarks/README.md)) | **implemented** |
| Retries of infrastructure failures, resuming interrupted experiments | planned |
| `reproduce <run-id>`, HTTP API, metrics export | planned |
| Disk quotas, gVisor/Firecracker isolation, distributed workers | planned |

Known limitations are tracked honestly in [docs/development/status.md](docs/development/status.md).
In particular, Docker containers share the host kernel. Treat AgentEnvForge as a
defence-in-depth boundary for semi-trusted code, not a VM-grade one ([details](docs/architecture/security.md)).

## Development

```bash
make check        # ruff lint + format check, strict mypy, all tests
make test-unit    # fast, no Docker
make test-e2e     # complete runs of the example task (Docker)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/development/environment-setup.md](docs/development/environment-setup.md).

## License

[Apache-2.0](LICENSE). This is a permissive license with an explicit patent grant, a
common choice for infrastructure that companies and researchers build on.
