"""The sandbox extension point.

A *sandbox backend* turns an image description into running, isolated *sandboxes*.
The orchestrator, agents and graders only ever talk to these protocols, so adding a
Podman, Firecracker or Kubernetes backend means implementing two small classes rather
than touching the execution engine.

The protocol is intentionally narrow: execute a command, copy a tar archive in, copy a
tar archive out, destroy. Higher-level helpers (upload a directory, read a file) are
built on top of it in :mod:`agentenvforge.sandbox.transfer`, so every backend gets them
for free and behaves identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import IO, Protocol, runtime_checkable

from agentenvforge.domain.commands import CommandExecution, CommandRequest
from agentenvforge.workspace.archive import Ownership


class SandboxRole(StrEnum):
    """Why a sandbox exists. Agent and grader never share a sandbox (ADR-007)."""

    AGENT = "agent"
    GRADER = "grader"


@dataclass(frozen=True, slots=True)
class ImageSpec:
    """What image a task needs. Exactly one of ``dockerfile`` or ``image`` is set."""

    task_id: str
    dockerfile: Path | None
    context: Path | None
    image: str | None
    cache_key: str
    """Fingerprint of the build inputs; identical keys may reuse a previous build."""
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """An image ready to start sandboxes from."""

    reference: str
    """Tag or reference used to locate the image."""
    image_id: str
    """Content-addressed ID (e.g. ``sha256:...``) recorded for reproducibility."""
    cached: bool
    repo_digests: tuple[str, ...] = ()
    build_log: str = ""


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Everything needed to start one sandbox. All limits here are enforced."""

    run_id: str
    task_id: str
    role: SandboxRole
    image: PreparedImage
    cpus: float
    memory_mb: int
    pids_limit: int
    network_enabled: bool
    owner: Ownership
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BackendInfo:
    """Description of a backend for diagnostics and run manifests."""

    name: str
    version: str
    details: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Sandbox(Protocol):
    """One running, isolated environment."""

    @property
    def sandbox_id(self) -> str:
        """Backend-specific identifier (e.g. the container ID)."""
        ...

    @property
    def name(self) -> str:
        """Human-friendly unique name, used in logs."""
        ...

    def execute(self, request: CommandRequest) -> CommandExecution:
        """Run a command and wait for it, enforcing ``request.timeout_seconds``.

        A command that times out is reported with ``timed_out=True``; it is not an
        exception, because timing out is an expected outcome of running untrusted code.
        Exceptions are reserved for infrastructure failures.
        """
        ...

    def put_archive(self, destination: PurePosixPath, archive: IO[bytes]) -> None:
        """Extract a tar ``archive`` into the existing directory ``destination``."""
        ...

    def get_archive(self, path: PurePosixPath) -> IO[bytes]:
        """Return a tar archive of ``path``; raises :class:`SandboxPathNotFoundError`."""
        ...

    def attach_hint(self) -> str | None:
        """A shell command a human can use to enter the sandbox, if supported."""
        ...

    def destroy(self) -> None:
        """Stop and remove the sandbox. Must be idempotent and safe to call on failure."""
        ...


@runtime_checkable
class SandboxBackend(Protocol):
    """Creates sandboxes. Implementations: :class:`~.docker.DockerSandboxBackend`."""

    @property
    def name(self) -> str:
        """Short backend name, e.g. ``docker``."""
        ...

    def describe(self) -> BackendInfo:
        """Return version information; raises ``SandboxError`` if unavailable."""
        ...

    def prepare_image(self, spec: ImageSpec) -> PreparedImage:
        """Build or fetch the image described by ``spec``."""
        ...

    def create_sandbox(self, config: SandboxConfig) -> Sandbox:
        """Create and start a sandbox. The caller owns its destruction."""
        ...

    def remove_orphaned_sandboxes(
        self, run_id: str | None = None, *, include_live: bool = False
    ) -> list[str]:
        """Remove sandboxes left behind by crashed processes; return their names.

        With ``run_id``: every sandbox of that run. Without: only sandboxes whose owning
        process no longer exists, unless ``include_live`` (then every sandbox).
        """
        ...
