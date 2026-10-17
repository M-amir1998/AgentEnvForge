# Scaling

One process runs either a single run (`agentenv run`) or an experiment with bounded local
concurrency (`agentenv experiment run --workers N`, [experiments](experiments.md)).

## What is concurrency-safe

- **IDs** are ULIDs: unique across processes and machines without coordination.
- **Container names** (`agentenv-<run_id>-<role>`), **work directories**
  (`work/<run_id>`), **artifact namespaces** (`artifacts/<run_id>`) and **per-run log
  files** are all keyed by run ID, so parallel runs cannot touch each other's files.
- **SQLite** uses WAL plus a busy timeout. One connection per process is shared by all
  threads behind a lock; migrations take the write lock (`BEGIN IMMEDIATE`).
- The **orchestrator** holds no per-run state on `self`. Experiments still give each
  worker thread its own orchestrator and sandbox backend, so no Docker client is shared
  between threads.
- **Image builds** are content-addressed, and experiments prepare each task's image once
  before scheduling, so 100 runs of one task build once.
- **Sandboxes carry their owner** (`<host hash>:<pid>`): `agentenv cleanup` removes only
  sandboxes whose process is gone, never those of a run or experiment still executing in
  another terminal.

## Local batch execution (v0.2)

- A bounded worker pool: at most N runs, and so at most 2N sandboxes, exist at once. The
  bound is the backpressure mechanism: work waits in a lazily consumed iterator instead of
  overloading Docker.
- Matrix expansion over tasks x agents x repetitions, in interleaved rounds with a seeded
  shuffle per block, plus per-repetition seeds.
- Cancellation through a shared token; a circuit breaker for broken infrastructure.
- Per-experiment summaries with Wilson intervals, pass@k, latency distributions and a
  failure breakdown ([ADR-0009](../adr/0009-experiment-statistics.md)).

How much concurrency helps depends on the machine. `benchmarks/run_scaling.py` measures it
([results](../../benchmarks/results/scaling/scaling.md)). Retries of infrastructure
failures are not implemented: such runs are excluded from success rates and flagged.

100 tasks x 5 agents x 3 repetitions = 1,500 runs is a matter of queue length, not
architecture: each run is independent and keyed by its own ID, and every planned run is
persisted up front.

## Later: distributed execution

```text
        API / CLI ──► Coordinator ──► Queue ──► Workers (each: RunOrchestrator + Docker)
                                                   │
                                     PostgreSQL (RunRepository) · S3/MinIO (ArtifactStore)
```

What changes: a PostgreSQL `RunRepository`, an object-store `ArtifactStore`, a queue
(Redis/NATS/RabbitMQ; chosen when needed), and a worker entry point that wires the same
orchestrator. What does not change: the task schema, adapters, graders, the lifecycle.

Kubernetes, Ray or Celery will be evaluated against measured bottlenecks, not adopted ahead of them.
