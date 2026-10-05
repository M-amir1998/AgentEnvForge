# Persistence and reproducibility

Decision record: ADR-0004.

## Layout of `AGENTENV_HOME` (default `~/.agentenvforge`)

```text
agentenv.db                 SQLite: runs, events, artifacts (metadata), schema_migrations
artifacts/<run_id>/         every artifact of a run (see below)
work/<run_id>/              temporary per-run working trees, deleted at cleanup
                            (kept with --keep-workspace)
config.toml                 optional settings file
```

## Tables

- `runs`: one row per run: task, agent, current status, timestamps, passed, score,
  failure reason, duration, `manifest_json`, `result_json`.
- `events`: append-only, unique `(run_id, sequence)`.
- `artifacts`: `(run_id, name)` unique, with size and SHA-256. Content lives in the store.

## Artifacts of a run

| Name | Content |
|---|---|
| `result.json` | the final `RunResult` |
| `manifest.json` | reproducibility manifest (below) |
| `events.jsonl` | all events |
| `commands.jsonl` | every command with role, argv, cwd, exit code, stdout/stderr (redacted) |
| `solution.patch` | the agent's changes, `git apply`-compatible |
| `changed_files.json` | added / modified / deleted paths, plus unsafe entries skipped |
| `agent/stdout.log`, `agent/stderr.log` | command agent output |
| `grade.json`, `grader/<name>/...` | verdict, grader output, JUnit report |
| `sandbox/build.log` | image build output (when built) |
| `workspace/<path>` | files matched by the task's `artifacts.include` (size-limited) |
| `logs/agentenv.log` | the run's own JSON log |

## The manifest

`RunManifest` (`domain/manifest.py`) records: AgentEnvForge version, Python version,
platform, task ID/path/schema version, **task definition hash** and the full validated
definition, agent name, backend name/version, source type, **tree hash**, repository,
requested revision and **resolved commit**, image reference, **image ID**, repo digests,
**build-context hash**, and **grader-assets hash**.

Two runs are comparable when these hashes match. Determinism the framework cannot
provide (agent sampling temperature, network content when enabled, wall-clock-dependent
code) must be controlled by the agent or task. The agent's own configuration is recorded
only by name today. Recording full agent configuration is on the roadmap.

## Timestamps and IDs

All timestamps are timezone-aware UTC, serialised as ISO 8601. Run, event and artifact
IDs are ULIDs with a type prefix (`run_01j…`): unique without coordination, safe as
container and file names, and sortable by creation time.
