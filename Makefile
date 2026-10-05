# Thin wrappers around the real tools; every target works the same when run directly.
# Recipes avoid shell-specific syntax so they work with GNU make on Linux, macOS and Windows.

.PHONY: setup test test-unit test-integration test-e2e lint format typecheck check coverage build schema docs clean

setup:            ## Create the virtualenv, install dependencies and git hooks
	uv sync
	uv run pre-commit install

test:             ## All tests (Docker-dependent tests are skipped without a daemon)
	uv run pytest

test-unit:        ## Fast tests, no Docker needed
	uv run pytest tests/unit

test-integration: ## Docker backend and git integration tests
	uv run pytest tests/integration

test-e2e:         ## Complete runs of the example task (needs Docker)
	uv run pytest tests/e2e

lint:             ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format:           ## Apply formatting and safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck:        ## Strict static type checking
	uv run mypy

check: lint typecheck test  ## Everything CI runs

coverage:         ## Tests with a branch-coverage report
	uv run pytest --cov --cov-report=term-missing

build:            ## Build sdist and wheel into dist/
	uv build

schema:           ## Regenerate schemas/task-v1.schema.json
	uv run python scripts/generate_schema.py

docs:             ## Validate the example tasks referenced by the docs
	uv run agentenv task validate examples

clean:            ## Remove build and cache artefacts
	uv run python -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in (\"dist\", \"build\", \".pytest_cache\", \".mypy_cache\", \".ruff_cache\", \"htmlcov\")]"
