# Contributing to AgentEnvForge

Thank you for helping! This guide covers setup, workflow and how to extend each part of
the system.

## Setup

See [docs/development/environment-setup.md](docs/development/environment-setup.md). In short:

```bash
make setup && uv run agentenv doctor && make check
```

## Workflow

- Branch from `main` (`feat/batch-runner`, `fix/grader-timeout`, `docs/...`).
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat(sandbox): ...`,
  `fix(grading): ...`, `docs: ...`, `test(e2e): ...`, `chore: ...`.
- Keep pull requests focused. Every commit should leave the tree green.
- A change is done when it has: implementation, tests, types, error handling, docs, and
  an entry in `CHANGELOG.md` / `docs/development/status.md` when behaviour changed.

## Quality gates (run by CI)

```bash
make lint        # ruff check + ruff format --check
make typecheck   # mypy --strict over src and tests
make test        # unit + integration + e2e (Docker tests skip without a daemon)
make test-e2e    # run before touching the sandbox, orchestrator or graders
```

Fix root causes instead of silencing checks. A `# noqa` or `# type: ignore` needs a
specific code and a reason.

## Code style

- Read [ADR-0006](docs/adr/0006-layering-and-dependency-direction.md): `domain` imports no
  infrastructure; only `sandbox/docker_backend.py` touches the Docker SDK; only `cli/`
  touches Typer/Rich.
- Strong typing, immutable models for data that crosses boundaries, `pathlib` for paths,
  argument vectors (never shell strings) for commands, UTC timestamps.
- Comments explain *why*. Public APIs have docstrings.
- Expected failures raise an `AgentEnvForgeError` subclass with a classified `reason`.
  Never `except Exception: pass`; chain causes with `raise ... from error`.
- Tests must not need network access (except building the example image), paid APIs or
  GPUs. Mark Docker-dependent tests with `@pytest.mark.docker`.

## Architecture changes

Significant decisions get an ADR in `docs/adr/` (context, options, decision,
consequences, limitations). Update `docs/architecture/` when behaviour changes.

## Extending AgentEnvForge

### A sandbox backend

Implement `SandboxBackend` and `Sandbox` (`src/agentenvforge/sandbox/base.py`), wire it in
`cli/wiring.create_backend`, and make `tests/integration/test_docker_backend.py`'s
assertions pass against it (parametrise the fixture). Every `ResourceSpec` limit must be
enforced or the task refused. Document what isolation the backend does and does not give.

### A grader

Add a spec model to `tasks/schema.py` (a new `type` literal in the `GraderSpec` union), a
class implementing `Grader` in `grading/graders.py`, a branch in `build_grader`, unit
tests against `tests/fakes.FakeSandbox`, and docs in `docs/architecture/grading.md`.
Remember the rule: wrong solution means `passed=False`; cannot judge means `GraderError`.
Regenerate the JSON Schema with `make schema`.

### An agent

Prefer an agent spec (`type: command` or `type: python`) in your own repository; see
[the tutorial](docs/tutorials/custom-agent-adapter.md). Built-in adapters belong in the
core only if they are provider-neutral.

### A task

Follow [the first-task tutorial](docs/tutorials/first-task.md). Task contributions must
pin their base image by digest, keep hidden assets outside the source, ship a reference
solution outside both, and **fail** with the `noop` agent.
