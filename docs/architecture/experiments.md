# Experiments: batch execution

An **experiment** runs a matrix of tasks x agents x repetitions and summarises the results.
Every planned run is an ordinary run, executed by the same `RunOrchestrator` as
`agentenv run`. Batch execution adds scheduling, cancellation, bookkeeping and analysis
around runs, never a second way of executing one ([ADR-0008](../adr/0008-local-batch-execution.md)).
How to write and run one: [tutorial](../tutorials/batch-experiments.md).

## Components

```text
 experiment.yaml
       │  experiments/loader.py: validate the file, every task and agent; definition hash; warnings
       ▼
 ExperimentExecutor (execution/experiments.py)            main thread: lifecycle + bookkeeping
   ├─ CREATED     persist experiment, manifest and every planned run (experiments, experiment_runs)
   ├─ PREPARING   host environment, agent preflight (e.g. Ollama model digest), task images
   ├─ RUNNING     BoundedScheduler (execution/scheduler.py)
   │                ├─ worker 1: own RunOrchestrator + own Docker client ──► run ──► run ...
   │                ├─ worker 2: ...                                         (one run at a time)
   │                └─ worker N
   │              completed runs ──queue──► main thread: outcome, progress, circuit breaker
   ├─ CANCELLING  Ctrl+C / SIGTERM / circuit breaker: no new runs; active runs cancel + clean up
   └─ FINALIZING  analysis/aggregate.py ──► ExperimentSummary ──► COMPLETED | CANCELLED | FAILED
       │
       ▼
 reporting/publish.py ──► results/: summary.json, runs.csv, failures.csv, report.md, plots/
                         (also later, from the database: `agentenv experiment report ID`)

 shared by all workers: SQLite (WAL, one locked connection) · artifact store (keyed by run ID)
```

## Lifecycle

```text
CREATED ─► PREPARING ─► RUNNING ─► FINALIZING ─► COMPLETED | CANCELLED | FAILED
   │           │           │            ▲
   │           │           └► CANCELLING┘
   └───────────┴────────────────────────┘   (preparation failed or cancelled: finalize directly)
```

`ExperimentStateMachine` (`experiments/status.py`) rejects any other transition; a
property-based test checks the invariants over random sequences. Every transition is
persisted with its timestamp (`experiments.history_json`), so the RUNNING duration, which
throughput is computed from, is a fact rather than an estimate.

The terminal status says why the experiment stopped:

- `completed`: every planned run was executed (whatever its outcome);
- `cancelled`: the user stopped it; runs that had completed are kept and reported;
- `failed`: preparation failed (Docker unreachable, a model not installed, an image that
  does not build), the circuit breaker tripped, or an internal error occurred.

A non-terminal experiment whose owner process has exited (for example after SIGKILL) is
shown as `interrupted`. Nothing claims to be running forever.

## The matrix

`expand_matrix` produces interleaved rounds: every task x agent combination once for
repetition 1, then for repetition 2, and so on. Within each (repetition, task) block the
agent order is shuffled with a seeded RNG. Slow drift over a long experiment (thermal
throttling, background load, a warming cache) therefore affects all agents alike instead of
whichever agent happened to run last. The order is deterministic for a given definition
and recorded in the manifest. Repetition *r* receives seed `base_seed + r - 1`, offered to
agents that support seeding (`AgentContext.seed`, recorded in each run's manifest).

## Concurrency and backpressure

`BoundedScheduler` starts `workers` threads that pull planned runs one at a time from a
lazily consumed iterator. At most `workers` runs (each up to two containers plus any model
calls) exist at any moment, whatever the matrix size: work that cannot start yet waits in
the iterator, not in Docker. The default is 1 worker. `validate`, `inspect` and `run` warn
when the worker count exceeds half the logical CPUs (or 8), and when `workers > 1` is
combined with host-side agents, whose loops and model calls compete for the same CPU.

Workers never touch progress state. They hand each completed run to the main thread, which
classifies it, updates the counts, checks the circuit breaker and notifies the observer
(the CLI's progress printer). A failure inside one run, even an unexpected exception from
an adapter, is reported for that run only. The failure-isolation benchmark checks this
under concurrency.

**Circuit breaker.** After 5 consecutive runs that failed for reasons outside the agents'
control (`infrastructure_error`, `agent_unavailable`, `incomplete`), scheduling stops and
the experiment ends `failed`. When Docker or the model server is down, every remaining
run would fail the same way.

## Cancellation

```text
Ctrl+C / SIGTERM / Ctrl+Break ──► CLI handler sets the CancellationToken (never raises)
   scheduler: workers take no new runs
   each active run: next stage boundary, agent wait, grader wait or sandbox command
                    ──► RunCancelledError ──► CLEANING_UP ──► CANCELLED
   executor: CANCELLING ──► FINALIZING ──► CANCELLED, completed runs summarised and published
```

The token is checked at every stage boundary except the one before artifact collection (a
graded run is kept), while waiting for the agent and the grader, and before every command
a session runs. Starting a sandbox cannot be interrupted, but it is bounded by the start
timeout, and a sandbox that finishes starting after cancellation is destroyed
immediately. A second Ctrl+C only reports that cleanup is in progress; a third exits at
once, and `agentenv cleanup` then removes the sandboxes of the dead process.

## Persistence

Migration 2 adds two tables next to `runs`, `events` and `artifacts`
([persistence](persistence.md)):

| Table | Holds |
|---|---|
| `experiments` | definition ID and hash, status and status history, owner (`<host hash>:<pid>`), workers, manifest JSON, summary JSON, output directory; migration 3 adds the experiment file's path (local only, never exported) |
| `experiment_runs` | one row per planned run: position, task, agent, repetition, seed, and the run ID once started |

All planned runs are written in one transaction when the experiment is created. A run ID
is linked to its entry just before the run creates its own row. An entry's state is never
stored: no run ID means not started; otherwise the run's own record says what happened.
`planned = started + not started` therefore holds by construction, also after a crash.

## Reproducibility snapshot

The experiment manifest (`manifest.json`, `experiments/manifest.py`) records:

- the AgentEnvForge version, the full validated definition and its hash. The hash covers
  the repetitions, the base seed, every task's definition plus the content of the files it
  ships (local source tree, image build context, hidden grader assets), and every agent's
  role, full spec, code hash and budget override. Editing a hidden test or the buggy
  project therefore changes it. It deliberately excludes the name, workers and output
  directory: they change how and where an evaluation runs, not what it evaluates. Hashes
  are over the bytes on disk, so a checkout with different line endings hashes
  differently;
- per task: definition path as written, schema version, hash, prepared image reference and
  ID, whether it was cached;
- per agent: role, adapter name, full spec (secret names only, never values), spec and code
  hashes, time-budget override, and the adapter's preflight provenance (for Ollama: server
  version, model digest, parameter size, quantisation, temperature, context size);
- the host: OS and release, architecture, CPU model, logical CPUs, memory, Python version,
  Docker version, Docker's OS, CPUs and memory.

It never contains a host name, user name, home path, MAC address or serial number. Each run's
own manifest adds its image ID, resolved commit, source tree hash and seed.

## Results directory

`reporting/publish.py` writes `experiment.yaml`, `manifest.json`, `environment.json`,
`summary.json`, `runs.csv`, `failures.csv`, `report.md`, `plots/*.png` (if matplotlib is
installed) and, with `--export-artifacts`, each run's result, grade, patch and transcript.
The directory is a pure function of the database, so `agentenv experiment report ID`
regenerates it at any time.

Every exported string is sanitised *structurally*, before serialisation: the experiment
file's directory becomes `.`, `AGENTENV_HOME` becomes `$AGENTENV_HOME` and the home directory
becomes `~`. Each path is matched in its native and POSIX spelling, case-insensitively on
Windows. A results directory can therefore be shared or committed.

How outcomes are counted, which intervals are used and when pass@k or p95 are withheld:
[ADR-0009](../adr/0009-experiment-statistics.md).

## Limitations

- One machine. Host-side agents share the Python interpreter and the CPU.
- No automatic retries of infrastructure failures: they are excluded from rates and flagged.
- An interrupted experiment cannot be resumed.
- Wilson intervals assume independent repetitions. Seeds and server caches can correlate them.
