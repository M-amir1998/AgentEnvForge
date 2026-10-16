"""Experiment files: schema validation, resolution of tasks and agents, hashing, warnings."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from agentenvforge.agents.spec import AgentEnvironment, PythonAgentSpec
from agentenvforge.domain.failures import TaskValidationError
from agentenvforge.experiments.loader import LoadedExperiment, load_experiment
from tests.conftest import EXAMPLE_DIR
from tests.unit.test_experiment_execution import SolverAgent

ENVIRONMENT = AgentEnvironment(environ={}, write=lambda _: None, prompt=lambda _: "")
SOLVER = "tests.unit.test_experiment_loader:build_configurable"
PYTHON_SPEC = f"""schema_version: "1"
type: python
name: solver
adapter: {SOLVER}
options: {{temperature: 0, steps: 5}}
"""
HEADER = 'schema_version: "1"\nid: loader-test\ntasks: [example/task.yaml]\n'
NOOP = "agents:\n  - id: noop\n    agent: {type: noop}\n"

type Write = Callable[[str], Path]


def build_configurable(**options: object) -> SolverAgent:
    """A python adapter factory that accepts options, like real adapters do."""
    assert set(options) <= {"temperature", "steps"}
    return SolverAgent()


@pytest.fixture
def write(tmp_path: Path) -> Write:
    shutil.copytree(EXAMPLE_DIR, tmp_path / "example")
    (tmp_path / "solver.yaml").write_text(PYTHON_SPEC, encoding="utf-8")

    def write_experiment(body: str) -> Path:
        path = tmp_path / "experiment.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    return write_experiment


def load(path: Path, cpus: int = 8) -> LoadedExperiment:
    return load_experiment(path, ENVIRONMENT, cpu_count=lambda: cpus)


def issues(path: Path) -> list[tuple[str, str]]:
    with pytest.raises(TaskValidationError) as raised:
        load(path)
    return [(issue.field, issue.reason) for issue in raised.value.issues]


def test_minimal_experiment_resolves_tasks_and_agents(write: Write) -> None:
    loaded = load(write(HEADER + NOOP + "repetitions: 2\n"))
    assert loaded.task_ids == ("ledger-transaction-rollback",)
    assert loaded.agent_ids == ("noop",)
    assert loaded.spec.planned_runs == 2 and loaded.spec.execution.workers == 1
    assert len(loaded.definition_hash) == 64 and loaded.warnings == ()


@pytest.mark.parametrize(
    ("body", "field", "reason"),
    [
        (HEADER + NOOP + "repetitions: 0\n", "repetitions", "greater than or equal to 1"),
        (HEADER + NOOP + "repetitions: 1\nexecution: {workers: 65}\n", "execution.workers", ""),
        (
            'schema_version: "1"\nid: x\ntasks: [/abs/task.yaml]\n' + NOOP + "repetitions: 1\n",
            "tasks[0]",
            "relative",
        ),
        (
            HEADER + "agents:\n  - id: a\n    agent: {type: noop}\n"
            "  - id: a\n    agent: {type: noop}\nrepetitions: 1\n",
            "",
            "unique",
        ),
        (
            HEADER + "agents:\n  - id: a\n    spec: solver.yaml\n    agent: {type: noop}\n"
            "repetitions: 1\n",
            "agents[0]",
            "exactly one",
        ),
        (HEADER + "agents:\n  - id: m\n    agent: {type: manual}\nrepetitions: 1\n", "", "manual"),
        (HEADER + NOOP + "repetitions: 1\nunknown: 1\n", "unknown", ""),
        ('schema_version: "2"\nid: x\ntasks: [a.yaml]\n' + NOOP + "repetitions: 1\n", "", ""),
    ],
)
def test_invalid_definitions_are_rejected_with_the_field(
    write: Write, body: str, field: str, reason: str
) -> None:
    found = issues(write(body))
    assert any(field in f and reason in r for f, r in found), found


def test_missing_task_and_agent_files_are_reported_together(write: Write) -> None:
    body = (
        'schema_version: "1"\nid: x\ntasks: [missing/task.yaml]\n'
        "agents:\n  - id: a\n    spec: missing.yaml\nrepetitions: 1\n"
    )
    fields = [field for field, _ in issues(write(body))]
    assert any(field.startswith("tasks") for field in fields)
    assert any(field.startswith("agents[0]") for field in fields)


def test_options_override_a_python_spec_and_change_the_hash(write: Write) -> None:
    base = HEADER + "agents:\n  - id: s\n    spec: solver.yaml\n{extra}repetitions: 1\n"
    plain = load(write(base.format(extra="")))
    tuned = load(write(base.format(extra="    options: {temperature: 0.2}\n")))
    spec = tuned.agent("s").spec
    assert isinstance(spec, PythonAgentSpec)
    assert spec.options == {"temperature": 0.2, "steps": 5}  # merged, not replaced
    assert tuned.definition_hash != plain.definition_hash
    assert tuned.agent("s").canonical_spec["options"]["temperature"] == 0.2


def test_options_are_rejected_for_non_python_agents(write: Write) -> None:
    body = HEADER + "agents:\n  - id: n\n    agent: {type: noop}\n    options: {x: 1}\n"
    found = issues(write(body + "repetitions: 1\n"))
    assert (
        "agents[0] options",
        "only python agents take options; put command settings in the spec",
    ) in found


def test_hash_covers_the_evaluation_but_not_how_or_where_it_runs(write: Write) -> None:
    base = load(write(HEADER + NOOP + "repetitions: 2\n"))
    elsewhere = load(
        write(HEADER + NOOP + "repetitions: 2\nexecution: {workers: 3}\noutput: {directory: r}\n")
    )
    more = load(write(HEADER + NOOP + "repetitions: 3\n"))
    reseeded = load(write(HEADER + NOOP + "repetitions: 2\nexecution: {base_seed: 7}\n"))
    assert base.definition_hash == elsewhere.definition_hash
    assert len({base.definition_hash, more.definition_hash, reseeded.definition_hash}) == 3


@pytest.mark.parametrize(
    "changed",
    [
        "project/ledger/store.py",  # the buggy source the agent works on
        "grader/tests/test_hidden_transactions.py",  # a hidden test
        "environment/Dockerfile",  # the image
    ],
)
def test_hash_covers_the_files_a_task_ships(write: Write, tmp_path: Path, changed: str) -> None:
    path = write(HEADER + NOOP + "repetitions: 1\n")
    before = load(path).definition_hash
    target = tmp_path / "example" / changed
    target.write_text(target.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert load(path).definition_hash != before


def test_single_invalid_item_is_reported_once(write: Write) -> None:
    body = 'schema_version: "1"\nid: x\ntasks: [/abs/task.yaml]\n' + NOOP + "repetitions: 1\n"
    assert [field for field, _ in issues(write(body))] == ["tasks[0]"]


def test_concurrency_warnings(write: Write) -> None:
    body = HEADER + "agents:\n  - id: s\n    spec: solver.yaml\n{0}repetitions: 1\n"
    assert load(write(body.format("") + "execution: {workers: 1}\n")).warnings == ()
    parallel = load(write(body.format("") + "execution: {workers: 2}\n")).warnings
    assert any("host-side agents (s)" in warning for warning in parallel)
    crowded = load(write(HEADER + NOOP + "repetitions: 1\nexecution: {workers: 8}\n"), cpus=4)
    assert any("8 workers on a machine with 4 logical CPUs" in w for w in crowded.warnings)
