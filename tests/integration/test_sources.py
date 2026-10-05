"""Source preparation against real git repositories (local file:// remotes)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from agentenvforge.domain.failures import SourcePreparationError
from agentenvforge.sources import prepare_source
from agentenvforge.tasks.loader import load_task
from tests.conftest import MINIMAL_TASK

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
GIT = [
    "git",
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.com",
    "-c",
    "core.autocrlf=false",
]


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository with two commits; returns (path, first_sha, second_sha)."""
    repo = tmp_path / "upstream"
    (repo / "pkg").mkdir(parents=True)
    subprocess.run([*GIT, "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "pkg" / "module.py").write_text("VERSION = 1\n")
    subprocess.run([*GIT, "add", "."], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "one"], cwd=repo, check=True)
    first = subprocess.run(
        [*GIT, "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "pkg" / "module.py").write_text("VERSION = 2\n")
    subprocess.run([*GIT, "commit", "-q", "-am", "two"], cwd=repo, check=True)
    second = subprocess.run(
        [*GIT, "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, first, second


def _git_task(
    write_task: Callable[[str], Path], repo: Path, revision: str, extra: str = ""
) -> Path:
    source = f"  type: git\n  repository: {repo.as_uri()}\n  revision: {revision}\n{extra}"
    return write_task(MINIMAL_TASK.replace("  type: local\n  path: project\n", source))


def test_checkout_of_pinned_commit_records_resolved_sha(
    write_task: Callable[[str], Path], repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, first, _ = repository
    task = load_task(_git_task(write_task, repo, first))
    prepared = prepare_source(task, tmp_path / "checkout", timeout_seconds=60)
    assert prepared.resolved_commit == first
    assert (prepared.path / "pkg" / "module.py").read_text() == "VERSION = 1\n"
    assert task.warnings == ()


def test_branch_revision_resolves_to_current_sha_with_warning(
    write_task: Callable[[str], Path], repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, _, second = repository
    task = load_task(_git_task(write_task, repo, "main"))
    assert task.warnings
    assert prepare_source(task, tmp_path / "checkout", timeout_seconds=60).resolved_commit == second


def test_subdirectory_checkout(
    write_task: Callable[[str], Path], repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, first, _ = repository
    task = load_task(_git_task(write_task, repo, first, "  subdirectory: pkg\n"))
    prepared = prepare_source(task, tmp_path / "checkout", timeout_seconds=60)
    assert sorted(path.name for path in prepared.path.iterdir()) == ["module.py"]
    assert not (tmp_path / "checkout-clone").exists()


def test_unknown_revision_is_a_source_failure(
    write_task: Callable[[str], Path], repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, _, _ = repository
    task = load_task(_git_task(write_task, repo, '"' + "0" * 40 + '"'))
    with pytest.raises(SourcePreparationError, match="checkout failed"):
        prepare_source(task, tmp_path / "checkout", timeout_seconds=60)
