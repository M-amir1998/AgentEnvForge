# Roadmap

Versions follow semantic versioning; before 1.0, minor versions may change interfaces
(noted in the changelog). Order may change based on what real users need.

| Version | Theme | Key deliverables |
|---|---|---|
| **v0.1** | Local single-task execution | task schema v1, Docker sandbox, command/manual/noop/python agents, pytest/command/composite graders, lifecycle + events, SQLite + artifacts, `doctor`, e2e example ✅ |
| **v0.2** | Batch execution | experiment schema v1 (tasks x agents x repetitions, roles, option overrides), bounded worker pool with backpressure, cooperative cancellation, experiment persistence and reproducibility snapshot, Wilson intervals / pass@k / latency summaries, JSON/CSV/Markdown/plot exports, infrastructure, failure-isolation and local-LLM benchmarks (in progress on `milestone/v0.2`). Deferred: infrastructure-only retries, resuming interrupted experiments, `runs compare` |
| v0.3 | Better graders and agents | `agentenv reproduce`, recorded agent configuration, diff-based graders, LLM-provider example adapters in `examples/` (not in the core), more example task types |
| v0.4 | API and observability | FastAPI service over the same application services, Prometheus metrics, OpenTelemetry traces, container resource sampling |
| v0.5 | Distributed prototype | PostgreSQL repository, S3/MinIO artifact store, queue + worker process |
| v0.6 | Benchmark import/export | SWE-bench-style task importer, Hugging Face dataset loader, result dataset export |
| v0.7 | Hardened sandboxing | gVisor runtime option, disk quotas where supported, egress allow-lists, separate grader user, read-only assets |
| v1.0 | Stable extension APIs | frozen `SandboxBackend`, `AgentAdapter`, `Grader`, `ArtifactStore`, schema v1 compatibility guarantees |

Explicitly not planned: SaaS frontend, billing, enterprise RBAC, multi-region deployment.
