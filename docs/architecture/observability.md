# Observability

Three channels, each with one job:

| Channel | Audience | Where |
|---|---|---|
| **Events** | machines and humans investigating a run | SQLite `events` table, `events.jsonl` artifact, `agentenv runs events` |
| **Logs** | operators debugging AgentEnvForge itself | stderr (`--log-level`, `--log-format text|json`), per-run `logs/agentenv.log` |
| **CLI output** | the person running the command | Rich rendering in `cli/render.py` only |

## Events

See ADR-0005 for the model. Event types (`domain/events.py`):

```text
run.created  run.status_changed  run.completed  run.failed  task.validated
source.preparation.started|completed  sandbox.build.started|completed  sandbox.started
sandbox.cleanup.started|completed|failed  setup.started|completed  agent.started|completed
command.started|completed  solution.captured  grader.started|completed  artifact.stored
```

`command.*` events carry the role (`agent`, `setup`, `grader`), the shell-quoted command,
exit code, timeout flag, output sizes and duration. Full output goes to the `commands.jsonl` artifact.

## Logs

Records carry `run_id`, `task_id` (and `sandbox` where relevant) from a `contextvars`
context, which is propagated into worker threads by `call_with_timeout`. Each run also
writes a DEBUG-level JSON log filtered to its own `run_id` into its artifacts, so
concurrent runs never mix their logs.

## Metrics available today

From `RunResult.stage_timings` and event durations: duration of every lifecycle stage,
image build time and cache hit, sandbox start latency, agent runtime and command count,
grader runtime, cleanup time, and the timeout/failure category of every run. These are
queryable from SQLite now.

## Roadmap

- Prometheus exposition (counters per terminal status/reason, histograms per stage) from
  the API process.
- OpenTelemetry: one trace per run, a span per stage, events as span events. The event
  model maps onto this directly.
- Container CPU/memory usage sampling via the Docker stats API.
