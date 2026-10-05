# ADR-0002: YAML task files with an explicitly versioned, strictly validated schema

- Status: accepted
- Date: 2026-10-04

## Context

Task definitions are written by benchmark authors, reviewed in pull requests, and must
remain loadable for years so that published results can be reproduced.

## Options considered

1. **Python code** (tasks as classes): maximally flexible, but executing a task file runs
   arbitrary host code, and tasks become hard to lint, diff and translate.
2. **JSON**: unambiguous, but no comments and painful to hand-write.
3. **TOML**: good for flat config; awkward for lists of nested grader objects.
4. **YAML**: comments, readable nesting, the de-facto format for CI and Kubernetes manifests.

## Decision

YAML parsed with a `SafeLoader` (no object construction) that additionally **rejects
duplicate keys**, validated by Pydantic models with `extra="forbid"`.

- Every file declares `schema_version`. The loader checks it *first* and refuses unknown
  versions with a dedicated message.
- Compatibility policy: adding an optional field with a default is compatible within a
  version. Removing, renaming or changing semantics requires a new version, and the loader
  must keep accepting old versions.
- Unknown keys are errors (`resorces:` is a typo, not a silently ignored section).
- Errors are reported as `field / reason / received`, all issues of a pass at once.
- Filesystem checks (paths exist, stay inside the task directory, hidden assets are not
  inside the agent-visible source) run before any expensive work.
- A JSON Schema is generated from the models (`schemas/task-v1.schema.json`) for editor
  support; a test fails if it is stale.

## Consequences

- Task files are data, safe to load from untrusted sources.
- YAML pitfalls are handled explicitly: unquoted numeric git revisions are rejected with a
  hint to quote them (YAML would parse `1234567` as an integer).
- The definition hash is computed over the canonical JSON form, so reformatting a file
  does not change its identity.

## Limitations

Dynamic task generation needs an external generator that writes YAML (or a future
loader plugin), by design.
