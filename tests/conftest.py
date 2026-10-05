"""Shared fixtures."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPOSITORY_ROOT / "examples" / "basic"
EXAMPLE_TASK = EXAMPLE_DIR / "task.yaml"
REFERENCE_AGENT = EXAMPLE_DIR / "agents" / "reference.yaml"


@cache
def docker_available() -> bool:
    """True when a Docker daemon answers (checked once per session)."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.close()
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return False
    return True


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``docker``-marked tests when no daemon is reachable, with a clear reason."""
    if docker_available():
        return
    skip = pytest.mark.skip(reason="Docker daemon not reachable (run `agentenv doctor`)")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def agentenv_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated AGENTENV_HOME for the test."""
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTENV_HOME", str(home))
    for variable in [
        name for name in os.environ if name.startswith("AGENTENV_") and name != "AGENTENV_HOME"
    ]:
        monkeypatch.delenv(variable)
    return home


@pytest.fixture
def example_copy(tmp_path: Path) -> Callable[[Callable[[str], str] | None], Path]:
    """Copy the example task to a temp dir, optionally rewriting task.yaml; return its path."""

    def make(edit: Callable[[str], str] | None = None) -> Path:
        destination = tmp_path / "example"
        shutil.copytree(EXAMPLE_DIR, destination)
        task = destination / "task.yaml"
        if edit is not None:
            task.write_text(edit(task.read_text(encoding="utf-8")), encoding="utf-8")
        return task

    return make


@pytest.fixture
def write_task(tmp_path: Path) -> Iterator[Callable[[str], Path]]:
    """Write a task YAML document (plus a minimal project/ and Dockerfile) and return its path."""
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "main.py").write_text("print('hi')\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "grader").mkdir()

    def write(text: str) -> Path:
        path = tmp_path / "task.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    yield write


MINIMAL_TASK = """\
schema_version: "1"
id: minimal-task
name: Minimal task
instructions: Do the thing.
source:
  type: local
  path: project
environment:
  dockerfile: Dockerfile
grading:
  graders:
    - type: command
      command: ["true"]
"""
