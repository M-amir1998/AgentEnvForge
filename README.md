# AgentEnvForge

[![CI](https://github.com/M-amir1998/AgentEnvForge/actions/workflows/ci.yml/badge.svg)](https://github.com/M-amir1998/AgentEnvForge/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**AgentEnvForge is an open-source framework for defining, executing, grading and
reproducing isolated software-engineering environments for training and evaluating AI
agents.**

You describe a task once (a repository in a known state, an environment and a way to
grade a solution) in a versioned YAML file. AgentEnvForge then runs any agent against it
in a disposable, resource-limited Docker sandbox, grades the result in a *separate*
sandbox with hidden tests, and records everything needed to reproduce and audit the run.

> Status: **pre-release (0.1.0.dev0)**. The single-task vertical slice is implemented and
> tested end to end. See [what is supported today](#status) and the [roadmap](docs/design/roadmap.md).

## Why

Evaluating a coding agent on one task by hand is easy: clone, install, break, prompt,
test, clean up. Doing it reproducibly for hundreds of tasks, several agents and
repeated trials is an infrastructure problem: isolation, resource limits, hidden
tests, timeouts, cleanup, provenance and result storage. AgentEnvForge is that
infrastructure, so benchmark authors and agent builders can stop rebuilding it.

It is for people who **build coding agents**, **create agent benchmarks**, **generate RL
rollouts** or **study failure modes and reward hacking**. It is provider-agnostic: no
LLM SDK is a dependency, and agents plug in through a small adapter interface.

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker (Desktop or Engine), git.

```bash
git clone https://github.com/M-amir1998/AgentEnvForge.git
cd AgentEnvForge
make setup                      # or: uv sync

uv run agentenv doctor          # checks Python, git, Docker daemon, disk, database
uv run agentenv task validate examples/basic/task.yaml
uv run agentenv run examples/basic/task.yaml --agent examples/basic/agents/reference.yaml
```

```text
AgentEnvForge  run run_01m44csm77277gf42f064mej0t  task ledger-transaction-rollback  agent command:reference-solver

  Validation           PASS     0.07s
  Source preparation   PASS     0.02s
  Sandbox build        PASS     9.02s      (0.03s on later runs: the image is cached)
  Sandbox start        PASS     0.37s
  Agent execution      PASS     1.25s
  Solution capture     PASS     0.23s
  Grader               PASS     2.18s
  Artifacts            PASS     0.01s
  Cleanup              PASS     0.12s

Result: SUCCESS  (2/2 graders passed)
Score:  1.000
  - hidden-tests: pass 7/7 tests passed
  - visible-tests: pass 2/2 tests passed
```

The same task with an agent that does nothing **fails** grading (`--agent noop`, exit
code 1), which shows the task really discriminates. Then inspect what happened:

```bash
uv run agentenv runs list
uv run agentenv runs show <run-id> --events     # timings, provenance, event log
uv run agentenv runs artifacts <run-id>         # solution.patch, grader reports, logs, ...
```

Next: [write your first task](docs/tutorials/first-task.md) ·
[connect your own agent](docs/tutorials/custom-agent-adapter.md) ·
[custom grading](docs/tutorials/custom-grader.md)

## How it works

```text
 task.yaml ──► validate ──► prepare source ──► build image (cached by content hash)
                                                     │
            ┌────────────── agent sandbox ───────────┴───┐
            │ workspace copied in (no host mounts)        │
            │ non-root · no capabilities · no network     │
            │ CPU / memory / PID / time limits            │
            │ agent acts through an observed session      │
            └───────────────┬─────────────────────────────┘
                            │ final workspace copied out → solution.patch
            ┌───────────────▼──────────── grader sandbox ──┐
            │ fresh container · agent's files + protected   │
            │ paths restored · hidden tests added only here │
            └───────────────┬───────────────────────────────┘
                            ▼
      result + events (SQLite) · artifacts (filesystem) · manifest · cleanup
```

Every run moves through an explicit state machine and **always** passes through cleanup
before reaching exactly one terminal state (`completed`, `failed_build`, `timed_out`, ...).
Infrastructure failure and agent failure are kept apart. `completed` with
`passed=false` means the agent was wrong. `failed_grader` means we could not judge.

Read more: [architecture overview](docs/architecture/overview.md) ·
[execution lifecycle](docs/architecture/execution-lifecycle.md) ·
[security model](docs/architecture/security.md) · [design decisions (ADRs)](docs/adr/)

## Status

| Capability | State |
|---|---|
| Versioned task schema (v1), validation with actionable errors, JSON Schema | **implemented** |
| Local-directory and git sources (pinned commit, resolved SHA recorded) | **implemented** |
| Docker sandbox: CPU, memory (no swap), PIDs, network off by default, non-root, caps dropped | **implemented** |
| Per-command and total-agent timeouts; build/start/grader/cleanup timeouts | **implemented** |
| Agents: `command` (in-sandbox program), `python` (host-side adapter), `manual`, `noop` | **implemented** |
| Graders: `pytest` (JUnit-parsed), `command`, composite (`all` / `weighted`) | **implemented** |
| Separate grading sandbox, hidden assets, protected-path restoration | **implemented** |
| Run state machine, failure taxonomy, structured events, JSON logs, secret redaction | **implemented** |
| SQLite persistence, local artifact store, reproducibility manifest, patch capture | **implemented** |
| `doctor`, `cleanup` of orphaned sandboxes | **implemented** |
| Batch execution with bounded concurrency | planned (v0.2) |
| `reproduce <run-id>`, HTTP API, metrics export | planned |
| Disk quotas, gVisor/Firecracker isolation, distributed workers | planned |

Known limitations are tracked honestly in [docs/development/status.md](docs/development/status.md).
In particular, Docker containers share the host kernel. Treat AgentEnvForge as a
defence-in-depth boundary for semi-trusted code, not a VM-grade one ([details](docs/architecture/security.md)).

## Development

```bash
make check        # ruff lint + format check, strict mypy, all tests
make test-unit    # fast, no Docker
make test-e2e     # complete runs of the example task (Docker)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/development/environment-setup.md](docs/development/environment-setup.md).

## License

[Apache-2.0](LICENSE). This is a permissive license with an explicit patent grant, a
common choice for infrastructure that companies and researchers build on.
