# Grading

Implementation: `grading/`, orchestrated by `_stage_run_grader`. Decision record: ADR-0007.

## Flow

1. The agent's final workspace is copied to the host and diffed against the baseline.
2. A grading tree is built on the host: the final workspace, with each
   `grading.protected_paths` entry restored to its baseline content (or removed if it
   did not exist originally).
3. A **fresh** sandbox from the same image receives the grading tree at the workspace
   path, and the hidden `grading.assets` directory at `grading.assets_mount` (default `/grader`).
4. The configured graders run in order. With more than one grader (or `weighted`
   aggregation), a `CompositeGrader` combines them.

## Graders

| Type | Passes when | Score |
|---|---|---|
| `pytest` | exit 0, no failures/errors, at least one test executed | passed / executed tests |
| `command` | exit code in `success_exit_codes` (default `[0]`) | 1.0 or 0.0 |
| composite `all` | every component passes | weighted mean |
| composite `weighted` | weighted mean ≥ `pass_threshold` | weighted mean |

`GradeResult` carries `passed`, `score ∈ [0, 1]`, test counters, duration, a summary,
scalar `details` and nested `components`, never just a boolean.

## Failed grade vs. grader error

| Situation | Outcome |
|---|---|
| Tests fail, or collection fails because the solution is broken (pytest exit 1/2 with a report) | `passed=false`, run `completed` |
| pytest internal/usage error (exit 3/4), no tests collected (exit 5), no report produced | `GraderError` → `failed_grader` |
| Grader exceeded `timeouts.grader_seconds` | `GraderTimeoutError` → `timed_out` |

The pytest grader runs `python -m pytest <paths> <args> -p no:cacheprovider
--junitxml=/tmp/agentenv-grader/<name>/junit.xml`. Hidden tests given as absolute paths
under `/grader` are outside the workspace, so the workspace's own pytest configuration
and `conftest.py` files are not used for them. The JUnit report is size-bounded (20 MiB)
and parsed only for aggregate counters.

## Artifacts written

`grade.json`, and per grader `grader/<name>/{stdout.log, stderr.log, junit.xml}`.

## Known limitations

The solution code runs inside the grading process (tests must import it), so a
deliberately malicious solution can still tamper with the test run. See
[security](security.md#grader-integrity).
