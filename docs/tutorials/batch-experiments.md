# Tutorial: batch experiments

A single `agentenv run` tells you what one agent did once. To compare agents, or to know how
reliable one agent is, you need many runs: several tasks, several agents, several
repetitions. Describing these in an **experiment file** makes them reproducible. This
tutorial writes one, runs it, and reads the results.

Prerequisites: the [quick start](../../README.md#quick-start) works (`agentenv doctor` is
green). The Ollama part is optional. For plots, install the extra:
`uv sync` (development) or `pip install 'agentenvforge[plots]'`.

## 1. Write the experiment

`my-experiment.yaml`, next to the repository's `examples/` directory:

```yaml
schema_version: "1"
id: my-first-experiment
name: Two tasks, a model and two control agents
tasks:                                   # task files, relative to this file
  - examples/off-by-one/task.yaml
  - examples/lru-cache/task.yaml
agents:
  - id: qwen-1.5b                        # the agent under evaluation ("subject")
    spec: examples/agents/ollama/qwen2.5-coder-1.5b.yaml
    options: {temperature: 0.2}          # python agents: override spec options
    agent_timeout_seconds: 600           # override the tasks' agent budget (recorded)
  - id: oracle                           # applies the reference fix: must pass
    role: reference
    spec: examples/agents/oracle/oracle.yaml
  - id: noop                             # changes nothing: must fail
    role: baseline
    agent: {type: noop}                  # inline spec instead of a file
repetitions: 3
execution:
  workers: 1                             # runs executing at once
  base_seed: 0                           # repetition r gets seed base_seed + r - 1
output:
  directory: results/my-first-experiment # default: <AGENTENV_HOME>/experiments/<id>
```

That is 2 tasks x 3 agents x 3 repetitions = 18 planned runs.

**Roles.** `reference` and `baseline` agents validate the tasks. An oracle that fails means
a broken task or broken infrastructure; a no-op that passes means the task does not
discriminate. Both conditions are reported as validity warnings. Control agents are never
compared with subjects. They are not AI agents, and their numbers are not results.

## 2. Validate and inspect

```bash
uv run agentenv experiment validate my-experiment.yaml --preflight
uv run agentenv experiment inspect my-experiment.yaml
```

`validate` checks the experiment file and every task and agent it references, and reports
all problems at once (`field / reason / received`). `--preflight` also asks each agent
that supports it whether it is ready: for Ollama, whether the server answers and the model
is installed. `inspect` shows the resolved tasks, agents, hashes, warnings and the first
runs in execution order.

## 3. Run

```bash
uv run agentenv experiment run my-experiment.yaml
```

The start of a real run, the [local-ollama benchmark](../../benchmarks/local-ollama/experiment.yaml)
on a CPU-only laptop:

```text
experiment local-ollama  Tiny local coding models (Ollama, CPU only)
  3 task(s) x 5 agent(s) x 3 repetition(s) = 45 planned runs, 1 worker(s)
  ...
  checking agent qwen2.5-coder-0.5b ...
  ...
  preparing the image of ledger-transaction-rollback ...
  [ 1/45] passed                  2.4s  oracle / moving-average-off-by-one #1
  [ 2/45] failed                 2m14s  qwen2.5-coder-0.5b / moving-average-off-by-one #1
  [ 3/45] failed                 4m41s  qwen2.5-coder-3b / moving-average-off-by-one #1
  [ 4/45] failed                  2.4s  noop / moving-average-off-by-one #1
  ...
```

Each line is one finished run: its outcome, its duration, and agent / task / repetition.
A graded `failed` means the agent finished but the hidden tests failed. For any other
failure (timeout, crash, unreachable model server), a second line says why and names the
run, so `agentenv runs show <run id>` can show everything about it. Agents run in a
shuffled order within each task and repetition, so slow drift on the machine affects them
alike.

Options:

| Option | Effect |
|---|---|
| `--workers N` | override `execution.workers` |
| `--output DIR` | override `output.directory` |
| `--overwrite` | replace results of an earlier experiment in that directory (otherwise refused before anything runs) |
| `--export-artifacts` | also copy each run's `result.json`, `grade.json`, `solution.patch` and agent transcript |

**Stopping.** Press Ctrl+C once: no new runs start, active runs are cancelled and cleaned
up, and the results of completed runs are summarised and written. The experiment ends
`cancelled` (exit code 130). A third Ctrl+C exits immediately; `agentenv cleanup` then
removes any sandboxes that were still running.

**Exit codes**: 0 completed with valid results; 1 completed, but with validity warnings;
2 invalid input; 3 the experiment failed (preparation failed, or 5 consecutive runs failed
for reasons outside the agents' control); 130 cancelled.

## 4. Read the results

```text
results/my-first-experiment/
  report.md          start here
  summary.json       every statistic, machine-readable
  runs.csv           one row per planned run
  failures.csv       failure breakdown: outcome, reason, stage, count
  manifest.json      what exactly ran: versions, hashes, agent provenance, images, host
  environment.json   hardware and software of the machine
  experiment.yaml    the definition as it was run
  plots/             success_by_agent, success_matrix, run_time_by_agent, outcomes_by_agent
```

`report.md` starts with validity: did every planned run execute, were runs excluded, did
the controls behave? Only then does it show results: per agent (success rate with a 95%
Wilson interval, mean over tasks, pass@k, timeouts), per agent x task, per task, run times
and failures.

How to read the numbers:

- **Scored runs** are the success-rate denominator: runs in which the agent got to act.
  Failures before the agent started (image build, sandbox start), unreachable model servers
  and cancellations are *excluded* and listed separately. They say nothing about the agent.
- **Intervals** are wide at small n: 2/3 is "somewhere between 21% and 94%". Do not rank
  agents whose intervals overlap.
- **identical**: all repetitions of a cell produced the same outcome and the same patch.
  The agent is deterministic there, so the repetitions count as one observation and no
  interval is shown.
- **pass@k** (k = repetitions) is the fraction of tasks solved at least once in k
  attempts. It is not shown for deterministic agents or incomplete cells.
- **`>=` on durations**: some runs hit their time budget, so the true value is larger.

The full methodology is in [ADR-0009](../adr/0009-experiment-statistics.md).

`runs.csv` loads directly into pandas or a spreadsheet:

```python
import pandas as pd

runs = pd.read_csv("results/my-first-experiment/runs.csv")
scored = runs[runs.scored]
print(scored.groupby(["agent_id", "task_id"]).success.agg(["sum", "count"]))
```

Columns: `experiment_id, position, run_id, task_id, agent_id, role, model, repetition,
seed, status, outcome, scored, success, score, duration_seconds, agent_seconds,
agent_budget_seconds, failure_reason, failure_stage, started_at, finished_at, cleanup_ok,
prompt_tokens, completion_tokens, image_id, patch_sha256`. Empty cells are missing values.

All exported files are sanitised: paths become relative (`.`), `$AGENTENV_HOME` or `~`, so
the directory can be shared or committed without revealing your user name or directory
layout.

## 5. Later

```bash
uv run agentenv experiments list
uv run agentenv experiments show <experiment id>    # status history, provenance, results
uv run agentenv experiment report <experiment id> [--output DIR]   # regenerate the files
```

`report` recomputes everything from the database, so results stay available after the
results directory is deleted. An experiment whose process was killed shows as
`interrupted`.

## Choosing the number of workers

- Agents that run inside the sandbox and are cheap (scripts, the oracle): more workers
  help until Docker or the disk saturates. Measure on your machine with
  `python benchmarks/run_scaling.py`.
- Host-side agents (python adapters, e.g. Ollama): use `workers: 1` when comparing them.
  Concurrent model calls compete for the same CPU, which changes timings and, through
  time budgets, results. AgentEnvForge warns about this.
- Every worker runs up to two containers. Keep the number of workers below the CPUs Docker
  may use.

## Troubleshooting

| Symptom | Meaning |
|---|---|
| `agent X is not ready` before any run | preflight failed: start Ollama, or `ollama pull` the model |
| status `failed`, "5 consecutive runs failed" | Docker or the model server stopped working; see `failures.csv` |
| `already holds the results of experiment ...` | pass `--overwrite` or another `--output` |
| status `interrupted` | the process was killed; run `agentenv cleanup`, then `experiment report` |
| `warning: workers=N with host-side agents` | see "Choosing the number of workers" |
