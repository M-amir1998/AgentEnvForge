"""Settings and their precedence.

Precedence, lowest to highest (documented in docs/development/configuration.md)::

    built-in defaults  <  config file  <  AGENTENV_* environment variables  <  CLI flags

``home`` decides where the config file lives, so it can only be set by an environment
variable or CLI flag, never by the config file itself.

All configuration is resolved once, at the process edge (the CLI), into an immutable
:class:`Settings` object that is passed explicitly to the components that need it.
Nothing below the CLI reads environment variables directly.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentenvforge.domain.failures import ConfigurationError, ValidationIssue
from agentenvforge.tasks.documents import issues_from_pydantic

ENV_PREFIX = "AGENTENV_"
CONFIG_FILE_NAME = "config.toml"
DEFAULT_HOME = Path("~/.agentenvforge")

LogFormat = Literal["text", "json"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseModel):
    """Resolved runtime settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    home: Path = DEFAULT_HOME
    docker_host: str | None = Field(
        default=None, description="Docker endpoint; defaults to Docker's own resolution."
    )
    log_level: LogLevel = "WARNING"
    log_format: LogFormat = "text"
    sandbox_start_timeout_seconds: float = Field(default=120.0, gt=0)
    cleanup_timeout_seconds: float = Field(default=60.0, gt=0)
    keep_workspace: bool = Field(
        default=False, description="Keep per-run host working directories for debugging."
    )

    @property
    def database_path(self) -> Path:
        """SQLite database holding runs, events and artifact records."""
        return self.home / "agentenv.db"

    @property
    def artifacts_dir(self) -> Path:
        """Root of the local artifact store."""
        return self.home / "artifacts"

    @property
    def work_dir(self) -> Path:
        """Root of per-run temporary working directories."""
        return self.home / "work"


_ENV_FIELDS = tuple(name for name in Settings.model_fields if name != "home")


def load_settings(
    *,
    cli_overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Resolve settings from all sources in precedence order."""
    environment = os.environ if environ is None else environ
    overrides = {key: value for key, value in (cli_overrides or {}).items() if value is not None}

    home_value = overrides.get("home") or environment.get(f"{ENV_PREFIX}HOME") or DEFAULT_HOME
    home = Path(home_value).expanduser().resolve()

    merged: dict[str, Any] = {}
    merged.update(_read_config_file(home / CONFIG_FILE_NAME))
    for field in _ENV_FIELDS:
        env_value = environment.get(f"{ENV_PREFIX}{field.upper()}")
        if env_value is not None:
            merged[field] = env_value
    merged.update(overrides)
    merged["home"] = home

    try:
        return Settings.model_validate(merged)
    except ValidationError as error:
        raise ConfigurationError("settings", issues_from_pydantic(error)) from error


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(
            str(path),
            [ValidationIssue(field="<file>", reason=f"cannot parse config file: {error}")],
        ) from error
    if "home" in data:
        raise ConfigurationError(
            str(path), [ValidationIssue(field="home", reason="cannot be set in the config file")]
        )
    return data
