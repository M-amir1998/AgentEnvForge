# ADR-0008: Local batch execution on top of the single-run orchestrator

- Status: accepted
- Date: 2026-10-16

## Context

Evaluating agents means running a matrix: tasks x agents x repetitions, typically tens to
thousands of runs. A single run already has a careful lifecycle (state machine, timeouts,
guaranteed cleanup, persistence, artifacts). Batch execution must add scheduling,
concurrency, cancellation and bookkeeping without weakening any of that, on one machine,
without new infrastructure.

Constraints that shaped the decision:

- Runs are dominated by waiting: Docker API calls, containers executing, HTTP calls to a
  model server. Little of a run's time is Python bytecode.
- Host-side agents (e.g. the Ollama adapter) run their loop in the AgentEnvForge process.
- Docker-py clients are not documented as thread-safe; SQLite needs one writer at a time.
- Ctrl+C must never leave containers running, and a killed process must not leave a batch
  that claims to be running forever.

## Options considered

1. **External job system** (Celery/Redis, Ray, a queue service). Distributed by design,
   but a broker or cluster is a heavy dependency for a local tool, and it would duplicate
   lifecycle logic the orchestrator already owns.
2. **Process pool.** True CPU parallelism for host-side agents, but adapters and specs
   must be picklable, Windows `spawn` start-up is slow, signals and cleanup have to be
   coordinated across processes, and SQLite writes come from many processes.
3. **asyncio.** No asynchronous Docker client is available; the orchestrator, sessions,
   graders and adapters would all need rewriting.
4. **Threads, each running the unchanged single-run orchestrator.**

## Decision

Option 4.

- `BoundedScheduler` starts `workers` threads that pull planned runs one at a time from a
  lazily consumed iterator: never more than `workers` runs, containers or model calls in
  flight, however large the matrix (backpressure). A failure inside one run is captured
  and reported for that run only.
- Each worker thread builds its own `RunOrchestrator` and sandbox backend (and therefore
  its own Docker client). The SQLite database (WAL, one connection guarded by a lock,
  `BEGIN IMMEDIATE` transactions) and the artifact store are shared.
- All bookkeeping (progress, outcome classification, the circuit breaker, status changes)
  happens on the main thread, which receives completed runs through a queue. The main
  thread stays responsive to signals, which Python only delivers to it.
- Cancellation is cooperative: one `CancellationToken` per experiment, checked at every
  stage boundary of a run, while waiting for the agent and the grader, and before every
  sandbox command. A cancelled run still passes through cleanup and ends `CANCELLED`.
  Sandbox creation is not interruptible (it is bounded by the start timeout); a sandbox
  that finishes starting after cancellation is destroyed immediately.
- The CLI's signal handler only sets the token. Raising from a handler at an arbitrary
  point of the main thread could abandon running workers; a third Ctrl+C exits at once.
- Every planned run is persisted when the experiment is created, with its matrix position,
  repetition and seed. A run is linked to its entry just before it starts. Entry state is
  derived (no run: not started; otherwise the run's own record), so planned, started and
  finished counts always reconcile, also after a crash.
- Experiments and sandboxes record their owner (`<host hash>:<pid>`). A non-terminal
  experiment whose owner process is gone is reported as `interrupted`; `agentenv cleanup`
  removes only sandboxes whose owner is gone.
- A circuit breaker stops scheduling after 5 consecutive runs that failed for reasons
  outside the agents' control (infrastructure, model server unavailable), instead of
  failing the rest of the plan the same way.
- The matrix runs in interleaved rounds (every task x agent once per repetition), with the
  agent order shuffled per block from a seed. Drift over a long experiment (thermal
  throttling, background load, a warming cache) then affects all agents alike.

## Consequences

- Batch runs are ordinary runs: `agentenv runs show <run id>` works for every run, with a
  link back to its experiment, seed and position.
- No new runtime dependency, broker or daemon. Threads make sharing the database and
  observing progress straightforward.
- The orchestrator gained only optional inputs (run ID, seed, cancellation token,
  experiment link); single runs behave as before.

## Limitations

- Host-side agents share the Python interpreter (GIL) and the machine's CPU. `validate`,
  `inspect` and `run` warn when `workers > 1` is combined with host-side agents, because
  contention changes their timings and can change their results.
- One machine only. Distributed workers would reuse the executor's interfaces (scheduler,
  repositories, orchestrator factory) but are not implemented.
- An interrupted experiment cannot be resumed yet; it can be reported (`experiment
  report`) and its leftover sandboxes removed (`cleanup`).
