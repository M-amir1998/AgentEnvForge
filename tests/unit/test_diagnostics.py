"""``agentenv doctor`` checks (with fake backends; no Docker required)."""

from __future__ import annotations

from pathlib import Path

from agentenvforge.config.settings import load_settings
from agentenvforge.diagnostics import CheckStatus, run_checks
from agentenvforge.domain.failures import SandboxError
from agentenvforge.sandbox.base import BackendInfo, SandboxBackend
from tests.fakes import FakeBackend


class UnreachableBackend(FakeBackend):
    def describe(self) -> BackendInfo:
        raise SandboxError("cannot connect to the Docker daemon")


def statuses(home: Path, backend: SandboxBackend) -> dict[str, CheckStatus]:
    settings = load_settings(environ={"AGENTENV_HOME": str(home)})
    return {check.name: check.status for check in run_checks(settings, lambda: backend)}


def test_healthy_environment_reports_ok(tmp_path: Path) -> None:
    result = statuses(tmp_path / "home", FakeBackend(root=tmp_path))
    assert result["Docker daemon"] is CheckStatus.OK
    assert result["Database"] is CheckStatus.OK
    assert result["Data directory"] is CheckStatus.OK
    assert result["Python"] is CheckStatus.OK


def test_unreachable_daemon_fails_with_hint(tmp_path: Path) -> None:
    settings = load_settings(environ={"AGENTENV_HOME": str(tmp_path / "home")})
    checks = run_checks(settings, lambda: UnreachableBackend(root=tmp_path))
    daemon = next(check for check in checks if check.name == "Docker daemon")
    assert daemon.status is CheckStatus.FAIL
    assert "start Docker" in daemon.hint
    assert not any(check.name == "Docker API" for check in checks)
