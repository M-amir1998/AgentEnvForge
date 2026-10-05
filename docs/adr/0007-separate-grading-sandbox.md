# ADR-0007: Grade in a fresh sandbox, with hidden assets and protected paths

- Status: accepted
- Date: 2026-10-04

## Context

If the grader runs where the agent ran, the agent can influence grading: leave a
background process that rewrites results, install a fake `pytest`, edit the tests, or
read hidden tests before they are used. Benchmarks also need tests the agent never sees.

## Options considered

1. **Grade in the agent's container** (simplest; common in early benchmarks): every change
   the agent made anywhere in the container is still active during grading.
2. **Apply a patch to a clean checkout** (SWE-bench style): strong, but needs `git` in the
   image and loses non-text changes.
3. **Copy the agent's final workspace into a fresh container from the same image.**

## Decision

Option 3:

1. After the agent finishes, its workspace is copied to the host (`final/`) and diffed
   against the baseline on the host (`solution.patch`).
2. The agent sandbox is destroyed, along with any processes or changes the agent made
   outside the workspace.
3. A grading tree is built on the host: the final workspace with every
   `grading.protected_paths` entry reset to its baseline content.
4. A new sandbox receives the grading tree and, only now, the hidden `grading.assets`
   (at `/grader`).

## Consequences

- Hidden tests never exist in any container the agent can reach. Validation also rejects
  assets located inside the agent-visible source tree.
- Editing protected tests has no effect on grading, but the edit stays visible in
  `solution.patch` for analysis.
- Setup commands run only in the agent sandbox, so grader dependencies belong in the image.

## Limitations (documented, not solved)

The solution's code still *executes* during grading, as tests must import it. Code in the
workspace can therefore tamper with the test process (e.g. monkeypatching pytest
internals from an imported module). Mitigations planned: running hidden tests as a
different user, read-only assets, and result signatures. See `docs/architecture/security.md`.
