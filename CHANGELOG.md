# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may break interfaces).

## [Unreleased]

### Added

- **Batch experiments** (Milestone 8): `agentenv experiment validate|inspect|run|report`
  and `agentenv experiments list|show`.
  - Experiment schema v1: tasks x agents x repetitions; agent roles (`subject`,
    `reference`, `baseline`); per-agent time budgets and option overrides; base seed;
    output directory.
  - Bounded local concurrency (`--workers`) with backpressure, failure isolation, a
    circuit breaker for broken infrastructure, and an interleaved, seeded run order.
  - Cooperative cancellation: Ctrl+C / SIGTERM stops scheduling, cancels active runs
    (which still clean up) and keeps completed results.
  - Experiment persistence (SQLite migrations 2 and 3): status history, every planned run
    linked to the run that executed it, and an `interrupted` status for experiments whose
    process died.
  - Reproducibility snapshot: definition hash over tasks, agent specs and code; agent
    preflight provenance (e.g. Ollama model digest and quantisation); prepared images;
    host hardware and software.
  - Statistics (ADR-0009): scored versus excluded outcomes by stage, 95% Wilson intervals,
    identical-repetition detection, gated pass@k (Chen et al. estimator), latency
    distributions with censoring, validity checks for reference and baseline agents.
  - Results directory: `summary.json`, `runs.csv`, `failures.csv`, `report.md`, plots
    (optional `plots` extra, matplotlib), manifest and environment, optional per-run
    evidence. Every exported path is sanitised.
- Benchmarks (`benchmarks/`): infrastructure scaling (workers 1/2/4), failure isolation
  with an expectation checker (also run in CI), hosted Claude models, and tiny local Ollama
  models on CPU (stopped early), with committed, sanitised results.
- Claude Code agent example (`examples/agents/claude-code`): hosted Claude models (Haiku,
  Sonnet, Opus) run through `claude -p` with every built-in tool disabled, acting only
  through the sandbox via a per-run MCP server on 127.0.0.1; isolated from the user's
  Claude Code configuration, killed when the run's budget ends or the run is cancelled,
  optional usage guard, token-free preflight that records no account details.
- Medium example task `examples/lru-cache`. Oracle, failing, sleeper and crasher test
  agents. An Ollama spec for qwen2.5-coder 0.5b.
- Ollama agent: preflight (server, model, digest), per-run seeds, generation timings,
  `agent_unavailable` for unreachable or failing servers.
- `agentenv cleanup --all`; by default, cleanup only removes sandboxes whose owning process
  has exited. `agentenv run` checks an agent's readiness before building a sandbox.
- ADR-0008 (local batch execution), ADR-0009 (experiment statistics), architecture page
  and tutorial for experiments.
- Real LLM coding agent on local Ollama models (`examples/agents/ollama`): host-side tool
  loop, native and text tool-call parsing, transcripts and token statistics; plus specs for
  qwen2.5-coder 1.5b/3b/7b and a first experiment report.
- Easy example task `examples/off-by-one`.
- `python` agent specs accept `path:` to load an adapter that lives next to its spec.
- `agentenv run --agent-timeout` overrides the agent time budget; recorded in the manifest.

### Fixed

- Container leaks: a sandbox whose post-start verification failed, or that finished starting
  after its run had given up, is now always removed.
- Docker transport errors (requests, OSError, Windows named-pipe errors) are reported as
  sandbox errors, not internal errors. The exec exit status is polled, and the readiness
  probe retries.
- Concurrent processes can no longer race on a schema migration, and setting a run's log
  file no longer changes the log level of other threads.
- The recorder refuses events after a run is finalised, and a failing event sink no longer
  breaks a run.
- `agentenv run` passes secrets of every adapter type to redaction, and prints
  manual-agent instructions without interpreting them as markup.
- Exported JSON evidence is sanitised value by value: JSON-escaped Windows paths
  (`C:\\Users\\...`) and paths turned into directory names (`C--Users-...`) no longer
  survive in shared result directories.
- Reports describe repetitions with identical patches as "identical repetitions" instead of
  calling the agent deterministic (a sampled model can converge on the same fix).
- Sandboxes are now identified by the run-id label. Images built by AgentEnvForge no longer
  carry the container `managed` label, which Docker copied onto unrelated containers started
  from those images, so `agentenv cleanup` could have removed them.

### Initial slice

- Versioned task specification (schema v1) with three-pass validation, actionable
  `field / reason / received` errors, duplicate-key and duplicate-ID detection, and a
  generated JSON Schema (`schemas/task-v1.schema.json`).
- Local-directory and git sources; resolved commit SHA and tree hash recorded.
- Docker sandbox backend: no host mounts, non-root user, all capabilities dropped,
  `no-new-privileges`, CPU/memory/PID limits, network disabled by default, per-command
  timeouts with a watchdog, bounded output, content-addressed image cache.
- Agent adapters: `command`, `python`, `manual`, `noop`; agent spec files; secret injection
  with redaction.
- Graders: `pytest` (JUnit-based), `command`, and composite aggregation (`all`, `weighted`).
- Grading in a fresh sandbox with hidden assets and protected-path restoration.
- Run lifecycle state machine, failure taxonomy and guaranteed cleanup (including Ctrl+C).
- Structured events, JSON/text logs with run context, per-run log files.
- SQLite persistence with migrations, local artifact store, reproducibility manifest,
  `git apply`-compatible solution patches.
- CLI: `version`, `doctor`, `task validate|inspect|schema`, `run`,
  `runs list|show|events|artifacts`, `cleanup`.
- Self-contained example task (`examples/basic`) with a scripted reference agent.
- Architecture documentation, ADRs 0001-0007, tutorials, CI workflow.
