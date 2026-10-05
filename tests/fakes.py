"""Test doubles for the sandbox protocols.

``FakeSandbox`` stores the "container filesystem" in a host temporary directory, so file
transfers exercise the real archive code. Commands are not executed; a ``CommandHandler``
decides their outcome and may modify the fake filesystem, which is how tests simulate
agents editing files and graders writing reports.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO

from agentenvforge.domain.clock import utc_now
from agentenvforge.domain.commands import CommandExecution, CommandRequest
from agentenvforge.domain.failures import SandboxError, SandboxPathNotFoundError
from agentenvforge.sandbox.base import (
    BackendInfo,
    ImageSpec,
    PreparedImage,
    SandboxConfig,
    SandboxRole,
)
from agentenvforge.workspace.archive import extract_archive


def make_execution(
    request: CommandRequest,
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    duration_ms: int = 5,
) -> CommandExecution:
    """Build a :class:`CommandExecution` answering ``request``."""
    return CommandExecution(
        argv=request.argv,
        cwd=request.cwd,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        started_at=utc_now(),
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


type CommandHandler = Callable[[CommandRequest, "FakeSandbox"], CommandExecution]


def succeed(request: CommandRequest, _sandbox: FakeSandbox) -> CommandExecution:
    """Default handler: every command succeeds without output."""
    return make_execution(request)


class FakeSandbox:
    """In-process stand-in for a container."""

    def __init__(self, root: Path, name: str, handler: CommandHandler) -> None:
        self.root = root
        self._name = name
        self.handler = handler
        self.requests: list[CommandRequest] = []
        self.destroy_calls = 0
        self.fail_destroy = False

    @property
    def sandbox_id(self) -> str:
        return f"id-{self._name}"

    @property
    def name(self) -> str:
        return self._name

    def host_path(self, path: PurePosixPath | str) -> Path:
        """Map a container path to its location in the fake filesystem."""
        return self.root / PurePosixPath(path).relative_to("/")

    def execute(self, request: CommandRequest) -> CommandExecution:
        if self.destroy_calls:
            raise SandboxError(f"{self._name} is destroyed")
        self.requests.append(request)
        return self.handler(request, self)

    def put_archive(self, destination: PurePosixPath, archive: IO[bytes]) -> None:
        target = self.host_path(destination)
        if not target.is_dir():
            target.mkdir(parents=True)
        extract_archive(archive, target)

    def get_archive(self, path: PurePosixPath) -> IO[bytes]:
        source = self.host_path(path)
        if not source.exists():
            raise SandboxPathNotFoundError(f"{path} does not exist")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            archive.add(source, arcname=source.name)
        buffer.seek(0)
        return buffer

    def attach_hint(self) -> str | None:
        return f"fake-attach {self._name}"

    def destroy(self) -> None:
        self.destroy_calls += 1
        if self.fail_destroy:
            raise SandboxError(f"cannot remove {self._name}")


@dataclass
class FakeBackend:
    """Backend producing :class:`FakeSandbox` instances, one directory per sandbox."""

    root: Path
    handlers: dict[SandboxRole, CommandHandler] = field(default_factory=dict)
    build_error: Exception | None = None
    start_error: Exception | None = None
    sandboxes: list[FakeSandbox] = field(default_factory=list)
    prepared: list[ImageSpec] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    def describe(self) -> BackendInfo:
        return BackendInfo(name="fake", version="1.0")

    def prepare_image(self, spec: ImageSpec) -> PreparedImage:
        self.prepared.append(spec)
        if self.build_error is not None:
            raise self.build_error
        return PreparedImage(reference="fake:latest", image_id="sha256:fake", cached=False)

    def create_sandbox(self, config: SandboxConfig) -> FakeSandbox:
        if self.start_error is not None:
            raise self.start_error
        name = f"{config.run_id}-{config.role.value}"
        directory = self.root / name
        (directory / "tmp").mkdir(parents=True)
        sandbox = FakeSandbox(directory, name, self.handlers.get(config.role, succeed))
        self.sandboxes.append(sandbox)
        return sandbox

    def remove_orphaned_sandboxes(self, run_id: str | None = None) -> list[str]:
        return []

    def sandbox(self, role: SandboxRole) -> FakeSandbox:
        """Return the (single) sandbox created for ``role``."""
        matches = [s for s in self.sandboxes if s.name.endswith(f"-{role.value}")]
        assert len(matches) == 1, f"expected one {role} sandbox, found {len(matches)}"
        return matches[0]


def junit_report(*, tests: int, failures: int = 0, errors: int = 0, skipped: int = 0) -> bytes:
    """A minimal JUnit XML document with the given counters."""
    return (
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{tests}" '
        f'failures="{failures}" errors="{errors}" skipped="{skipped}"/></testsuites>'
    ).encode()


def pytest_handler(
    *, tests: int, failures: int = 0, errors: int = 0, exit_code: int | None = None
) -> CommandHandler:
    """Handler that answers pytest invocations by writing a JUnit report."""

    def handle(request: CommandRequest, sandbox: FakeSandbox) -> CommandExecution:
        for argument in request.argv:
            if argument.startswith("--junitxml="):
                report = sandbox.host_path(argument.removeprefix("--junitxml="))
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_bytes(junit_report(tests=tests, failures=failures, errors=errors))
        code = exit_code if exit_code is not None else (1 if failures or errors else 0)
        return make_execution(request, exit_code=code, stdout=f"{tests} tests")

    return handle
