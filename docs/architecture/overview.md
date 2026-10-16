# Architecture overview

AgentEnvForge turns a **task** (a repository in a known state, an environment, a way to
grade) plus an **agent** into a **run**: an isolated, limited, observed execution that ends
in a structured, reproducible result.

```text
                 CLI (agentenv)            future: HTTP API, queue worker
                      │
                      │      ExperimentExecutor: tasks x agents x repetitions, N worker threads,
                      │      each running one RunOrchestrator at a time (see experiments.md)
                      ▼
             ┌──────────────────┐   protocols only
             │ RunOrchestrator  │──────────────┬───────────────┬───────────────┐
             └──────────────────┘              │               │               │
               │ lifecycle state machine   SandboxBackend  RunRepository  ArtifactStore
               │ failure classification        │               │               │
               │ guaranteed cleanup         Docker         SQLite        local files
               ▼
   AgentAdapter ──► SandboxSession ──► Sandbox       Grader ──► SandboxSession ──► Sandbox
   (agent sandbox)                                   (fresh grading sandbox)
```

## Where things happen

| Question | Answer |
|---|---|
| Where is a task parsed and validated? | `tasks/loader.py` (document, schema, filesystem passes) using `tasks/schema.py` |
| Where are containers created? | `sandbox/docker_backend.py` → `DockerSandboxBackend.create_sandbox` |
| Where are resource limits enforced? | Docker cgroups via `create_sandbox`; per-command time via `DockerSandbox.execute` |
| Where is the agent invoked? | `execution/orchestrator.py` → `_stage_run_agent` |
| Where are commands executed? | `sandbox/session.py` (`SandboxSession.run`) → `Sandbox.execute` |
| Where are events generated? | `events/recorder.py` (`RunRecorder.emit`), called by the orchestrator and command observer |
| Where is grading performed? | `_stage_run_grader` builds the grading sandbox; `grading/graders.py` judges |
| Where are results persisted? | `persistence/sqlite.py`, through the `RunRepository` protocol |
| Where is cleanup handled? | `RunOrchestrator._cleanup` (always runs, in `finally`) and `agentenv cleanup` for orphans |
| Where are failures classified? | `domain/failures.py` (`FailureInfo.from_exception`, `terminal_status_for`) |
| Where are batches scheduled? | `execution/experiments.py` (`ExperimentExecutor`) on `execution/scheduler.py` ([experiments](experiments.md)) |
| Where are results aggregated? | `analysis/outcomes.py` (classification), `analysis/aggregate.py`, `analysis/stats.py` |
| Where are result files written? | `reporting/publish.py` (JSON, CSV, Markdown, plots), sanitised by `reporting/sanitize.py` |
| Where are retries implemented? | Nowhere yet, deliberately: a retry is a *new run* (see [scaling](scaling.md)) |

## Design principles

1. **Isolation first**: untrusted code only ever runs inside a sandbox; nothing from the
   host is mounted into one ([sandboxing](sandboxing.md), [security](security.md)).
2. **Separate agent failure from infrastructure failure**: `completed` + `passed=false` is
   a valid benchmark outcome; `failed_*` states are infrastructure or authoring problems.
3. **Everything observable**: every state change and command is an event, persisted
   immediately ([observability](observability.md)).
4. **Reproducible by construction**: pinned inputs, content hashes, resolved commit, image
   ID, all in a manifest ([persistence](persistence.md)).
5. **Small explicit interfaces** at the four extension points: `SandboxBackend`,
   `AgentAdapter`, `Grader`, `ArtifactStore`.

Continue with [components](components.md), the [execution lifecycle](execution-lifecycle.md)
and [experiments](experiments.md).
