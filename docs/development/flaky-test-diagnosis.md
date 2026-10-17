# Flaky test diagnosis: `test_hanging_agent_times_out_and_is_cleaned_up`

Status: **not reproduced since the fixes below; root cause not proven.** This page records
what was observed, what was tested, and what changed, so a recurrence can be diagnosed
from its own output.

## Symptom

The end-to-end test (`tests/e2e/test_example_task.py`) runs the example task with
`--agent-command "sleep 600"` and a 3-second agent budget. It expects exit code 3, a
`TIMED_OUT` result with reason `agent_timeout`, and no container left behind.

It failed once in a full-suite run on Windows 11 with Docker Desktop (187 of 188 tests
passed), then passed when re-run alone and in the next two full-suite runs. The failure
report did not include the run's stored result. Exit code 3 is shared by every
infrastructure failure, so it could not be determined afterwards which failure occurred.

## What was tested

| Hypothesis | Experiment | Result |
|---|---|---|
| `exec` exit status read before Docker recorded it | 600 short `exec` calls, each checking for a missing/stale exit code | 0 of 600 stale |
| Cold Docker state (first container after start-up) | 6 cold-start cycles: 12 runs of the test, 6 full suites | all passed |
| Timing under contention | 20 consecutive runs on an idle machine; 20 under load (12 CPU-burning processes plus concurrent container churn) | 40 of 40 passed, 0 containers left |

## Conclusion

The failure could not be reproduced, so its root cause is **not proven**. The most
plausible explanation is a transient Docker Desktop transport error (named pipe / HTTP)
during sandbox start or command execution, which the run correctly reported as an
infrastructure failure. The test then failed on the `TIMED_OUT` expectation, without
saying which failure it saw.

## What changed

Real defects found while analysing the test, fixed whether or not they caused the failure:

- **Transport errors** from `requests`, `OSError` and `pywintypes` (Windows named pipes)
  are normalised to `SandboxError` instead of surfacing as internal errors.
- **Exec exit status** is polled (`exec_inspect`, up to 5 s) instead of read once.
- **The readiness probe** after container start retries up to 3 times.
- **A container leak**: a container whose post-start verification failed was not removed.
  It is now destroyed on every verification error.
- **Sandbox start timeouts** are classified as `SandboxStartError`.
- **Orphan cleanup** removes only sandboxes whose owner process has exited, so a test can no
  longer remove (or be confused by) another process's sandboxes.

And in the test itself:

- Every assertion prints the exit code, the CLI output and the stored run result, so a
  recurrence identifies its own cause.
- The leftover-container check is scoped to the runs recorded in the test's own
  `AGENTENV_HOME`. Previously any AgentEnvForge container on the daemon, for example from
  an experiment in another terminal, would have failed it.

## Verification after the changes

See the final verification section of the v0.2 milestone report: the test is repeated on
Windows with Docker Desktop, and the number of repetitions and their results are recorded
there and in [status](status.md).

## If it fails again

1. Read the stored result in the assertion message: `status`, `failure.reason`,
   `failure.stage` and `failure.message` name the failing stage.
2. `agentenv runs show <run id> --events` (with the test's `AGENTENV_HOME`, printed by
   pytest as `tmp_path`) shows the event log up to the failure.
3. A `failed_sandbox` with a transport error points at the Docker daemon. A `timed_out`
   result with leftover containers points at cleanup.
