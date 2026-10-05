"""Task specification schema, loader and validation errors."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentenvforge.domain.failures import TaskValidationError
from agentenvforge.tasks.loader import (
    definition_hash,
    discover_task_files,
    find_duplicate_ids,
    load_task,
)
from agentenvforge.tasks.schema import TaskSpec, _validate_container_path, _validate_relative_path
from tests.conftest import EXAMPLE_TASK, MINIMAL_TASK, REPOSITORY_ROOT

WriteTask = Callable[[str], Path]


def issues_of(path: Path) -> dict[str, str]:
    with pytest.raises(TaskValidationError) as caught:
        load_task(path)
    return {issue.field: issue.reason for issue in caught.value.issues}


def test_example_task_is_valid() -> None:
    task = load_task(EXAMPLE_TASK)
    assert task.spec.id == "ledger-transaction-rollback"
    assert task.spec.grading.protected_paths == ("tests",)
    assert task.warnings == ()


def test_minimal_task_gets_conservative_defaults(write_task: WriteTask) -> None:
    spec = load_task(write_task(MINIMAL_TASK)).spec
    assert spec.network.enabled is False
    assert spec.environment.user == "1000:1000"
    assert spec.environment.workspace == "/workspace"
    assert spec.resources.memory_mb == 2048
    assert spec.resources.pids_limit == 512


def test_integer_schema_version_is_accepted(write_task: WriteTask) -> None:
    text = MINIMAL_TASK.replace('schema_version: "1"', "schema_version: 1")
    assert load_task(write_task(text)).spec.schema_version == "1"


def test_unsupported_schema_version_is_reported_alone(write_task: WriteTask) -> None:
    text = MINIMAL_TASK.replace('schema_version: "1"', 'schema_version: "2"\nbogus: 1')
    with pytest.raises(TaskValidationError) as caught:
        load_task(write_task(text))
    (issue,) = caught.value.issues
    assert issue.field == "schema_version"
    assert "unsupported schema version" in issue.reason
    assert issue.received == "'2'"


def test_negative_memory_reports_field_reason_and_value(write_task: WriteTask) -> None:
    with pytest.raises(TaskValidationError) as caught:
        load_task(write_task(MINIMAL_TASK + "resources:\n  memory_mb: -20\n"))
    (issue,) = caught.value.issues
    assert issue.field == "resources.memory_mb"
    assert "greater than or equal to 128" in issue.reason
    assert issue.received == "-20"


def test_all_schema_issues_are_reported_together(write_task: WriteTask) -> None:
    text = (
        MINIMAL_TASK.replace("id: minimal-task", "id: Not Valid!")
        + "timeouts:\n  agent_seconds: 0\n"
    )
    issues = issues_of(write_task(text))
    assert set(issues) == {"id", "timeouts.agent_seconds"}


def test_unknown_field_is_flagged_as_probable_typo(write_task: WriteTask) -> None:
    issues = issues_of(write_task(MINIMAL_TASK + "resorces:\n  cpus: 2\n"))
    assert issues == {"resorces": "unknown field (check for typos)"}


def test_duplicate_yaml_key_is_rejected(write_task: WriteTask) -> None:
    issues = issues_of(write_task(MINIMAL_TASK + "name: Second name\n"))
    assert "duplicate key 'name'" in issues["<yaml>"]


def test_missing_git_revision_is_reported_without_union_noise(write_task: WriteTask) -> None:
    text = MINIMAL_TASK.replace(
        "  type: local\n  path: project", "  type: git\n  repository: https://example.com/x.git"
    )
    assert issues_of(write_task(text)) == {"source.revision": "Field required"}


def test_unquoted_numeric_revision_is_rejected_with_hint(write_task: WriteTask) -> None:
    text = MINIMAL_TASK.replace(
        "  type: local\n  path: project",
        "  type: git\n  repository: https://example.com/x.git\n  revision: 1234567",
    )
    assert "quote it" in issues_of(write_task(text))["source.revision"]


def test_unpinned_git_revision_produces_warning(write_task: WriteTask) -> None:
    text = MINIMAL_TASK.replace(
        "  type: local\n  path: project",
        "  type: git\n  repository: https://example.com/x.git\n  revision: main",
    )
    task = load_task(write_task(text))
    assert any("not a full commit SHA" in warning for warning in task.warnings)


def test_missing_grader_command_is_reported(write_task: WriteTask) -> None:
    text = MINIMAL_TASK.replace('      command: ["true"]\n', "")
    assert "grading.graders[0].command" in issues_of(write_task(text))


def test_unknown_source_type_points_at_the_type_field(write_task: WriteTask) -> None:
    issues = issues_of(write_task(MINIMAL_TASK.replace("type: local", "type: svn")))
    assert list(issues) == ["source.type"]


def test_dockerfile_and_image_are_mutually_exclusive(write_task: WriteTask) -> None:
    text = MINIMAL_TASK.replace(
        "  dockerfile: Dockerfile", "  dockerfile: Dockerfile\n  image: python:3.12"
    )
    assert "exactly one of 'dockerfile' or 'image'" in issues_of(write_task(text))["environment"]


@pytest.mark.parametrize(
    ("replacement", "field"),
    [
        ("  path: ../outside", "source.path"),
        ("  path: missing-dir", "source.path"),
    ],
)
def test_host_paths_must_exist_inside_task_directory(
    write_task: WriteTask, replacement: str, field: str
) -> None:
    text = MINIMAL_TASK.replace("  path: project", replacement)
    assert field in issues_of(write_task(text))


def test_hidden_assets_inside_source_are_rejected(write_task: WriteTask, tmp_path: Path) -> None:
    (tmp_path / "project" / "hidden").mkdir()
    text = MINIMAL_TASK.replace("grading:\n", "grading:\n  assets: project/hidden\n")
    assert "visible to the agent" in issues_of(write_task(text))["grading.assets"]


def test_unsafe_workspace_path_is_rejected(write_task: WriteTask) -> None:
    text = MINIMAL_TASK.replace(
        "  dockerfile: Dockerfile", "  dockerfile: Dockerfile\n  workspace: /etc"
    )
    assert "system directory" in issues_of(write_task(text))["environment.workspace"]


def test_missing_file_and_non_mapping_documents(tmp_path: Path) -> None:
    assert issues_of(tmp_path / "absent.yaml") == {"<file>": "file does not exist"}
    (tmp_path / "list.yaml").write_text("- 1\n- 2\n")
    assert issues_of(tmp_path / "list.yaml") == {"<document>": "top level must be a mapping"}


def test_definition_hash_ignores_formatting(write_task: WriteTask, tmp_path: Path) -> None:
    first = load_task(write_task(MINIMAL_TASK))
    reformatted = "# comment\n" + MINIMAL_TASK.replace(
        "name: Minimal task", "name:   'Minimal task'"
    )
    second = load_task(write_task(reformatted))
    assert first.definition_hash == second.definition_hash == definition_hash(second.spec)


def test_duplicate_ids_are_detected(write_task: WriteTask, tmp_path: Path) -> None:
    task = load_task(write_task(MINIMAL_TASK))
    duplicates = find_duplicate_ids([task, task])
    assert list(duplicates) == ["minimal-task"]


def test_discovery_finds_task_files_recursively() -> None:
    assert EXAMPLE_TASK in discover_task_files(REPOSITORY_ROOT / "examples")


def test_committed_json_schema_is_current() -> None:
    committed = json.loads((REPOSITORY_ROOT / "schemas" / "task-v1.schema.json").read_text())
    committed.pop("$schema")
    assert committed == TaskSpec.model_json_schema(), (
        "regenerate with: uv run python scripts/generate_schema.py"
    )


# ------------------------------------------------------------- path properties

_segments = st.text(alphabet="abc.-_/\\ ", min_size=0, max_size=12)


@given(_segments)
def test_relative_path_validator_never_accepts_escaping_paths(value: str) -> None:
    try:
        result = _validate_relative_path(value)
    except ValueError:
        return
    assert not result.startswith("/")
    assert ".." not in result.split("/")
    assert "\\" not in result


@given(st.text(alphabet="abc./", min_size=1, max_size=12))
def test_container_path_validator_only_accepts_absolute_non_root_paths(value: str) -> None:
    try:
        result = _validate_container_path(value)
    except ValueError:
        return
    assert result.startswith("/")
    assert result != "/"
    assert ".." not in result.split("/")
