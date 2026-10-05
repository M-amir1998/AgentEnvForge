# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may break interfaces).

## [Unreleased]

### Added

- Real LLM coding agent on local Ollama models (`examples/agents/ollama`): host-side tool
  loop, native and text tool-call parsing, transcripts and token statistics; plus specs for
  qwen2.5-coder 1.5b/3b/7b and a first experiment report.
- Easy example task `examples/off-by-one`.
- `python` agent specs accept `path:` to load an adapter that lives next to its spec.
- `agentenv run --agent-timeout` overrides the agent time budget; recorded in the manifest.

### Fixed

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
