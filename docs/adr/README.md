# Architecture Decision Records

Each ADR records one significant decision: the context, the options considered, what was
decided, and its consequences and limitations. ADRs are immutable once accepted. A
changed decision gets a new ADR that supersedes the old one.

| ADR | Decision |
|---|---|
| [0001](0001-docker-as-initial-sandbox-backend.md) | Docker as the initial sandbox backend; files move only as archives |
| [0002](0002-yaml-versioned-task-specification.md) | YAML task files with a versioned, strictly validated schema |
| [0003](0003-agent-framework-agnostic-core.md) | Agent-agnostic core; small synchronous adapter protocol |
| [0004](0004-persistence-sqlite-and-artifact-store.md) | stdlib SQLite behind a repository; blobs in an artifact store |
| [0005](0005-event-model.md) | Append-only sequenced events with a single write path |
| [0006](0006-layering-and-dependency-direction.md) | Layering and dependency direction |
| [0007](0007-separate-grading-sandbox.md) | Grade in a fresh sandbox with hidden assets and protected paths |
| [0008](0008-local-batch-execution.md) | Local batch execution: bounded threads over the unchanged single-run orchestrator |
| [0009](0009-experiment-statistics.md) | Experiment statistics: scored outcomes, Wilson intervals, gated pass@k |

Template: copy an existing ADR and keep the sections *Context*, *Options considered*,
*Decision*, *Consequences*, *Limitations*.
