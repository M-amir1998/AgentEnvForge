"""End-to-end: the real example task, the real CLI, real Docker sandboxes.

This is the project's critical milestone test. It proves the complete chain

    task YAML -> validation -> Docker sandbox -> agent -> source modification
              -> grader (separate sandbox, hidden tests) -> result -> artifacts -> cleanup

and that the grader actually discriminates: the reference agent passes, while a no-op
agent, a reward-hacking agent and a hanging agent all fail for the right reasons.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import docker
import pytest
from typer.testing import CliRunner, Result

from agentenvforge.cli.app import EXIT_GRADE_FAILED, EXIT_INFRASTRUCTURE, EXIT_OK, app
from agentenvforge.persistence.sqlite import SqliteRunRepository
from agentenvforge.sandbox.docker_backend import LABEL_MANAGED
from tests.conftest import EXAMPLE_TASK, REFERENCE_AGENT

pytestmark = [pytest.mark.docker, pytest.mark.e2e]
runner = CliRunner()


def agentenv(*arguments: str) -> Result:
    return runner.invoke(app, list(arguments))


def latest_result(home: Path) -> dict[str, object]:
    repository = SqliteRunRepository(home / "agentenv.db")
    (summary, *_) = repository.list_runs(limit=1)
    stored = repository.get_run(summary.run_id)
    repository.close()
    assert stored is not None and stored.result is not None
    return stored.result.model_dump(mode="json")


def assert_no_sandboxes_left() -> None:
    leftovers = docker.from_env().containers.list(
        all=True, filters={"label": f"{LABEL_MANAGED}=true"}
    )
    assert leftovers == [], [container.name for container in leftovers]


def test_reference_agent_solves_the_task(agentenv_home: Path) -> None:
    result = agentenv("run", str(EXAMPLE_TASK), "--agent", str(REFERENCE_AGENT))
    assert result.exit_code == EXIT_OK, result.output
    assert "SUCCESS" in result.output

    run = latest_result(agentenv_home)
    assert run["status"] == "completed" and run["passed"] is True and run["score"] == 1.0
    grade = run["grade"]
    assert isinstance(grade, dict)
    assert {
        component["grader"]: component["tests_passed"] for component in grade["components"]
    } == {
        "hidden-tests": 7,
        "visible-tests": 2,
    }

    artifacts = agentenv_home / "artifacts" / str(run["run_id"])
    patch = (artifacts / "solution.patch").read_text()
    assert "+        self._snapshots.append(copy.deepcopy(self._data))" in patch
    assert json.loads((artifacts / "changed_files.json").read_text())["modified"] == [
        "ledger/store.py"
    ]
    assert (artifacts / "workspace" / "ledger" / "store.py").is_file()  # artifacts.include
    manifest = json.loads((artifacts / "manifest.json").read_text())
    assert manifest["image"]["image_id"].startswith("sha256:")
    assert manifest["source"]["tree_hash"]
    assert_no_sandboxes_left()

    shown = agentenv("runs", "show", str(run["run_id"]), "--events")
    assert shown.exit_code == 0
    assert "grader.completed" in shown.output


def test_noop_agent_fails_grading(agentenv_home: Path) -> None:
    result = agentenv("run", str(EXAMPLE_TASK), "--agent", "noop")
    assert result.exit_code == EXIT_GRADE_FAILED, result.output
    run = latest_result(agentenv_home)
    assert run["status"] == "completed" and run["passed"] is False
    assert_no_sandboxes_left()


def test_editing_the_visible_tests_does_not_help(agentenv_home: Path) -> None:
    """Reward hacking attempt: replace the failing visible tests with trivial ones."""
    hack = "printf 'def test_ok():\\n    pass\\n' > tests/test_store.py"
    result = agentenv("run", str(EXAMPLE_TASK), "--agent-command", f'sh -c "{hack}"')
    assert result.exit_code == EXIT_GRADE_FAILED, result.output
    run = latest_result(agentenv_home)
    grade = run["grade"]
    assert isinstance(grade, dict)
    visible = next(c for c in grade["components"] if c["grader"] == "visible-tests")
    assert visible["tests_failed"] == 1  # the original test was restored and still fails
    assert_no_sandboxes_left()


def test_hanging_agent_times_out_and_is_cleaned_up(
    agentenv_home: Path, example_copy: Callable[[Callable[[str], str] | None], Path]
) -> None:
    task = example_copy(lambda text: text.replace("agent_seconds: 300", "agent_seconds: 3"))
    result = agentenv("run", str(task), "--agent-command", "sleep 600")
    assert result.exit_code == EXIT_INFRASTRUCTURE, result.output
    assert "TIMED_OUT" in result.output
    run = latest_result(agentenv_home)
    assert run["status"] == "timed_out"
    assert run["failure"]["reason"] == "agent_timeout"  # type: ignore[index]
    assert_no_sandboxes_left()
