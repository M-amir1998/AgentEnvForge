# ADR-0004: Initial persistence: stdlib SQLite behind a repository, blobs in an artifact store

- Status: accepted
- Date: 2026-10-04

## Context

Runs, status transitions, events and artifact references must be queryable
(`runs list`, `runs show`), survive crashes, and later move to a shared database for
multi-worker execution. Artifacts (logs, patches, reports) can be megabytes each.

## Options considered

1. **JSON files per run**: zero dependencies, but no querying, no atomic multi-record
   updates, and concurrent writers race.
2. **SQLAlchemy + Alembic over SQLite**: portable to PostgreSQL, but a large dependency for
   three tables accessed by one class.
3. **stdlib `sqlite3` behind a `RunRepository` protocol**, with versioned migrations.
4. **Blobs in database columns**: simple, but bloats the database and couples blob storage to it.

## Decision

Option 3 for metadata, plus a separate `ArtifactStore` protocol for blobs.

- `RunRepository` (`agentenvforge.persistence.repository`) is the interface; the
  orchestrator never sees SQL. `SqliteRunRepository` implements it.
- Schema changes are append-only migrations in `MIGRATIONS`, applied in order and recorded
  in `schema_migrations`, so any database is at a known version.
- WAL journaling, a busy timeout and a connection lock make concurrent writers (agent
  threads emitting events, a parallel `runs list`) safe.
- `LocalArtifactStore` writes `artifacts/<run_id>/<name>` atomically (temp file + rename).
  The database stores only name, size and SHA-256.

## Consequences

- No ORM dependency. A PostgreSQL implementation means writing one class against the same
  protocol. Alembic becomes worthwhile when there are two backends to migrate in step.
- Events are written as they happen, so a crashed process still leaves a trace.
- Artifact names are validated so a run can never write outside its namespace.

## Limitations

SQLite is single-host. Multi-machine workers will need the PostgreSQL implementation and
an object store (S3/MinIO) implementation of `ArtifactStore`.
