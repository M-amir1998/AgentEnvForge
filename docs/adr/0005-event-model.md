# ADR-0005: An append-only, sequenced event model with a single write path

- Status: accepted
- Date: 2026-10-04

## Context

Debugging a failed run, measuring stage latencies and later exporting traces/metrics all
need a precise, ordered record of what happened, including from runs whose process died.

## Options considered

1. **Logs only**: easy, but unstructured and hard to query reliably.
2. **Final result only**: loses everything about *how* a run got there.
3. **Structured events persisted as they occur**, with logs and UI as consumers.

## Decision

`ExecutionEvent` (`agentenvforge.domain.events`) has `event_id`, `run_id`, `task_id`, a
per-run strictly increasing `sequence`, UTC `timestamp`, a stable `event_type`
(`<subject>.<verb>`, e.g. `sandbox.build.completed`), the run `status` at emission time,
optional `duration_ms`, and **scalar-only** `data`.

`RunRecorder` is the single write path: it assigns the sequence, redacts secrets,
**persists first, then notifies sinks** (log output, CLI progress). Bulky data never goes in
events; it goes in artifacts, and an `artifact.stored` event references it by name.

## Consequences

- A sink can never display something that is not stored.
- `sequence` gives a total order even when timestamps collide.
- The model maps directly onto OpenTelemetry spans/events and Prometheus counters later.
- Scalar metadata keeps events cheap to store and index.

## Limitations

Events are per run. Cross-run aggregation (batch summaries) is computed by querying,
not by streaming.
