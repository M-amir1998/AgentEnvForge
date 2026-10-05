# ADR-0006: Layering and dependency direction

- Status: accepted
- Date: 2026-10-04

## Context

Docker, SQLite, Typer and Rich details must not spread through the codebase, or replacing
any of them (a new sandbox backend, PostgreSQL, an HTTP API) would mean a rewrite.

## Decision

Pragmatic layering, enforced by review (and checkable with `grep`):

```text
cli/                  presentation: argument parsing, Rich rendering, exit codes
cli/wiring.py         composition root: the only place concrete classes are chosen
execution/            application service: RunOrchestrator drives the lifecycle
agents/ grading/      extension points + built-ins (depend on sandbox.session, domain)
sandbox/ persistence/ artifacts.py sources.py telemetry/   infrastructure adapters
tasks/ workspace/     specification parsing; pure tree operations
domain/               models, state machine, failures, events: no infrastructure imports
```

Rules:

- `domain` imports nothing from the package except other `domain` modules (Pydantic is
  allowed as a modelling library).
- Only `sandbox/docker_backend.py` imports the Docker SDK; only `persistence/sqlite.py`
  imports `sqlite3`; only `cli/` imports Typer and Rich.
- The orchestrator depends on protocols and receives implementations through its constructor.

We deliberately do *not* implement textbook Clean Architecture (no use-case classes per
command, no DTO mapping layers). Pydantic models are shared across layers because they are
plain immutable data.

## Consequences

Tests substitute a fake backend and run the complete orchestrator without Docker. A
future API server reuses `RunOrchestrator` with its own wiring.
