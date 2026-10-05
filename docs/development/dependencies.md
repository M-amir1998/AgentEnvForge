# Dependencies and why they exist

Every dependency is maintenance burden and attack surface, so each one must earn its place.

## Runtime

| Package | Why | Alternative rejected |
|---|---|---|
| `docker` | Official Docker Engine API client: exec streams, archives, builds, labels | shelling out to the `docker` CLI (string parsing, an extra binary, weaker error types) |
| `pydantic` | Strict, typed validation with precise error locations; JSON round-trips for persisted records | hand-written validation (error-prone, much more code) |
| `pyyaml` | YAML parsing (`SafeLoader` only) | `ruamel.yaml` (heavier; round-trip editing not needed) |
| `typer` | Typed CLI from function signatures | `argparse` (verbose), `click` directly (Typer adds typing on top) |
| `rich` | Readable tables and coloured output, used only in `cli/` | plain `print` formatting |

Deliberately **not** used: SQLAlchemy/Alembic (ADR-0004), structlog (stdlib `logging` +
`contextvars` covers it), GitPython (the git CLI with argument arrays is simpler and safer),
any LLM SDK (ADR-0003), FastAPI (until the API milestone).

## Development

`pytest`, `pytest-cov` (branch coverage), `hypothesis` (property tests for the state
machine and path validators), `ruff` (lint + format), `mypy` (strict), `pre-commit`,
and the type stubs `types-docker`, `types-pyyaml`.

## Locking and updates

`uv.lock` pins the full graph. Update deliberately with `uv lock --upgrade-package <name>`,
then run `make check` plus the Docker tests. Do not apply bulk upgrades blindly.
