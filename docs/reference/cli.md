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
| `agentenv cleanup [--run-id ID]` | remove sandboxes left by crashed processes |

`python -m agentenvforge` is equivalent to `agentenv`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (`run`: completed **and** passed) |
| 1 | `run` completed but the solution failed grading |
| 2 | invalid input: task/agent spec, settings or usage |
| 3 | infrastructure failure (build, sandbox, grader error, timeout, internal), or `doctor` found a failure |
| 130 | cancelled (Ctrl+C / SIGTERM); cleanup was still performed |
