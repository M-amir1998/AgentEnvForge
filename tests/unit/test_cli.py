"""CLI behaviour that does not need Docker (exit codes, validation output)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentenvforge import __version__
from agentenvforge.cli.app import app
from agentenvforge.cli.common import EXIT_INVALID
from tests.conftest import EXAMPLE_TASK, MINIMAL_TASK

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(agentenv_home: Path) -> None:
    """Every CLI test gets its own AGENTENV_HOME."""


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_validate_example_task() -> None:
    result = runner.invoke(app, ["task", "validate", str(EXAMPLE_TASK)])
    assert result.exit_code == 0, result.output
    assert "VALID" in result.output


def test_validate_reports_actionable_errors(tmp_path: Path) -> None:
    task = tmp_path / "task.yaml"
    task.write_text(MINIMAL_TASK + "resources:\n  memory_mb: -20\n")
    result = runner.invoke(app, ["task", "validate", str(task)])
    assert result.exit_code == EXIT_INVALID
    assert "TaskValidationError" in result.output
    assert "resources.memory_mb" in result.output
    assert "-20" in result.output


def test_validate_detects_duplicate_ids_across_files(tmp_path: Path) -> None:
    for name in ("a", "b"):
        directory = tmp_path / name
        (directory / "project").mkdir(parents=True)
        (directory / "Dockerfile").write_text("FROM scratch\n")
        (directory / "task.yaml").write_text(MINIMAL_TASK)
    result = runner.invoke(app, ["task", "validate", str(tmp_path)])
    assert result.exit_code == EXIT_INVALID
    assert "duplicate task ID" in result.output


def test_inspect_shows_resolved_defaults() -> None:
    result = runner.invoke(app, ["task", "inspect", str(EXAMPLE_TASK)])
    assert result.exit_code == 0
    assert "ledger-transaction-rollback" in result.output
    assert "disabled" in result.output  # network


def test_schema_is_valid_json() -> None:
    result = runner.invoke(app, ["task", "schema"])
    assert json.loads(result.output)["title"] == "TaskSpec"


def test_run_requires_exactly_one_agent() -> None:
    result = runner.invoke(app, ["run", str(EXAMPLE_TASK)])
    assert result.exit_code == EXIT_INVALID
    assert "specify exactly one" in result.output


def test_run_rejects_invalid_task_before_touching_docker(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", str(tmp_path / "missing.yaml"), "--agent", "noop"])
    assert result.exit_code == EXIT_INVALID


def test_runs_list_on_empty_database() -> None:
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0


def test_runs_show_unknown_run() -> None:
    result = runner.invoke(app, ["runs", "show", "run_doesnotexist"])
    assert result.exit_code == EXIT_INVALID


def test_invalid_settings_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTENV_LOG_FORMAT", "xml")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == EXIT_INVALID
