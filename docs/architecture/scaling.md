# Scaling

v0.1 executes one run per process invocation. The design leaves room to grow without
rewriting the core.

## What is already concurrency-safe

- **IDs** are ULIDs: unique across processes and machines without coordination.
- **Container names** (`agentenv-<run_id>-<role>`), **work directories**
  (`work/<run_id>`), **artifact namespaces** (`artifacts/<run_id>`) and **per-run log
  files** are all keyed by run ID, so parallel runs cannot touch each other's files.
- **SQLite** uses WAL plus a busy timeout. The repository serialises writes behind a lock,
  and events from several threads are tested.
- The **orchestrator** holds no per-run state on `self`; one instance can execute runs
  from several threads.
- **Image builds** are content-addressed, so 100 runs of one task build once.

## v0.2: local batch execution (planned)

`agentenv batch run tasks/ --agent a.yaml --repetitions 3 --workers 4`:

- task discovery (`discover_task_files` exists) and duplicate-ID detection (exists);
- a bounded worker pool, so at most N sandboxes run at once. The bound is the
  backpressure mechanism: work waits in the queue instead of overloading Docker;
- retries only for infrastructure failure reasons (never for a failed grade), each
  retry being a new run linked to the original;
- a batch ID and summary table (pass rate, failures by reason, latency percentiles).

100 tasks × 5 agents × 3 repetitions = 1,500 runs is a matter of queue length, not
architecture: each run is independent and keyed by its own ID.

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
