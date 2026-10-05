# Non-functional requirements

| Quality | Requirement | How v0.1 meets it |
|---|---|---|
| **Isolation** | Untrusted code never runs on the host and never sees host files or secrets | Docker sandbox with no mounts, non-root, no capabilities, no network by default |
| **Reproducibility** | Every run records enough to recreate and compare it | Manifest with task/source/context/assets hashes, resolved commit, image ID; pinned example image |
| **Reliability** | No leaked containers on any failure path | Cleanup in `finally`; labels + `agentenv cleanup`; e2e tests assert zero leftovers |
| **Observability** | Any run can be explained after the fact | Persisted events, command log, per-run logs, stage timings |
| **Fail fast** | Malformed tasks fail before builds start | Three-pass validation |
| **Extensibility** | New backends/agents/graders/stores without core changes | Four protocols; orchestrator depends only on them |
| **Testability** | Core tests need no Docker, network or paid APIs | Fake backend; 140+ tests run offline in seconds |
| **Portability** | Linux, macOS, Windows hosts | Archive transfer (no bind mounts), pathlib, CI matrix |
| **Security defaults** | The safe choice is the default | network off, non-root, limits always set, secrets opt-in |
| **Performance** | Infrastructure overhead small relative to agent time | Cached images; measured on the example: ~0.4 s sandbox start, ~3 s total overhead |
| **Maintainability** | Understandable months later | Layering (ADR-0006), docstrings, ADRs, strict mypy, ruff |
