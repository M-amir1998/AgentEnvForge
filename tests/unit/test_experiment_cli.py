"""``agentenv experiment ...`` and ``agentenv experiments ...`` against the fake backend."""

from __future__ import annotations

import csv
import json
import re
import shutil
import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentenvforge.cancellation import CancellationToken
from agentenvforge.cli import wiring
from agentenvforge.cli.app import app
from agentenvforge.cli.common import (
    EXIT_CANCELLED,
    EXIT_INFRASTRUCTURE,
    EXIT_INVALID,
    EXIT_RESULTS_INVALID,
)
from agentenvforge.cli.experiment_commands import _Interrupts
from agentenvforge.config.settings import Settings
from agentenvforge.sandbox.base import SandboxBackend, SandboxRole
from tests.conftest import EXAMPLE_DIR
from tests.fakes import FakeBackend
from tests.unit.test_experiment_execution import CANCEL_AFTER, GAUGE, grade_by_fix

runner = CliRunner()
SOLVER = "tests.unit.test_experiment_execution:build_solver"
UNAVAILABLE = "tests.unit.test_experiment_execution:build_unavailable"


@pytest.fixture(autouse=True)
def fake_docker(agentenv_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every backend the CLI creates is a fake one (no Docker needed)."""
    GAUGE.active = GAUGE.peak = 0
    CANCEL_AFTER.clear()
    created: list[FakeBackend] = []

    def create_backend(_: Settings) -> SandboxBackend:
        backend = FakeBackend(
            root=tmp_path / "sandboxes" / f"b{len(created)}",
            handlers={SandboxRole.GRADER: grade_by_fix},
        )
        created.append(backend)
        return backend

    monkeypatch.setattr(wiring, "create_backend", create_backend)


def experiment(tmp_path: Path, *, agents: str, extra: str = "", repetitions: int = 2) -> Path:
    shutil.copytree(EXAMPLE_DIR, tmp_path / "example")
    path = tmp_path / "experiment.yaml"
    path.write_text(
        'schema_version: "1"\n'
        "id: cli-experiment\n"
        "tasks: [example/task.yaml]\n"
        f"agents:\n{agents}"
        f"repetitions: {repetitions}\n"
        f"{extra}",
        encoding="utf-8",
    )
    return path


SOLVER_AND_BASELINE = (
    f"  - id: solver\n    agent: {{type: python, adapter: '{SOLVER}'}}\n"
    "  - id: noop\n    role: baseline\n    agent: {type: noop}\n"
)


def experiment_id(output: str) -> str:
    match = re.search(r"exp_[0-9a-z]{26}", output)
    assert match is not None, output
    return match.group(0)


def test_validate_and_inspect(tmp_path: Path) -> None:
    path = experiment(tmp_path, agents=SOLVER_AND_BASELINE, extra="execution: {workers: 2}\n")
    result = runner.invoke(app, ["experiment", "validate", str(path)])
    assert result.exit_code == 0, result.output
    assert "4 planned runs" in result.output and "valid" in result.output
    assert "host-side agents (solver)" in result.output  # workers=2 with a python adapter

    result = runner.invoke(app, ["experiment", "inspect", str(path), "--workers", "1"])
    assert result.exit_code == 0, result.output
    assert "Run order" in result.output and "host-side" not in result.output


def test_validate_rejects_invalid_definitions(tmp_path: Path) -> None:
    path = experiment(tmp_path, agents=SOLVER_AND_BASELINE, repetitions=0)
    result = runner.invoke(app, ["experiment", "validate", str(path)])
    assert result.exit_code == EXIT_INVALID
    assert "repetitions" in result.output


def test_preflight_reports_agents_that_are_not_ready(tmp_path: Path) -> None:
    agents = f"  - id: down\n    agent: {{type: python, adapter: '{UNAVAILABLE}'}}\n"
    path = experiment(tmp_path, agents=agents)
    result = runner.invoke(app, ["experiment", "validate", str(path), "--preflight"])
    assert result.exit_code == EXIT_INFRASTRUCTURE
    assert "down is not ready" in result.output


def test_run_publishes_results_and_records_the_experiment(
    tmp_path: Path, agentenv_home: Path
) -> None:
    path = experiment(tmp_path, agents=SOLVER_AND_BASELINE)
    result = runner.invoke(app, ["experiment", "run", str(path), "--export-artifacts"])
    assert result.exit_code == 0, result.output
    exp_id = experiment_id(result.output)
    directory = agentenv_home / "experiments" / exp_id
    assert "results valid" in result.output
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed" and summary["counts"]["passed"] == 2
    with (directory / "runs.csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 4
    assert (directory / "experiment.yaml").read_text(encoding="utf-8") == path.read_text(
        encoding="utf-8"
    )
    assert (directory / "artifacts").is_dir()

    listed = runner.invoke(app, ["experiments", "list"])
    assert listed.exit_code == 0 and exp_id in listed.output and "completed" in listed.output
    shown = runner.invoke(app, ["experiments", "show", exp_id])
    assert shown.exit_code == 0, shown.output
    assert "history: created" in shown.output and "completed" in shown.output

    run_id = summary_run_id(directory)
    run_shown = runner.invoke(app, ["runs", "show", run_id])
    assert run_shown.exit_code == 0 and f"experiment: {exp_id}" in run_shown.output

    other = tmp_path / "regenerated"
    report = runner.invoke(app, ["experiment", "report", exp_id, "--output", str(other)])
    assert report.exit_code == 0, report.output
    assert (
        json.loads((other / "summary.json").read_text(encoding="utf-8"))["counts"]
        == (summary["counts"])
    )
    assert (other / "experiment.yaml").read_text(encoding="utf-8") == path.read_text(
        encoding="utf-8"
    )


def summary_run_id(directory: Path) -> str:
    with (directory / "runs.csv").open(encoding="utf-8", newline="") as handle:
        return next(csv.DictReader(handle))["run_id"]


def test_output_directory_holding_other_results_needs_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "results"
    path = experiment(
        tmp_path, agents=SOLVER_AND_BASELINE, extra=f"output: {{directory: {output.name}}}\n"
    )
    assert runner.invoke(app, ["experiment", "run", str(path)]).exit_code == 0
    refused = runner.invoke(app, ["experiment", "run", str(path)])
    assert refused.exit_code == EXIT_INVALID and "--overwrite" in refused.output
    replaced = runner.invoke(app, ["experiment", "run", str(path), "--overwrite"])
    assert replaced.exit_code == 0, replaced.output
    published = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert published["experiment_id"] == experiment_id(replaced.output)


def test_failing_reference_agent_makes_results_invalid(tmp_path: Path) -> None:
    agents = (
        f"  - id: solver\n    agent: {{type: python, adapter: '{SOLVER}'}}\n"
        "  - id: broken-reference\n    role: reference\n    agent: {type: noop}\n"
    )
    result = runner.invoke(app, ["experiment", "run", str(experiment(tmp_path, agents=agents))])
    assert result.exit_code == EXIT_RESULTS_INVALID, result.output
    assert "warning:" in result.output


def test_failed_experiment_exits_with_infrastructure_code(tmp_path: Path) -> None:
    agents = f"  - id: down\n    agent: {{type: python, adapter: '{UNAVAILABLE}'}}\n"
    result = runner.invoke(app, ["experiment", "run", str(experiment(tmp_path, agents=agents))])
    assert result.exit_code == EXIT_INFRASTRUCTURE, result.output
    assert "experiment failed" in result.output
    exp_id = experiment_id(result.output)
    report = runner.invoke(app, ["experiment", "report", exp_id])
    assert report.exit_code == 0, report.output
    shown = runner.invoke(app, ["experiments", "show", exp_id])
    assert "experiment failed: agent down is not ready" in shown.output  # kept on regeneration


def test_unknown_experiment_is_reported() -> None:
    for command in (["experiments", "show", "exp_x"], ["experiment", "report", "exp_x"]):
        result = runner.invoke(app, command)
        assert result.exit_code == EXIT_INVALID and "not found" in result.output


def test_interrupt_handler_cancels_first_and_exits_on_the_third_press() -> None:
    token = CancellationToken()
    with _Interrupts(token) as interrupts:
        interrupts._handle(signal.SIGINT, None)
        assert token.cancelled and "SIGINT" in token.reason
        interrupts._handle(signal.SIGINT, None)  # second press: only a message
        with pytest.raises(SystemExit) as exited:
            interrupts._handle(signal.SIGINT, None)
        assert exited.value.code == EXIT_CANCELLED
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # restored
