# Functional requirements

Status legend: ✅ implemented in v0.1 · 🔜 planned (version) · ⛔ out of scope for now.

| ID | Requirement | Status |
|---|---|---|
| F1 | Load a versioned task specification and validate it before any expensive work | ✅ |
| F2 | Report validation problems as field / reason / received, all at once | ✅ |
| F3 | Obtain the source from a local directory or a git repository at a revision | ✅ |
| F4 | Record the resolved commit SHA and a content hash of the source tree | ✅ |
| F5 | Build or fetch the sandbox image; reuse identical builds | ✅ |
| F6 | Run the agent in an isolated, resource-limited sandbox | ✅ |
| F7 | Support provider-agnostic agents through an adapter interface | ✅ |
| F8 | Record every command the agent and grader execute | ✅ |
| F9 | Capture the agent's changes as a replayable patch | ✅ |
| F10 | Grade in a separate sandbox with hidden assets and protected paths | ✅ |
| F11 | Produce a structured grade (passed, score, test counts, components) | ✅ |
| F12 | Classify every failure; distinguish agent failure from infrastructure failure | ✅ |
| F13 | Always clean up sandboxes and temporary files; remove orphans on demand | ✅ |
| F14 | Persist runs, events and artifacts; inspect them from the CLI | ✅ |
| F15 | Diagnose the host environment (`doctor`) | ✅ |
| F16 | Inject secrets explicitly and redact them from all records | ✅ |
| F17 | Execute many tasks with bounded concurrency and summarise results | 🔜 v0.2 |
| F18 | Reproduce a past run from its manifest and verify input hashes | 🔜 v0.3 |
| F19 | HTTP API for submitting and inspecting runs | 🔜 v0.4 |
| F20 | Import tasks from external benchmarks (e.g. SWE-bench) and datasets (e.g. Hugging Face) | 🔜 v0.6 |
| F21 | Distributed workers, Kubernetes, multi-tenant auth, GPU scheduling, SaaS UI | ⛔ |
