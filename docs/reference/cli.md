# CLI reference

Run `agentenv <command> --help` for full options. Global options (before the command):
`--home`, `--log-level`, `--log-format`.

| Command | Purpose |
|---|---|
| `agentenv version` | print the version |
| `agentenv doctor` | check Python, git, Docker SDK/daemon/API, data directory, disk, database |
| `agentenv task validate PATH...` | validate task files or directories of `task.yaml` files; detects duplicate IDs |
| `agentenv task inspect TASK` | show the task with all defaults resolved, plus warnings |
| `agentenv task schema` | print the JSON Schema of the task format |
| `agentenv run TASK --agent SPEC\|manual\|noop` | execute a run with an agent spec file or built-in agent |
| `agentenv run TASK --agent-command "CMD"` | execute with an inline in-sandbox command agent |
| `agentenv run ... --agent-timeout SECONDS` | override the task's agent time budget (recorded in the manifest's `overrides`) |
| `agentenv runs list [--task ID] [--status S] [--limit N]` | recent runs |
| `agentenv runs show RUN_ID [--events]` | result, timings, provenance, optional event log |
| `agentenv runs events RUN_ID` | event log |
| `agentenv runs artifacts RUN_ID` | artifact names, sizes, hashes and location |
| `agentenv cleanup [--run-id ID] [--all]` | remove sandboxes left by processes that have exited (`--all`: also those of live or unknown owners) |
| `agentenv experiment validate FILE [--preflight]` | validate an experiment file and everything it references; `--preflight` checks that agents are ready (e.g. Ollama model installed) |
| `agentenv experiment inspect FILE [--workers N]` | resolved tasks, agents, hashes, warnings and the run order |
| `agentenv experiment run FILE [--workers N] [--output DIR] [--overwrite] [--export-artifacts]` | execute the matrix and write the results directory |
| `agentenv experiment report ID [--output DIR] [--overwrite] [--export-artifacts]` | regenerate an experiment's results directory from the database |
| `agentenv experiments list [--limit N]` | recent experiments (`interrupted`: the owning process is gone) |
| `agentenv experiments show ID` | status history, provenance and current results |

`python -m agentenvforge` is equivalent to `agentenv`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (`run`: completed **and** passed; `experiment run`: completed with valid results) |
| 1 | `run`: completed but the solution failed grading; `experiment run`: completed, but with validity warnings (excluded runs, a failing reference, a passing baseline) |
| 2 | invalid input: task/agent/experiment spec, settings, usage, or an output directory holding other results |
| 3 | infrastructure failure (build, sandbox, grader error, timeout, internal), an agent that is not ready, an experiment that FAILED, or `doctor` found a failure |
| 130 | cancelled (Ctrl+C / SIGTERM); cleanup was still performed |

An agent that solves no task does not make `experiment run` fail: that is a result.
