"""Docker sandbox backend (ADR-001).

Isolation provided by every sandbox container:

* no host mounts at all - files move only via tar archives through the Docker API;
* non-root numeric user (from the task, default ``1000:1000``);
* all Linux capabilities dropped and ``no-new-privileges`` set; never ``--privileged``;
* CPU (``nano_cpus``), memory (swap disabled) and PID limits;
* ``network_mode=none`` unless the task explicitly enables networking;
* an init process (``--init``) so orphaned children are reaped;
* labels identifying run/task/role, so orphans can always be found and removed.

What Docker isolation does **not** guarantee is documented in docs/architecture/security.md:
containers share the host kernel and are not a strong boundary against kernel exploits.

Image requirements: ``sleep`` (keeps the container alive) and GNU coreutils ``timeout``
(enforces per-command time limits inside the container). Debian/Ubuntu-based images,
including the official ``python`` images, satisfy both.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import threading
from collections.abc import Iterator
from functools import cached_property
from pathlib import PurePosixPath
from typing import IO, Any

import docker
import docker.credentials.errors
import docker.errors
from docker.models.containers import Container

from agentenvforge.domain.clock import elapsed_ms, monotonic_ms, utc_now
from agentenvforge.domain.commands import CommandExecution, CommandRequest
from agentenvforge.domain.failures import (
    SandboxBuildError,
    SandboxBuildTimeoutError,
    SandboxError,
    SandboxPathNotFoundError,
    SandboxStartError,
)
from agentenvforge.sandbox.base import (
    BackendInfo,
    ImageSpec,
    PreparedImage,
    SandboxConfig,
)
from agentenvforge.timeouts import CallTimeoutError, call_with_timeout

logger = logging.getLogger(__name__)

LABEL_MANAGED = "io.agentenvforge.managed"
LABEL_RUN_ID = "io.agentenvforge.run-id"
LABEL_TASK_ID = "io.agentenvforge.task-id"
LABEL_ROLE = "io.agentenvforge.role"

_KILL_GRACE_SECONDS = 5
"""After a command's timeout, `timeout` sends SIGTERM, then SIGKILL this much later."""
_WATCHDOG_GRACE_SECONDS = 30
"""Extra time before the host-side watchdog kills the whole container (last resort)."""
_TIMEOUT_EXIT_CODES = frozenset({124, 137})
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_SPOOL_IN_MEMORY_BYTES = 16 * 1024 * 1024
_MAX_BUILD_LOG_CHARS = 200_000
_DAEMON_HINT = "is Docker running? `agentenv doctor` diagnoses connectivity problems"
_CREDENTIAL_HINT = (
    "Docker's credential helper failed; make sure Docker's bin directory is on PATH "
    "(restart the shell after installing Docker Desktop)"
)

# Errors from the Docker SDK that are infrastructure failures. Credential-helper errors
# do not derive from DockerException, so they are listed explicitly.
_DOCKER_ERRORS = (docker.errors.DockerException, docker.credentials.errors.StoreError)


class _BoundedBuffer:
    """Collects stream output up to a byte limit, recording whether anything was dropped."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._chunks: list[bytes] = []
        self._size = 0
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        remaining = self._limit - self._size
        if remaining <= 0:
            self.truncated = self.truncated or bool(chunk)
            return
        if len(chunk) > remaining:
            chunk, self.truncated = chunk[:remaining], True
        self._chunks.append(chunk)
        self._size += len(chunk)

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


class DockerSandbox:
    """One running container. Created by :class:`DockerSandboxBackend`."""

    def __init__(self, client: docker.DockerClient, container: Container, name: str) -> None:
        self._client = client
        self._container = container
        self._name = name
        self._destroyed = False
        self._lock = threading.Lock()

    @property
    def sandbox_id(self) -> str:
        """The Docker container ID."""
        return str(self._container.id)

    @property
    def name(self) -> str:
        """The Docker container name."""
        return self._name

    def execute(self, request: CommandRequest) -> CommandExecution:
        """Run ``request`` via ``docker exec`` with an in-container ``timeout`` wrapper."""
        timeout = request.timeout_seconds
        argv = ["timeout", f"--kill-after={_KILL_GRACE_SECONDS}s", f"{timeout:g}s", *request.argv]
        stdout = _BoundedBuffer(request.output_limit_bytes)
        stderr = _BoundedBuffer(request.output_limit_bytes)
        started_at, start = utc_now(), monotonic_ms()

        # The in-container `timeout` is the primary limit. If the exec still has not
        # returned well after it (e.g. a process that escaped `timeout`'s process group
        # keeps the output stream open), the watchdog kills the whole container.
        watchdog_fired = threading.Event()

        def _watchdog() -> None:
            watchdog_fired.set()
            logger.warning("command watchdog fired; killing sandbox %s", self._name)
            self._kill_quietly()

        watchdog = threading.Timer(
            timeout + _KILL_GRACE_SECONDS + _WATCHDOG_GRACE_SECONDS, _watchdog
        )
        watchdog.daemon = True
        exit_code: int | None = None
        try:
            exec_id = self._client.api.exec_create(
                self.sandbox_id,
                argv,
                stdout=True,
                stderr=True,
                environment=dict(request.env),
                workdir=request.cwd,
            )["Id"]
            watchdog.start()
            for out_chunk, err_chunk in self._client.api.exec_start(
                exec_id, stream=True, demux=True
            ):
                if out_chunk:
                    stdout.append(out_chunk)
                if err_chunk:
                    stderr.append(err_chunk)
            raw_exit = self._client.api.exec_inspect(exec_id).get("ExitCode")
            exit_code = int(raw_exit) if raw_exit is not None else None
        except docker.errors.DockerException as error:
            if not watchdog_fired.is_set():
                raise SandboxError(
                    f"command execution failed in sandbox {self._name}: {error}",
                    details={"sandbox": self._name},
                ) from error
        finally:
            watchdog.cancel()

        duration = elapsed_ms(start)
        timed_out = watchdog_fired.is_set() or (
            exit_code in _TIMEOUT_EXIT_CODES and duration >= timeout * 1000
        )
        return CommandExecution(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=None if watchdog_fired.is_set() else exit_code,
            stdout=stdout.text(),
            stderr=stderr.text(),
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            started_at=started_at,
            duration_ms=duration,
            timed_out=timed_out,
        )

    def put_archive(self, destination: PurePosixPath, archive: IO[bytes]) -> None:
        """Extract ``archive`` into ``destination`` (performed by the Docker daemon)."""
        try:
            accepted = self._container.put_archive(destination.as_posix(), archive.read())
        except docker.errors.DockerException as error:
            raise SandboxError(
                f"cannot copy files into {self._name}:{destination}: {error}"
            ) from error
        if not accepted:
            raise SandboxError(f"Docker rejected archive for {self._name}:{destination}")

    def get_archive(self, path: PurePosixPath) -> IO[bytes]:
        """Return a tar archive of ``path``, spooled to disk when large."""
        try:
            chunks, _stat = self._container.get_archive(path.as_posix())
            buffer = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY_BYTES)  # noqa: SIM115
            size = 0
            for chunk in chunks:
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    buffer.close()
                    raise SandboxError(
                        f"{self._name}:{path} exceeds the {_MAX_ARCHIVE_BYTES} byte copy limit"
                    )
                buffer.write(chunk)
        except docker.errors.NotFound as error:
            raise SandboxPathNotFoundError(f"{self._name}:{path} does not exist") from error
        except docker.errors.DockerException as error:
            raise SandboxError(f"cannot copy {self._name}:{path}: {error}") from error
        buffer.seek(0)
        return buffer

    def attach_hint(self) -> str | None:
        """Command for a human to open a shell in this container."""
        return f"docker exec -it {self._name} sh"

    def destroy(self) -> None:
        """Force-remove the container. Idempotent; a missing container is not an error."""
        with self._lock:
            if self._destroyed:
                return
            try:
                self._container.remove(force=True, v=True)
            except docker.errors.NotFound:
                pass
            except docker.errors.APIError as error:
                # 409: removal already in progress (e.g. racing the watchdog) - fine.
                if error.status_code != 409:
                    raise SandboxError(f"cannot remove sandbox {self._name}: {error}") from error
            except docker.errors.DockerException as error:
                raise SandboxError(f"cannot remove sandbox {self._name}: {error}") from error
            self._destroyed = True

    def _kill_quietly(self) -> None:
        try:
            self._container.kill()
        except docker.errors.DockerException as error:
            # Best effort: destroy() during cleanup will retry removal and report failure.
            logger.warning("could not kill sandbox %s: %s", self._name, error)


class DockerSandboxBackend:
    """Creates :class:`DockerSandbox` containers via the Docker Engine API."""

    def __init__(self, *, docker_host: str | None = None) -> None:
        self._docker_host = docker_host

    @property
    def name(self) -> str:
        """Backend name."""
        return "docker"

    @cached_property
    def _client(self) -> docker.DockerClient:
        try:
            client = (
                docker.DockerClient(base_url=self._docker_host)
                if self._docker_host
                else docker.from_env()
            )
            client.ping()
        except docker.errors.DockerException as error:
            raise SandboxError(
                f"cannot connect to the Docker daemon ({_DAEMON_HINT}): {error}"
            ) from error
        return client

    def describe(self) -> BackendInfo:
        """Return Docker server version information."""
        try:
            version: dict[str, Any] = self._client.version()
        except docker.errors.DockerException as error:
            raise SandboxError(f"cannot query Docker version: {error}") from error
        return BackendInfo(
            name=self.name,
            version=str(version.get("Version", "unknown")),
            details={
                "api_version": str(version.get("ApiVersion", "unknown")),
                "os": str(version.get("Os", "unknown")),
                "arch": str(version.get("Arch", "unknown")),
            },
        )

    # ------------------------------------------------------------------ images

    def prepare_image(self, spec: ImageSpec) -> PreparedImage:
        """Build the task's Dockerfile (reusing a cached build) or fetch its image."""
        try:
            return call_with_timeout(
                lambda: self._prepare_image_blocking(spec),
                spec.timeout_seconds,
                name=f"image-build-{spec.task_id}",
            )
        except CallTimeoutError as error:
            raise SandboxBuildTimeoutError(
                f"image preparation exceeded {spec.timeout_seconds:g}s",
                details={"timeout_seconds": spec.timeout_seconds},
            ) from error

    def _prepare_image_blocking(self, spec: ImageSpec) -> PreparedImage:
        if spec.image is not None:
            return self._fetch_image(spec.image)
        if spec.dockerfile is None or spec.context is None:
            raise SandboxBuildError("image spec has neither an image nor a Dockerfile")

        tag = f"agentenvforge/{_docker_name(spec.task_id)}:{spec.cache_key[:24]}"
        try:
            image = self._client.images.get(tag)
            return _prepared(tag, image, cached=True)
        except docker.errors.ImageNotFound:
            pass
        except docker.errors.DockerException as error:
            raise SandboxBuildError(f"cannot inspect image {tag}: {error}") from error

        dockerfile = spec.dockerfile.relative_to(spec.context).as_posix()
        logger.info("building image %s from %s", tag, spec.dockerfile)
        try:
            image, log_stream = self._client.images.build(
                path=str(spec.context),
                dockerfile=dockerfile,
                tag=tag,
                rm=True,
                forcerm=True,
                pull=False,
                labels={LABEL_MANAGED: "true", LABEL_TASK_ID: spec.task_id},
            )
        except docker.errors.BuildError as error:
            raise SandboxBuildError(
                f"image build failed: {error.msg}",
                details={"build_log_tail": _render_build_log(error.build_log)[-4000:]},
            ) from error
        except docker.credentials.errors.StoreError as error:
            raise SandboxBuildError(f"image build failed: {_CREDENTIAL_HINT}: {error}") from error
        except docker.errors.DockerException as error:
            raise SandboxBuildError(f"image build failed: {error}") from error
        return _prepared(tag, image, cached=False, build_log=_render_build_log(log_stream))

    def _fetch_image(self, reference: str) -> PreparedImage:
        try:
            return _prepared(reference, self._client.images.get(reference), cached=True)
        except docker.errors.ImageNotFound:
            logger.info("pulling image %s", reference)
        except docker.errors.DockerException as error:
            raise SandboxBuildError(f"cannot inspect image {reference}: {error}") from error
        try:
            image = self._client.images.pull(reference)
        except _DOCKER_ERRORS as error:
            raise SandboxBuildError(f"cannot pull image {reference}: {error}") from error
        return _prepared(reference, image, cached=False)

    # --------------------------------------------------------------- sandboxes

    def create_sandbox(self, config: SandboxConfig) -> DockerSandbox:
        """Create, start and verify a hardened container for ``config``."""
        name = f"agentenv-{config.run_id}-{config.role.value}"
        environment = {"HOME": "/tmp", **config.environment}  # noqa: S108 - inside sandbox
        try:
            container = self._client.containers.run(
                config.image.image_id,
                command=["sleep", "infinity"],
                detach=True,
                name=name,
                labels={
                    LABEL_MANAGED: "true",
                    LABEL_RUN_ID: config.run_id,
                    LABEL_TASK_ID: config.task_id,
                    LABEL_ROLE: config.role.value,
                },
                user=f"{config.owner.uid}:{config.owner.gid}",
                environment=environment,
                network_mode="bridge" if config.network_enabled else "none",
                nano_cpus=int(config.cpus * 1_000_000_000),
                mem_limit=f"{config.memory_mb}m",
                memswap_limit=f"{config.memory_mb}m",
                pids_limit=config.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                init=True,
                privileged=False,
            )
        except docker.errors.DockerException as error:
            self._remove_by_name(name)
            raise SandboxStartError(f"cannot start sandbox {name}: {error}") from error

        sandbox = DockerSandbox(self._client, container, name)
        try:
            self._verify_running(container, sandbox)
        except SandboxStartError:
            sandbox.destroy()
            raise
        return sandbox

    def _verify_running(self, container: Container, sandbox: DockerSandbox) -> None:
        container.reload()
        if container.status != "running":
            logs = container.logs(tail=50).decode("utf-8", errors="replace")
            raise SandboxStartError(
                f"sandbox {sandbox.name} exited immediately (status {container.status}); "
                "the image must provide `sleep`",
                details={"container_logs": logs[-2000:]},
            )
        probe = sandbox.execute(CommandRequest(argv=("true",), timeout_seconds=30))
        if probe.exit_code != 0:
            raise SandboxStartError(
                f"sandbox {sandbox.name} cannot run commands; the image must provide GNU "
                f"coreutils `timeout` (probe exit code {probe.exit_code})",
                details={"probe_output": (probe.stdout + probe.stderr)[-2000:]},
            )

    def remove_orphaned_sandboxes(self, run_id: str | None = None) -> list[str]:
        """Remove every AgentEnvForge container (optionally only those of ``run_id``)."""
        labels = [f"{LABEL_MANAGED}=true"]
        if run_id is not None:
            labels.append(f"{LABEL_RUN_ID}={run_id}")
        try:
            containers = self._client.containers.list(all=True, filters={"label": labels})
        except docker.errors.DockerException as error:
            raise SandboxError(f"cannot list containers: {error}") from error
        removed = []
        for container in containers:
            DockerSandbox(self._client, container, str(container.name)).destroy()
            removed.append(str(container.name))
        return removed

    def _remove_by_name(self, name: str) -> None:
        # A failed `run` can leave a created-but-not-started container behind.
        try:
            self._client.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.DockerException as error:
            logger.warning("could not remove partially created sandbox %s: %s", name, error)


def _docker_name(value: str) -> str:
    """Make ``value`` safe as a Docker repository name component."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "task"


def _prepared(reference: str, image: Any, *, cached: bool, build_log: str = "") -> PreparedImage:
    return PreparedImage(
        reference=reference,
        image_id=str(image.id),
        cached=cached,
        repo_digests=tuple(image.attrs.get("RepoDigests") or ()),
        build_log=build_log,
    )


def _render_build_log(entries: Iterator[Any] | Any) -> str:
    lines: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            text = entry.get("stream") or entry.get("error") or entry.get("status")
            if text is None:
                text = json.dumps(entry)
            lines.append(str(text))
    return "".join(lines)[-_MAX_BUILD_LOG_CHARS:]
