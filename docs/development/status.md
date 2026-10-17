# Implementation status

Last updated: 2026-10-17 (v0.1.0.dev0, Milestone 8: batch experiments, on branch `milestone/v0.2`). Keep this file in sync with reality in every PR.

## Implemented (tested)

- Task schema v1: validation passes, actionable errors, duplicate-key and duplicate-ID
  detection, JSON Schema, definition hash, reproducibility warnings.
- Sources: local directories; git with pinned/resolved commit, subdirectories, safe options.
- Docker backend: hardened containers (see sandboxing.md), content-addressed image cache,
  timeouts with watchdog, bounded output, archive transfer, orphan removal.
- Agents: `command`, `python` (import path), `manual`, `noop`; agent spec files; secrets.
- Graders: `pytest` (JUnit), `command`, composite `all`/`weighted`.
- Lifecycle: state machine (property-tested), orchestrator, failure taxonomy, guaranteed
  cleanup, Ctrl+C/SIGTERM handling.
- Fresh grading sandbox with hidden assets and protected-path restoration.
- Events, JSON/text logging with run context, per-run log files, secret redaction.
- SQLite repository with migrations; local artifact store; manifest; patch capture.
- CLI: `version`, `doctor`, `task validate|inspect|schema`, `run`, `runs list|show|events|artifacts`,
  `cleanup [--all]`, `experiment validate|inspect|run|report`, `experiments list|show`.
- Batch experiments ([architecture](../architecture/experiments.md)): experiment schema v1,
  matrix expansion with seeds and an interleaved order, bounded worker pool with
  backpressure, failure isolation, cooperative cancellation, circuit breaker, experiment
  state machine (property-tested), persistence of every planned run, `interrupted` status,
  owner-aware orphan cleanup.
- Experiment statistics ([ADR-0009](../adr/0009-experiment-statistics.md)) and the results
  directory: JSON, CSV, Markdown report, headless plots, path sanitisation, regeneration.
- Benchmarks: scaling, failure isolation (also in CI), hosted Claude models and tiny local
  models (stopped early), with committed results ([benchmarks](../../benchmarks/README.md)).
- Example tasks: `examples/basic` (hard, three bugs), `examples/lru-cache` (medium, two bugs)
  and `examples/off-by-one` (easy), each with hidden tests and a reference patch; oracle,
  failing, sleeper and crasher test agents.
- Real LLM agent example on local Ollama models (`examples/agents/ollama`, experimental);
  python adapters loadable from a spec-relative `path`; `--agent-timeout` override.
- Claude Code agent example for hosted Claude models (`examples/agents/claude-code`,
  experimental): sandbox-only tools over a per-run MCP server, isolation from the user's
  Claude Code configuration, usage guard.
- First experiment with real models: `docs/experiments/2026-10-small-local-models.md`.
- 188 tests (unit, Docker integration, e2e). Strict mypy. Ruff.

## In progress

- Milestone 8 (batch experiments) is complete on `milestone/v0.2`, pending review and a green
  CI run on GitHub. CI status could not be checked from the development machine, so `v0.1.0`
  has not been tagged and the version is still `0.1.0.dev0`.

## Planned

See [roadmap](../design/roadmap.md). Next: retries of infrastructure failures, resuming
interrupted experiments, `agentenv reproduce`.

## Known limitations

- Disk usage of sandboxes is not limited.
- Timed-out in-process (`python`) adapters cannot be killed; their thread is abandoned
  after their session is revoked and sandbox destroyed.
- Solution code runs inside the grading process and can, if malicious, tamper with it.
- A second Ctrl+C during cleanup can interrupt cleanup; `agentenv cleanup` recovers.
- Runs interrupted by SIGKILL remain in a non-terminal status (no automatic reconciliation
  yet); experiments are shown as `interrupted` when their process is gone.
- For single runs, the agent's configuration is recorded only by name; experiment runs
  record the full agent spec, seed and experiment link.
- Experiments run on one machine; host-side agents share the CPU and the Python interpreter
  (`workers > 1` with such agents triggers a warning). Interrupted experiments cannot be
  resumed, and infrastructure failures are not retried.
- Starting a sandbox cannot be cancelled mid-way; it is bounded by the start timeout.
- Image tags of prebuilt images are resolved from the local cache if present, without re-pulling.
- Requires GNU `timeout` and `sleep` in task images.
- Model results are small-sample: 3 repetitions on 3 example tasks. The local-ollama
  benchmark was stopped after 8 of 45 runs (CPU inference too slow on the development
  laptop). Claude Code takes no seed, so its repetitions cannot be reproduced exactly.
- `test_hanging_agent_times_out_and_is_cleaned_up` failed once without a reproducible cause
  ([diagnosis](flaky-test-diagnosis.md)).

## Technical debt

- The Docker backend's error paths (daemon dying mid-run, watchdog firing) are only
  partially covered by tests (74% for that module).
- `cli/app.py` `runs show` rendering is inline; it should move into `render.py` when the
  API adds a second consumer of the same view.
- No test yet runs the CLI on macOS with Docker (CI covers Linux Docker; Windows was verified manually).
