# Implementation status

Last updated: 2026-10-05 (v0.1.0.dev0, after the local-LLM agent work). Keep this file in sync with reality in every PR.

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
- CLI: `version`, `doctor`, `task validate|inspect|schema`, `run`, `runs list|show|events|artifacts`, `cleanup`.
- Example tasks: `examples/basic` (hard, three bugs) and `examples/off-by-one` (easy), each
  with hidden tests and a reference patch.
- Real LLM agent example on local Ollama models (`examples/agents/ollama`, experimental);
  python adapters loadable from a spec-relative `path`; `--agent-timeout` override.
- First experiment with real models: `docs/experiments/2026-10-small-local-models.md`.
- 188 tests (unit, Docker integration, e2e). Strict mypy. Ruff.

## In progress

- Nothing. The v0.1 vertical slice is complete pending a first green CI run on GitHub.

## Planned

See [roadmap](../design/roadmap.md). Next: batch execution (v0.2).

## Known limitations

- Disk usage of sandboxes is not limited.
- Timed-out in-process (`python`) adapters cannot be killed; their thread is abandoned
  after their session is revoked and sandbox destroyed.
- Solution code runs inside the grading process and can, if malicious, tamper with it.
- A second Ctrl+C during cleanup can interrupt cleanup; `agentenv cleanup` recovers.
- Runs interrupted by SIGKILL remain in a non-terminal status (no automatic reconciliation yet).
- The agent's configuration is recorded only by name in the manifest.
- Image tags of prebuilt images are resolved from the local cache if present, without re-pulling.
- Requires GNU `timeout` and `sleep` in task images.
- The Ollama agent is single-run-validated only (n=1 per model/task); no pass-rate statistics yet.

## Technical debt

- The Docker backend's error paths (daemon dying mid-run, watchdog firing) are only
  partially covered by tests (74% for that module).
- `cli/app.py` `runs show` rendering is inline; it should move into `render.py` when the
  API adds a second consumer of the same view.
- No test yet runs the CLI on macOS with Docker (CI covers Linux Docker; Windows was verified manually).
