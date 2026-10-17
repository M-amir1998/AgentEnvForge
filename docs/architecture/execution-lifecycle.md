# Execution lifecycle

Implemented by `RunOrchestrator` (`execution/orchestrator.py`) on top of the state machine
in `domain/status.py`.

```text
CREATED
  → VALIDATING            task file unchanged since load (definition hash); backend reachable
  → PREPARING_SOURCE      copy local dir / git clone + checkout; tree hash, resolved SHA
  → BUILDING_SANDBOX      build Dockerfile (cached by build-context hash) or fetch image
  → STARTING_SANDBOX      start agent sandbox, verify it, copy workspace in, run setup
  → RUNNING_AGENT         adapter acts through a deadline-bound SandboxSession
  → CAPTURING_SOLUTION    copy workspace out, diff → solution.patch; destroy agent sandbox
  → RUNNING_GRADER        fresh sandbox: solution + restored protected paths + hidden assets
  → COLLECTING_ARTIFACTS  commands.jsonl, manifest.json
  → CLEANING_UP           destroy every sandbox, delete work dirs   (always, even on failure)
  → COMPLETED | FAILED_VALIDATION | FAILED_SOURCE | FAILED_BUILD | FAILED_SANDBOX
    | FAILED_AGENT | FAILED_GRADER | TIMED_OUT | CANCELLED | INTERNAL_ERROR
```

Any active state may jump straight to `CLEANING_UP`. Terminal states are reachable
**only** from `CLEANING_UP`, which is what guarantees cleanup was attempted for every
finished run. Illegal transitions raise `InvalidTransitionError`; a property-based test
(`tests/unit/test_status.py`) checks the invariants over random transition sequences.

## Failure classification

The stage loop has one deliberate catch-all boundary. Any exception, including
unexpected bugs and `KeyboardInterrupt`, becomes a `FailureInfo` (reason, stage,
message, error type, details). `terminal_status_for(reason)` picks the terminal state:

| Raised | Reason | Terminal status |
|---|---|---|
| `TaskValidationError` | `task_validation` | `failed_validation` |
| `SourcePreparationError` | `source_preparation` | `failed_source` |
| `SandboxBuildError` / `SandboxBuildTimeoutError` | `sandbox_build(_timeout)` | `failed_build` |
| `SandboxStartError`, `SetupCommandError`, other `SandboxError` | ... | `failed_sandbox` |
| `AgentError` (adapter crashed or misbehaved) | `agent_error` | `failed_agent` |
| `AgentUnavailableError` (the agent's service, e.g. a model server, is unreachable or failing) | `agent_unavailable` | `failed_agent` |
| `AgentTimeoutError` | `agent_timeout` | `timed_out` |
| `GraderError` / `GraderTimeoutError` | `grader_error` / `grader_timeout` | `failed_grader` / `timed_out` |
| `KeyboardInterrupt`, SIGTERM, a cancelled token (`RunCancelledError`) | `cancelled` | `cancelled` |
| anything else | `internal` (traceback logged) | `internal_error` |

Unexpected exceptions are logged with their traceback. Classified errors chain their
cause (`raise ... from error`), so the root cause is never lost.

### Agent outcomes are not run failures

An agent that exits non-zero has *run*. Its workspace is still graded, and the result
records `agent.outcome = failed`. `FAILED_AGENT` is reserved for adapters that could not
run the agent at all (exceptions, misuse of the session).

## Timeouts

| Boundary | Configured by | Enforcement |
|---|---|---|
| git clone/checkout | `timeouts.source_seconds` | `subprocess` timeout |
| image build/pull | `timeouts.build_seconds` | worker thread + deadline |
| sandbox start | `sandbox_start_timeout_seconds` setting | worker thread + deadline |
| setup (total) | `timeouts.setup_seconds` (per command: `setup[].timeout_seconds`) | session deadline |
| agent (total) | `timeouts.agent_seconds` | session deadline, each command clipped to remaining time; adapter abandoned after a grace period and its sandbox destroyed |
| single agent command | `timeouts.command_seconds` | in-container GNU `timeout` (TERM, then KILL after 5 s) plus a host watchdog that kills the container |
| grader (total) | `timeouts.grader_seconds` | session deadline + grace |
| sandbox removal | `cleanup_timeout_seconds` setting | worker thread + deadline |

## Cleanup guarantees

- Cleanup runs in `finally`, so it happens after agent crashes, grader crashes, timeouts,
  Ctrl+C and unexpected exceptions.
- Each sandbox is destroyed independently; a failure to remove one is recorded in
  `result.cleanup.errors` and emits `sandbox.cleanup.failed`, but **never replaces the
  run's primary failure**.
- If the whole process is killed (SIGKILL, power loss), containers survive. They are
  labelled `io.agentenvforge.managed=true` and `io.agentenvforge.run-id=<id>`, and
  `agentenv cleanup` removes them. The run row remains in a non-terminal status, which
  identifies the interrupted execution.

## Cancellation

A run can be cancelled in two ways:

- **Single runs** (`agentenv run`): Ctrl+C raises `KeyboardInterrupt` and SIGTERM raises
  `RunCancelledError` in the main thread, which runs the orchestrator.
- **Runs inside an experiment** receive a shared `CancellationToken` in their `RunRequest`.
  The orchestrator checks it at every stage boundary (except the one before
  `COLLECTING_ARTIFACTS`, so a graded run is kept), while waiting for the agent and the
  grader, and before every command a session runs. Any error raised because the token was
  set is classified as `cancelled`, not as an agent or grader failure.

Either way the run passes through `CLEANING_UP` and ends `CANCELLED`. Starting a sandbox
cannot be interrupted, but it is bounded by `sandbox_start_timeout_seconds`. A sandbox that
finishes starting after the run gave up on it is destroyed immediately.

## Retry, resume, reproduce

These are three distinct concepts. None is implemented in v0.1 beyond the data they need:

- **retry**: execute the same request again as a *new* run with a new ID (planned for
  batch execution, for infrastructure failures only);
- **resume**: continue an interrupted run. Not planned: sandboxes are disposable, so
  resuming would mean trusting partial state;
- **reproduce**: re-execute a past run from its manifest and compare input hashes
  (`agentenv reproduce`, planned).
