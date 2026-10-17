"""End-to-end: batch experiments on real Docker sandboxes.

A mixed batch (pass, graded failure, agent timeout, adapter crash) proves that concurrent
runs stay isolated and are all cleaned up; a cancelled batch proves that cancellation
stops scheduling, ends the active runs as CANCELLED and still removes their sandboxes.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from contextlib import closing
from pathlib import Path

import docker
import pytest
from typer.testing import CliRunner

from agentenvforge.agents.spec import AgentEnvironment
from agentenvforge.analysis.outcomes import Outcome
from agentenvforge.cancellation import CancellationToken
from agentenvforge.cli.app import app
from agentenvforge.cli.common import EXIT_OK
from agentenvforge.cli.wiring import open_services
from agentenvforge.config.settings import load_settings
from agentenvforge.domain.results import RunResult
from agentenvforge.domain.status import RunStatus
from agentenvforge.execution.experiments import Progress
from agentenvforge.experiments.loader import load_experiment
from agentenvforge.experiments.matrix import RunCombination
from agentenvforge.experiments.status import ExperimentStatus
from agentenvforge.sandbox.docker_backend import LABEL_RUN_ID
from tests.conftest import EXAMPLE_DIR

pytestmark = [pytest.mark.docker, pytest.mark.e2e]
runner = CliRunner()
EXAMPLES = EXAMPLE_DIR.parent
OFF_BY_ONE = EXAMPLES / "off-by-one" / "task.yaml"
AGENTS = EXAMPLES / "agents"


def relative(target: Path, start: Path) -> str:
    return Path(os.path.relpath(target, start)).as_posix()


def write_experiment(directory: Path, agents: list[str], *, repetitions: int, workers: int) -> Path:
    path = directory / "experiment.yaml"
    lines = [
        'schema_version: "1"',
        "id: e2e-batch",
        f"tasks: [{relative(OFF_BY_ONE, directory)}]",
        "agents:",
        *agents,
        f"repetitions: {repetitions}",
        f"execution: {{workers: {workers}}}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def spec_agent(directory: Path, agent_id: str, spec: str, *extra: str) -> str:
    return "\n".join(
        [f"  - id: {agent_id}", f"    spec: {relative(AGENTS / spec, directory)}", *extra]
    )


def leftover_containers(run_ids: list[str]) -> list[str]:
    with closing(docker.from_env()) as client:
        return [
            f"{container.name} ({container.status})"
            for run_id in run_ids
            for container in client.containers.list(
                all=True, filters={"label": f"{LABEL_RUN_ID}={run_id}"}
            )
        ]


def test_mixed_batch_isolates_failures_and_cleans_up(agentenv_home: Path, tmp_path: Path) -> None:
    agents = [
        spec_agent(tmp_path, "oracle", "oracle/oracle.yaml", "    role: reference"),
        "  - id: noop\n    role: baseline\n    agent: {type: noop}",
        spec_agent(
            tmp_path, "sleeper", "deterministic/sleeper.yaml", "    agent_timeout_seconds: 3"
        ),
        spec_agent(tmp_path, "crasher", "deterministic/crasher.yaml"),
    ]
    path = write_experiment(tmp_path, agents, repetitions=1, workers=2)
    output = tmp_path / "results"

    result = runner.invoke(app, ["experiment", "run", str(path), "--output", str(output)])

    assert result.exit_code == EXIT_OK, result.output
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    with (output / "runs.csv").open(encoding="utf-8", newline="") as handle:
        rows = {row["agent_id"]: row for row in csv.DictReader(handle)}
    outcomes = {agent: row["outcome"] for agent, row in rows.items()}
    assert outcomes == {
        "oracle": "passed",
        "noop": "failed",
        "sleeper": "agent_timeout",
        "crasher": "agent_error",
    }, result.output
    assert summary["status"] == "completed" and summary["results_valid"], summary["warnings"]
    assert all(row["cleanup_ok"] == "True" for row in rows.values())
    assert leftover_containers([row["run_id"] for row in rows.values()]) == []
    assert (output / "report.md").is_file()


class QuietObserver:
    def preparing(self, message: str) -> None: ...

    def run_finished(
        self,
        combination: RunCombination,
        run_id: str | None,
        outcome: Outcome,
        result: RunResult | None,
        progress: Progress,
    ) -> None: ...

    def cancelling(self, reason: str) -> None: ...


def test_cancelled_batch_ends_active_runs_and_removes_their_sandboxes(
    agentenv_home: Path, tmp_path: Path
) -> None:
    agents = [
        spec_agent(
            tmp_path, "sleeper", "deterministic/sleeper.yaml", "    agent_timeout_seconds: 300"
        )
    ]
    path = write_experiment(tmp_path, agents, repetitions=6, workers=2)
    loaded = load_experiment(
        path, AgentEnvironment(environ={}, write=lambda _: None, prompt=lambda _: "")
    )
    services = open_services(load_settings(cli_overrides={"home": agentenv_home}))
    token = CancellationToken()
    cancelled_at: list[float] = []

    def cancel_once_both_agents_run() -> None:
        # Not on a timer: on a fresh machine, building the image alone can take longer.
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and not token.cancelled:
            active = [
                run
                for run in services.repository.list_runs(limit=10)
                if run.status is RunStatus.RUNNING_AGENT
            ]
            if len(active) == 2:
                cancelled_at.append(time.monotonic())
                token.cancel("test cancellation")
                return
            time.sleep(0.2)

    watcher = threading.Thread(target=cancel_once_both_agents_run, daemon=True)
    watcher.start()
    try:
        outcome = services.experiment_executor().execute(
            loaded, workers=2, cancellation=token, observer=QuietObserver()
        )
        finished_at = time.monotonic()
        stored = services.experiments.get_experiment(outcome.experiment_id)
    finally:
        token.cancel("test finished")  # stops the watcher if the batch ended another way
        watcher.join()
        services.close()

    counts = outcome.summary.counts
    assert cancelled_at, "the two agents were never running at the same time"
    assert outcome.status is ExperimentStatus.CANCELLED
    assert counts.cancelled == 2, counts  # exactly the two active runs
    assert counts.not_started == 4 and counts.planned == 6
    stopped_in = finished_at - cancelled_at[0]
    assert stopped_in < 60, f"cancellation took {stopped_in:.0f}s (agent budget is 300s)"
    assert stored is not None
    run_ids = [entry.run_id for entry in stored.entries if entry.run_id]
    assert len(run_ids) == 2 and leftover_containers(run_ids) == []
