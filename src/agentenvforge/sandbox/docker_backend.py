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

import contextvars
import importlib
import json
import logging
import re
import tempfile
import threading
import time
from collections.abc import Iterator
from functools import cached_property
from pathlib import PurePosixPath
from typing import IO, Any

import docker
import docker.credentials.errors
import docker.errors
import requests.exceptions
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
from agentenvforge.processes import Owner, process_exists
from agentenvforge.sandbox.base import (
    BackendInfo,
    ImageSpec,
    PreparedImage,
    SandboxConfig,
)
from agentenvforge.timeouts import CallTimeoutError, call_with_timeout

logger = logging.getLogger(__name__)

# Container labels, set explicitly on every sandbox. A sandbox is identified by
# LABEL_RUN_ID: Docker copies *image* labels onto any container created from an image, so
# an image label would wrongly mark containers that other tools start from our images.
LABEL_MANAGED = "io.agentenvforge.managed"
LABEL_RUN_ID = "io.agentenvforge.run-id"
LABEL_IMAGE = "io.agentenvforge.image"
"""Image label: marks images built by AgentEnvForge (never used to find sandboxes)."""
LABEL_TASK_ID = "io.agentenvforge.task-id"
LABEL_ROLE = "io.agentenvforge.role"
LABEL_OWNER = "io.agentenvforge.owner"
"""``<host hash>:<pid>`` of the creating process; orphan cleanup never removes a sandbox whose
owner is still alive (see :mod:`agentenvforge.processes`)."""

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

_EXIT_STATUS_WAIT_SECONDS = 5.0
"""How long to wait for Docker to record an exec's exit status after its output ends."""
_PROBE_ATTEMPTS = 3


def _transport_errors() -> tuple[type[BaseException], ...]:
    """Every exception class that means "talking to the Docker daemon failed".

    The SDK wraps HTTP errors in DockerException, but connection-level failures surface as
    requests exceptions, OSError, or (Windows named pipe) pywintypes.error. All of them must
    become SandboxError, or they would be misclassified and could escape cleanup.
    """
    errors: list[type[BaseException]] = [
        docker.errors.DockerException,
        docker.credentials.errors.StoreError,
        requests.exceptions.RequestException,
        OSError,
    ]
    try:
        # Imported dynamically: pywin32 exists (and is needed) only on Windows.
        pywintypes = importlib.import_module("pywintypes")
    except ImportError:
        pass
    else:
        errors.append(pywintypes.error)
    return tuple(errors)


_DOCKER_ERRORS = _transport_errors()
_START_ERRORS: tuple[type[BaseException], ...] = (SandboxError, *_DOCKER_ERRORS)


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

        # The timer thread runs in a copy of this context so its log line carries the run ID.
        watchdog = threading.Timer(
            timeout + _KILL_GRACE_SECONDS + _WATCHDOG_GRACE_SECONDS,
            contextvars.copy_context().run,
            args=(_watchdog,),
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
            exit_code = self._wait_for_exit_code(exec_id)
        except _DOCKER_ERRORS as error:
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

    def _wait_for_exit_code(self, exec_id: str) -> int | None:
        """Return the exec's exit status once Docker has recorded it.

        The end of the output stream does not by itself prove that the process exited (a
        process can close its output early, and the stream can end on a transport hiccup), so
        the state is observed instead of assumed: poll briefly until ``Running`` is false.
        """
        deadline = time.monotonic() + _EXIT_STATUS_WAIT_SECONDS
        while True:
            info = self._client.api.exec_inspect(exec_id)
            exit_code = info.get("ExitCode")
            if not info.get("Running") and exit_code is not None:
                return int(exit_code)
            if time.monotonic() >= deadline:
                logger.warning("no exit status recorded for exec %s in %s", exec_id, self._name)
                return None
            time.sleep(0.05)

    def put_archive(self, destination: PurePosixPath, archive: IO[bytes]) -> None:
        """Extract ``archive`` into ``destination`` (performed by the Docker daemon)."""
        try:
            accepted = self._container.put_archive(destination.as_posix(), archive.read())
        except _DOCKER_ERRORS as error:
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
        except _DOCKER_ERRORS as error:
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
            except _DOCKER_ERRORS as error:
                raise SandboxError(f"cannot remove sandbox {self._name}: {error}") from error
            self._destroyed = True

    def _kill_quietly(self) -> None:
        try:
            self._container.kill()
        except _DOCKER_ERRORS as error:
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
        client: docker.DockerClient | None = None
        try:
            client = (
                docker.DockerClient(base_url=self._docker_host)
                if self._docker_host
                else docker.from_env()
            )
            client.ping()
        except _DOCKER_ERRORS as error:
            if client is not None:
                client.close()  # do not leak the connection of a failed ping
            raise SandboxError(
                f"cannot connect to the Docker daemon ({_DAEMON_HINT}): {error}"
            ) from error
        return client

    def close(self) -> None:
        """Release the Docker client's connections (it is created again if used later)."""
        client = self.__dict__.pop("_client", None)
        if client is not None:
            client.close()

    def describe(self) -> BackendInfo:
        """Return Docker server version information and the resources available to it."""
        try:
            version: dict[str, Any] = self._client.version()
            info: dict[str, Any] = self._client.info()
        except _DOCKER_ERRORS as error:
            raise SandboxError(f"cannot query Docker version: {error}") from error
        return BackendInfo(
            name=self.name,
            version=str(version.get("Version", "unknown")),
            details={
                "api_version": str(version.get("ApiVersion", "unknown")),
                "os": str(version.get("Os", "unknown")),
                "arch": str(version.get("Arch", "unknown")),
                # Resources of the Docker host / VM (on Docker Desktop: the VM's allocation),
                # which bound what parallel runs can achieve.
                "cpus": str(info.get("NCPU", "unknown")),
                "memory_bytes": str(info.get("MemTotal", "unknown")),
                "operating_system": str(info.get("OperatingSystem", "unknown")),
                "storage_driver": str(info.get("Driver", "unknown")),
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
        except _DOCKER_ERRORS as error:
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
                labels={LABEL_IMAGE: "true", LABEL_TASK_ID: spec.task_id},
            )
        except docker.errors.BuildError as error:
            raise SandboxBuildError(
                f"image build failed: {error.msg}",
                details={"build_log_tail": _render_build_log(error.build_log)[-4000:]},
            ) from error
        except docker.credentials.errors.StoreError as error:
            raise SandboxBuildError(f"image build failed: {_CREDENTIAL_HINT}: {error}") from error
        except _DOCKER_ERRORS as error:
            raise SandboxBuildError(f"image build failed: {error}") from error
        return _prepared(tag, image, cached=False, build_log=_render_build_log(log_stream))

    def _fetch_image(self, reference: str) -> PreparedImage:
        try:
            return _prepared(reference, self._client.images.get(reference), cached=True)
        except docker.errors.ImageNotFound:
            logger.info("pulling image %s", reference)
        except _DOCKER_ERRORS as error:
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
                    LABEL_OWNER: Owner.current().token(),
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
        except _DOCKER_ERRORS as error:
            self._remove_by_name(name)
            raise SandboxStartError(f"cannot start sandbox {name}: {error}") from error

        sandbox = DockerSandbox(self._client, container, name)
        try:
            self._verify_running(container, sandbox)
        except _START_ERRORS as error:
            # The container exists now, but the caller only learns about sandboxes that are
            # returned: remove it here, whatever went wrong, or it would outlive the run.
            self._destroy_quietly(sandbox)
            if isinstance(error, SandboxStartError):
                raise
            raise SandboxStartError(
                f"sandbox {name} failed its readiness check: {error}",
                details={"sandbox": name},
            ) from error
        return sandbox

    @staticmethod
    def _destroy_quietly(sandbox: DockerSandbox) -> None:
        try:
            sandbox.destroy()
        except SandboxError as error:
            # Reported, not raised: the readiness failure is the root cause. The run's final
            # label-based sweep (or `agentenv cleanup`) removes the container later.
            logger.warning(
                "could not remove sandbox %s after a failed start: %s", sandbox.name, error
            )

    def _verify_running(self, container: Container, sandbox: DockerSandbox) -> None:
        container.reload()
        if container.status != "running":
            logs = container.logs(tail=50).decode("utf-8", errors="replace")
            raise SandboxStartError(
                f"sandbox {sandbox.name} exited immediately (status {container.status}); "
                "the image must provide `sleep`",
                details={"container_logs": logs[-2000:]},
            )
        probe = self._probe(sandbox)
        if probe.exit_code != 0:
            raise SandboxStartError(
                f"sandbox {sandbox.name} cannot run commands; the image must provide GNU "
                f"coreutils `timeout` (probe exit code {probe.exit_code})",
                details={"probe_output": (probe.stdout + probe.stderr)[-2000:]},
            )

    @staticmethod
    def _probe(sandbox: DockerSandbox) -> CommandExecution:
        # `true` is idempotent, so a transport error on the first command after start is
        # retried a few times; a non-zero exit is deterministic and is never retried.
        for attempt in range(1, _PROBE_ATTEMPTS + 1):
            try:
                return sandbox.execute(CommandRequest(argv=("true",), timeout_seconds=30))
            except SandboxError:
                if attempt == _PROBE_ATTEMPTS:
                    raise
                logger.warning("readiness probe of %s failed (attempt %d)", sandbox.name, attempt)
                time.sleep(0.5 * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def remove_orphaned_sandboxes(
        self, run_id: str | None = None, *, include_live: bool = False
    ) -> list[str]:
        """Remove sandboxes left behind by processes that no longer exist.

        With ``run_id``, every sandbox of that run is removed (an explicit request). Without
        it, only sandboxes whose owner process on this host is gone are removed, so a run in
        another terminal is never disturbed; ``include_live`` removes all AgentEnvForge
        sandboxes regardless of owner (also those of other hosts or of older versions).
        """
        # Requiring the run-id label selects only containers AgentEnvForge created itself.
        labels = [
            f"{LABEL_MANAGED}=true",
            LABEL_RUN_ID if run_id is None else f"{LABEL_RUN_ID}={run_id}",
        ]
        try:
            containers = self._client.containers.list(all=True, filters={"label": labels})
        except _DOCKER_ERRORS as error:
            raise SandboxError(f"cannot list containers: {error}") from error
        removed = []
        for container in containers:
            if run_id is None and not include_live and not _owner_is_gone(container):
                continue
            DockerSandbox(self._client, container, str(container.name)).destroy()
            removed.append(str(container.name))
        return removed

    def _remove_by_name(self, name: str) -> None:
        # A failed `run` can leave a created-but-not-started container behind.
        try:
            self._client.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass
        except _DOCKER_ERRORS as error:
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


def _owner_is_gone(container: Container) -> bool:
    """True when the container's owning process ran on this host and no longer exists."""
    owner = Owner.parse((container.labels or {}).get(LABEL_OWNER))
    return owner is not None and owner.is_local() and not process_exists(owner.pid)
