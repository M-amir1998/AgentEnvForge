# Development environment

## Requirements

| Tool | Version | Needed for | Check |
|---|---|---|---|
| Python | 3.12+ (CI: 3.12, 3.13, 3.14) | everything | `python --version` |
| uv | 0.5+ | dependency management, running tools | `uv --version` |
| git | any recent | git-sourced tasks, some tests | `git --version` |
| Docker | Engine 24+ or Docker Desktop | integration and e2e tests, real runs | `docker info` |
| make | GNU make (optional) | convenience targets | `make --version` |

Unit tests need only Python and uv. Docker-marked tests are skipped automatically,
with a reason, when no daemon is reachable.

## Setup

```bash
git clone https://github.com/M-amir1998/AgentEnvForge.git && cd AgentEnvForge
make setup              # = uv sync && uv run pre-commit install
uv run agentenv doctor  # every line should be OK
make check              # lint, format check, mypy --strict, tests
```

`uv sync` creates `.venv` from `uv.lock` with the `dev` dependency group, so every
contributor gets identical tool versions.

## Platform notes

- **Windows**: install Docker Desktop (WSL 2 backend) and make (`winget install
  ezwinports.make`); restart the shell afterwards so `docker-credential-desktop` is on
  `PATH`. Without it, image builds fail with a credential-helper error, which
  AgentEnvForge reports as a sandbox build failure with a hint. The repository uses LF
  line endings (`.gitattributes`); if your global `core.autocrlf=true`, that is fine.
- **macOS**: Docker Desktop, OrbStack or Colima all expose a compatible API.
- **Linux**: add your user to the `docker` group, or set `AGENTENV_DOCKER_HOST` for a
  rootless daemon.

## Reference machine (where v0.1 was developed and verified)

Windows 11 (x86_64), Git Bash, Python 3.12.10 via uv,
uv 0.11.21, git 2.45, Docker Desktop with Engine 29.8.1 (API 1.56),
GNU make 4.4 (ezwinports). Docker Desktop and make were installed with winget for this
project (Docker Desktop requires administrator rights).
