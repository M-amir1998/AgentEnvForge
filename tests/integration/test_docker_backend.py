"""DockerSandboxBackend against a real Docker daemon.

These tests verify that every isolation property the documentation claims is actually
applied to the container, not merely requested.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import docker
import pytest

from agentenvforge.domain.commands import CommandRequest
from agentenvforge.domain.failures import SandboxBuildError, SandboxPathNotFoundError
from agentenvforge.domain.ids import new_run_id
from agentenvforge.sandbox.base import ImageSpec, PreparedImage, SandboxConfig, SandboxRole
from agentenvforge.sandbox.docker_backend import (
    LABEL_MANAGED,
    LABEL_RUN_ID,
    DockerSandbox,
    DockerSandboxBackend,
)
from agentenvforge.sandbox.transfer import download_directory, read_file, upload_directory
from agentenvforge.workspace.archive import Ownership

pytestmark = pytest.mark.docker

IMAGE = "python:3.12-slim"
FORK_UNTIL_LIMITED = """
import os, time
forked = 0
try:
    for _ in range(500):
        if os.fork() == 0:
            time.sleep(5)
            os._exit(0)
        forked += 1
except OSError:
    print("limited at", forked)
"""


@pytest.fixture(scope="module")
def backend() -> DockerSandboxBackend:
    return DockerSandboxBackend()


@pytest.fixture(scope="module")
def image(backend: DockerSandboxBackend) -> PreparedImage:
    spec = ImageSpec(
        task_id="it", dockerfile=None, context=None, image=IMAGE, cache_key="", timeout_seconds=600
    )
    return backend.prepare_image(spec)


@pytest.fixture
def sandbox(backend: DockerSandboxBackend, image: PreparedImage) -> Iterator[DockerSandbox]:
    config = SandboxConfig(
        run_id=new_run_id(),
        task_id="it",
        role=SandboxRole.AGENT,
        image=image,
        cpus=0.5,
        memory_mb=256,
        pids_limit=64,
        network_enabled=False,
        owner=Ownership(1000, 1000),
        environment={"TASK_VAR": "value"},
    )
    created = backend.create_sandbox(config)
    yield created
    created.destroy()


def run(
    sandbox: DockerSandbox, *argv: str, timeout: float = 30, limit: int = 1024 * 1024
) -> tuple[int | None, str, bool]:
    result = sandbox.execute(
        CommandRequest(argv=argv, timeout_seconds=timeout, output_limit_bytes=limit)
    )
    return result.exit_code, result.stdout + result.stderr, result.timed_out


def test_container_is_hardened_as_documented(sandbox: DockerSandbox) -> None:
    attrs = docker.from_env().containers.get(sandbox.sandbox_id).attrs
    host = attrs["HostConfig"]
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["NetworkMode"] == "none"
    assert host["NanoCpus"] == 500_000_000
    assert host["Memory"] == host["MemorySwap"] == 256 * 1024 * 1024
    assert host["PidsLimit"] == 64
    assert host["Init"] is True
    assert not attrs["Mounts"]
    assert attrs["Config"]["User"] == "1000:1000"


def test_commands_run_as_unprivileged_user_with_task_environment(sandbox: DockerSandbox) -> None:
    code, output, _ = run(sandbox, "sh", "-c", "id -u; echo $TASK_VAR; echo $HOME")
    assert code == 0
    assert output.split() == ["1000", "value", "/tmp"]


def test_network_is_disabled(sandbox: DockerSandbox) -> None:
    code, output, _ = run(
        sandbox, "python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), 3)"
    )
    assert code != 0
    assert "unreachable" in output.lower() or "error" in output.lower()


def test_command_timeout_is_enforced(sandbox: DockerSandbox) -> None:
    code, _, timed_out = run(sandbox, "sleep", "30", timeout=1)
    assert timed_out
    assert code == 124


def test_memory_limit_kills_without_reporting_timeout(sandbox: DockerSandbox) -> None:
    code, _, timed_out = run(sandbox, "python", "-c", "b = bytearray(1024 * 1024 * 1024)")
    assert code == 137  # SIGKILL by the OOM killer
    assert not timed_out


def test_pid_limit_contains_fork_bombs(sandbox: DockerSandbox) -> None:
    # Fork children until the PID limit (64) refuses; children sleep so they stay alive.
    _code, output, _ = run(sandbox, "python", "-c", FORK_UNTIL_LIMITED, timeout=20)
    assert "limited at" in output, output


def test_output_is_bounded(sandbox: DockerSandbox) -> None:
    result = sandbox.execute(
        CommandRequest(
            argv=("python", "-c", "print('x' * 100000)"),
            timeout_seconds=30,
            output_limit_bytes=1000,
        )
    )
    assert len(result.stdout) == 1000
    assert result.stdout_truncated


def test_directory_round_trip(sandbox: DockerSandbox, tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "a.py").write_text("A = 1\n")
    upload_directory(sandbox, source, PurePosixPath("/workspace"), owner=Ownership(1000, 1000))
    code, _, _ = run(
        sandbox, "sh", "-c", "echo new > /workspace/new.txt && ls -n /workspace | grep pkg"
    )
    assert code == 0

    download_directory(sandbox, PurePosixPath("/workspace"), tmp_path / "back")
    assert (tmp_path / "back" / "pkg" / "a.py").read_text() == "A = 1\n"
    assert read_file(sandbox, PurePosixPath("/workspace/new.txt")) == b"new\n"
    with pytest.raises(SandboxPathNotFoundError):
        read_file(sandbox, PurePosixPath("/workspace/missing"))


def test_destroy_is_idempotent_and_orphans_are_found(
    backend: DockerSandboxBackend, image: PreparedImage
) -> None:
    run_id = new_run_id()
    config = SandboxConfig(
        run_id=run_id,
        task_id="it",
        role=SandboxRole.GRADER,
        image=image,
        cpus=0.5,
        memory_mb=128,
        pids_limit=32,
        network_enabled=False,
        owner=Ownership(1000, 1000),
    )
    backend.create_sandbox(config)  # "forgotten" by a crashed process
    labelled = docker.from_env().containers.list(
        all=True, filters={"label": f"{LABEL_RUN_ID}={run_id}"}
    )
    assert len(labelled) == 1
    assert backend.remove_orphaned_sandboxes(run_id) == [f"agentenv-{run_id}-grader"]
    assert backend.remove_orphaned_sandboxes(run_id) == []


def test_containers_started_by_others_from_our_images_are_not_sandboxes(
    backend: DockerSandboxBackend, image: PreparedImage
) -> None:
    """Regression: image labels are inherited by every container created from the image,
    so a user's `docker run <our image>` must not be mistaken for (and deleted as) a sandbox."""
    client = docker.from_env()
    foreign = client.containers.create(
        image.image_id, command=["true"], labels={LABEL_MANAGED: "true"}
    )
    try:
        assert foreign.name not in backend.remove_orphaned_sandboxes()
        foreign.reload()  # still exists
    finally:
        foreign.remove(force=True)


def test_dockerfile_build_is_cached_and_failures_are_classified(
    backend: DockerSandboxBackend, tmp_path: Path
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    (context / "Dockerfile").write_text(f"FROM {IMAGE}\nRUN echo built > /built.txt\n")
    spec = ImageSpec(
        task_id="it-build",
        dockerfile=context / "Dockerfile",
        context=context,
        image=None,
        cache_key=new_run_id(),
        timeout_seconds=600,
    )
    first = backend.prepare_image(spec)
    second = backend.prepare_image(spec)
    assert not first.cached and second.cached
    assert first.image_id == second.image_id
    docker.from_env().images.remove(first.reference, force=True)

    (context / "Dockerfile").write_text(f"FROM {IMAGE}\nRUN exit 3\n")
    failing = ImageSpec(
        task_id="it-build",
        dockerfile=context / "Dockerfile",
        context=context,
        image=None,
        cache_key=new_run_id(),
        timeout_seconds=600,
    )
    with pytest.raises(SandboxBuildError, match="image build failed"):
        backend.prepare_image(failing)
