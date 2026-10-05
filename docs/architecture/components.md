# Components

Each component lists *why it exists*, *what it owns*, and *the interface that separates
it* from the rest. Paths are relative to `src/agentenvforge/`.

## Domain (`domain/`)

- **Why:** a shared vocabulary that does not change when infrastructure does.
- **Owns:** `RunStatus` and the state machine (`status.py`); the failure taxonomy
  (`failures.py`); `CommandRequest`/`CommandExecution` (`commands.py`); `AgentResult`,
  `GradeResult`, `RunResult` (`results.py`); `ExecutionEvent` (`events.py`); `RunManifest`
  (`manifest.py`); ULID-style IDs (`ids.py`); UTC clock helpers (`clock.py`).
- **Interface:** immutable Pydantic models and plain functions. Imports no infrastructure.

## Task specification (`tasks/`)

- **Why:** fail before expensive work, with messages a task author can act on.
- **Owns:** schema v1 (`schema.py`), YAML reading with duplicate-key detection and
  error conversion (`documents.py`), loading, filesystem checks, hashing and discovery (`loader.py`).
- **Interface:** `load_task(path) -> LoadedTask` or `TaskValidationError` with all issues.

## Sandbox (`sandbox/`)

- **Why:** the isolation boundary, and the only place that knows about Docker.
- **Owns:** the `SandboxBackend` / `Sandbox` protocols (`base.py`); the Docker
  implementation (`docker_backend.py`); archive-based transfer helpers (`transfer.py`);
  `SandboxSession` (`session.py`), the deadline-enforcing, observed facade that agents and
  graders use.
- **Interface:** `prepare_image`, `create_sandbox`, `Sandbox.execute / put_archive /
  get_archive / destroy`, `remove_orphaned_sandboxes`.

## Workspace operations (`workspace/`)

- **Why:** moving and comparing directory trees safely, independent of any backend.
- **Owns:** tar creation and *safe* extraction of untrusted archives (`archive.py`),
  content fingerprints (`fingerprint.py`), host-side diffs producing `git apply`-compatible
  patches (`diff.py`).

## Sources (`sources.py`)

- **Why:** materialise the pristine workspace (local copy or git checkout) and record
  provenance (resolved commit, tree hash). Git runs with hooks disabled and `ext::` blocked.

## Agents (`agents/`)

- **Why:** run any agent without the core knowing what it is (ADR-0003).
- **Owns:** `AgentAdapter`, `AgentContext`, `AgentTask` (`base.py`); `CommandAgent`,
  `ManualAgent`, `NoopAgent` (`builtin.py`); agent spec files and construction (`spec.py`).

## Grading (`grading/`)

- **Why:** a structured verdict, not a boolean, kept separate from "could not judge".
- **Owns:** the `Grader` protocol and `GradingContext` (`base.py`); `PytestGrader`,
  `CommandGrader`, `CompositeGrader`, `build_grader` (`graders.py`); bounded JUnit parsing (`junit.py`).

## Execution (`execution/`)

- **Why:** the application service; the only code that sequences a run.
- **Owns:** `RunOrchestrator` and `RunRequest` (`orchestrator.py`); host-side handling of
  the solution between agent and grader: protected-path restoration and artifact
  selection (`solution.py`).

## Recording (`events/`, `persistence/`, `artifacts.py`, `telemetry/`)

- `RunRecorder` (`events/recorder.py`): the single write path for events, artifacts and
  the command log; applies redaction.
- `RunRepository` + `SqliteRunRepository` (`persistence/`): run metadata and events.
- `ArtifactStore` + `LocalArtifactStore` (`artifacts.py`): blob storage.
- `telemetry/logging.py`: JSON/text logs with `run_id`/`task_id` context and per-run log
  files. `telemetry/redaction.py`: secret masking.

## Configuration and diagnostics

- `config/settings.py`: one immutable `Settings`, resolved once at the process edge
  ([precedence](../development/configuration.md)).
- `diagnostics.py`: the checks behind `agentenv doctor`, returned as data.

## CLI (`cli/`)

- `app.py`: Typer commands and exit codes. `render.py`: all Rich output, including the
  live `ProgressPrinter` event sink. `wiring.py`: the composition root.
