"""Environment diagnostics behind ``agentenv doctor``.

Checks return plain :class:`CheckResult` values; rendering is the CLI's job. Each failing
check carries a hint that tells the user what to do next.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata

from agentenvforge._version import __version__
from agentenvforge.config.settings import Settings
from agentenvforge.domain.failures import AgentEnvForgeError
from agentenvforge.sandbox.base import SandboxBackend

MINIMUM_PYTHON = (3, 12)
_DISK_FAIL_BYTES = 1 * 1024**3
_DISK_WARN_BYTES = 5 * 1024**3


class CheckStatus(StrEnum):
    """Outcome of one diagnostic check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One line of the doctor report."""

    name: str
    status: CheckStatus
    detail: str
    hint: str = ""


def check_python() -> CheckResult:
    """The interpreter satisfies the minimum supported version."""
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] < MINIMUM_PYTHON:
        return CheckResult(
            "Python", CheckStatus.FAIL, version, "AgentEnvForge requires Python 3.12+"
        )
    return CheckResult("Python", CheckStatus.OK, version)


def check_git() -> CheckResult:
    """``git`` is available (needed for tasks with git sources)."""
    executable = shutil.which("git")
    if executable is None:
        return CheckResult(
            "Git", CheckStatus.WARN, "not found", "install git to run git-sourced tasks"
        )
    completed = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=10, check=False
    )
    return CheckResult("Git", CheckStatus.OK, completed.stdout.strip().removeprefix("git version "))


def check_docker(backend_factory: Callable[[], SandboxBackend]) -> list[CheckResult]:
    """The Docker SDK is installed and the daemon answers."""
    # Read the version from package metadata: only the Docker backend imports the SDK.
    try:
        sdk_version = metadata.version("docker")
    except metadata.PackageNotFoundError:
        return [
            CheckResult("Docker SDK", CheckStatus.FAIL, "not installed", "reinstall agentenvforge")
        ]
    results = [CheckResult("Docker SDK", CheckStatus.OK, sdk_version)]
    try:
        info = backend_factory().describe()
    except AgentEnvForgeError as error:
        results.append(
            CheckResult(
                "Docker daemon",
                CheckStatus.FAIL,
                "unreachable",
                f"start Docker Desktop / the Docker service ({error.message})",
            )
        )
        return results
    results.append(CheckResult("Docker daemon", CheckStatus.OK, f"Engine {info.version}"))
    results.append(
        CheckResult(
            "Docker API",
            CheckStatus.OK,
            f"API {info.details.get('api_version')} ({info.details.get('os')}/"
            f"{info.details.get('arch')})",
        )
    )
    return results


def check_home(settings: Settings) -> list[CheckResult]:
    """The data directory is writable and has free space."""
    try:
        settings.home.mkdir(parents=True, exist_ok=True)
        probe = settings.home / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        return [CheckResult("Data directory", CheckStatus.FAIL, str(settings.home), str(error))]
    results = [CheckResult("Data directory", CheckStatus.OK, str(settings.home))]
    free = shutil.disk_usage(settings.home).free
    detail = f"{free / 1024**3:.1f} GiB free"
    if free < _DISK_FAIL_BYTES:
        results.append(CheckResult("Disk space", CheckStatus.FAIL, detail, "free up disk space"))
    elif free < _DISK_WARN_BYTES:
        results.append(
            CheckResult("Disk space", CheckStatus.WARN, detail, "images need several GiB")
        )
    else:
        results.append(CheckResult("Disk space", CheckStatus.OK, detail))
    return results


def check_database(settings: Settings) -> CheckResult:
    """The run database opens and is migrated."""
    from agentenvforge.persistence.sqlite import SqliteRunRepository  # noqa: PLC0415

    try:
        repository = SqliteRunRepository(settings.database_path)
    except Exception as error:  # noqa: BLE001 - any failure here is the diagnosis
        return CheckResult("Database", CheckStatus.FAIL, str(settings.database_path), str(error))
    try:
        return CheckResult(
            "Database",
            CheckStatus.OK,
            f"schema v{repository.schema_version} ({settings.database_path})",
        )
    finally:
        repository.close()


def run_checks(
    settings: Settings, backend_factory: Callable[[], SandboxBackend]
) -> list[CheckResult]:
    """Run every check in display order."""
    return [
        CheckResult("AgentEnvForge", CheckStatus.OK, __version__),
        check_python(),
        check_git(),
        *check_docker(backend_factory),
        *check_home(settings),
        check_database(settings),
    ]
