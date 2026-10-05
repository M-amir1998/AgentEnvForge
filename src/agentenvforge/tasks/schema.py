"""Task specification, schema version 1.

A task file describes *what* to run (source, environment, grading) and *within which
limits*; it never describes *which agent* runs it. Agents are supplied separately at run
time so one task can evaluate many agents (see ADR-003).

Compatibility policy (ADR-002): the ``schema_version`` field selects the model. Adding an
optional field with a default is a compatible change within version 1. Removing,
renaming or changing the meaning of a field requires version 2 and a loader that keeps
accepting version 1 files.

Paths come in two flavours, validated differently:

* **host paths** (``source.path``, ``environment.dockerfile``, ``grading.assets`` ...) are
  relative to the task file and must stay inside the task's directory; the loader checks
  this so a task cannot copy arbitrary host files (e.g. ``~/.ssh``) into a sandbox.
* **container paths** are POSIX paths inside the sandbox: either absolute
  (``environment.workspace``) or relative to the workspace (``grading.protected_paths``).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = ("1",)

_TASK_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"
_ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_USER_PATTERN = r"^\d{1,10}:\d{1,10}$"
_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_CONTAINER_ROOTS = frozenset({"/", "/bin", "/dev", "/etc", "/lib", "/proc", "/sys"})

MAX_TIMEOUT_SECONDS = 24 * 60 * 60


def _validate_relative_path(value: str) -> str:
    """A relative POSIX path that cannot escape its base directory."""
    if not value or value.strip() != value:
        raise ValueError("must be a non-empty path without surrounding whitespace")
    if "\\" in value:
        raise ValueError("must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("must be a relative path")
    if ".." in path.parts:
        raise ValueError("must not contain '..'")
    return path.as_posix()


def _validate_container_path(value: str) -> str:
    """An absolute POSIX path inside the sandbox that is safe to own exclusively."""
    path = PurePosixPath(value)
    if not path.is_absolute():
        raise ValueError("must be an absolute POSIX path such as /workspace")
    if ".." in path.parts:
        raise ValueError("must not contain '..'")
    normalised = path.as_posix()
    if normalised in _FORBIDDEN_CONTAINER_ROOTS:
        raise ValueError(f"must not be a system directory ({normalised})")
    return normalised


RelativePath = Annotated[str, AfterValidator(_validate_relative_path)]
ContainerPath = Annotated[str, AfterValidator(_validate_container_path)]
Argv = Annotated[tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)]
TimeoutSeconds = Annotated[int, Field(gt=0, le=MAX_TIMEOUT_SECONDS)]


class _Spec(BaseModel):
    """Common configuration: immutable, and unknown keys are errors (catches typos)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------- source


class LocalSource(_Spec):
    """Use a directory shipped alongside the task file as the workspace."""

    type: Literal["local"]
    path: RelativePath = Field(description="Directory relative to the task file.")


class GitSource(_Spec):
    """Clone a git repository and check out an exact revision."""

    type: Literal["git"]
    repository: str = Field(
        min_length=1,
        description="Clone URL (https://, ssh:// or git@host:path, or file:// for tests).",
    )
    revision: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9._/-]+$",
        description="Commit SHA (recommended), tag or branch. The resolved SHA is recorded.",
    )
    subdirectory: RelativePath | None = Field(
        default=None, description="Use only this subdirectory of the repository."
    )

    @field_validator("revision", mode="before")
    @classmethod
    def _revision_must_be_quoted(cls, value: object) -> object:
        # YAML parses unquoted all-digit revisions (e.g. a short SHA like 1234567) as
        # integers, silently dropping leading zeros. Refuse instead of guessing.
        if isinstance(value, int | float):
            raise ValueError('YAML parsed the revision as a number; quote it: revision: "..."')
        return value

    @property
    def is_pinned(self) -> bool:
        """True when ``revision`` is a full commit SHA and therefore immutable."""
        return bool(_SHA1_PATTERN.match(self.revision) or _SHA256_PATTERN.match(self.revision))


SourceSpec = Annotated[LocalSource | GitSource, Field(discriminator="type")]


# ----------------------------------------------------------------------- environment


class EnvironmentSpec(_Spec):
    """How the sandbox image is obtained and how processes inside it run."""

    dockerfile: RelativePath | None = Field(
        default=None, description="Dockerfile relative to the task file."
    )
    context: RelativePath | None = Field(
        default=None, description="Build context directory; defaults to the Dockerfile's."
    )
    image: str | None = Field(
        default=None,
        min_length=1,
        description="Prebuilt image reference. Pin with @sha256:... for reproducibility.",
    )
    workspace: ContainerPath = Field(
        default="/workspace", description="Where the task source is placed in the sandbox."
    )
    user: str = Field(
        default="1000:1000",
        pattern=_USER_PATTERN,
        description="Numeric uid:gid that owns the workspace and runs every command.",
    )
    variables: dict[Annotated[str, Field(pattern=_ENV_NAME_PATTERN)], str] = Field(
        default_factory=dict,
        description="Non-secret environment variables for every sandbox command.",
    )

    @model_validator(mode="after")
    def _exactly_one_image_source(self) -> EnvironmentSpec:
        if (self.dockerfile is None) == (self.image is None):
            raise ValueError("exactly one of 'dockerfile' or 'image' must be set")
        if self.context is not None and self.dockerfile is None:
            raise ValueError("'context' requires 'dockerfile'")
        return self

    @property
    def is_image_pinned(self) -> bool:
        """True when a prebuilt image is referenced by immutable digest."""
        return self.image is not None and "@sha256:" in self.image


class SetupCommand(_Spec):
    """A command run in the agent sandbox after the workspace is copied in."""

    command: Argv
    timeout_seconds: TimeoutSeconds | None = None


# ------------------------------------------------------------------ limits & network


class ResourceSpec(_Spec):
    """Per-sandbox resource limits. Every field here is enforced by the Docker backend."""

    cpus: float = Field(default=1.0, gt=0, le=256, description="CPU cores (fractional ok).")
    memory_mb: int = Field(
        default=2048, ge=128, le=1024 * 1024, description="Memory limit; swap is disabled."
    )
    pids_limit: int = Field(
        default=512, ge=16, le=1_000_000, description="Maximum processes (fork-bomb guard)."
    )


class TimeoutSpec(_Spec):
    """Time limits for each lifecycle boundary, in seconds."""

    source_seconds: TimeoutSeconds = Field(default=600, description="Git clone/checkout.")
    build_seconds: TimeoutSeconds = 1800
    setup_seconds: TimeoutSeconds = 600
    agent_seconds: TimeoutSeconds = 1800
    command_seconds: TimeoutSeconds = Field(
        default=600, description="Default limit for a single agent command."
    )
    grader_seconds: TimeoutSeconds = 900


class NetworkSpec(_Spec):
    """Network access for task sandboxes. Disabled unless explicitly enabled."""

    enabled: bool = False


# ---------------------------------------------------------------------------- grading


class _GraderBase(_Spec):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    weight: float = Field(default=1.0, gt=0, description="Relative weight for 'weighted'.")


class PytestGraderSpec(_GraderBase):
    """Run pytest and score by the fraction of tests that passed."""

    type: Literal["pytest"]
    paths: tuple[str, ...] = Field(
        default=(), description="Test paths inside the grading sandbox (absolute or relative)."
    )
    args: tuple[str, ...] = Field(default=(), description="Extra pytest arguments.")
    python: str = Field(default="python", min_length=1, description="Interpreter to use.")


class CommandGraderSpec(_GraderBase):
    """Run a command; pass/fail is decided by its exit status."""

    type: Literal["command"]
    command: Argv
    success_exit_codes: tuple[int, ...] = Field(default=(0,), min_length=1)


GraderSpec = Annotated[PytestGraderSpec | CommandGraderSpec, Field(discriminator="type")]


class GradingSpec(_Spec):
    """How the agent's solution is evaluated.

    Grading happens in a *fresh* sandbox: the agent's final workspace is copied in,
    ``protected_paths`` are restored from the pristine source, and ``assets`` (hidden
    tests) are added at ``assets_mount``. The agent never sees the assets.
    """

    graders: Annotated[tuple[GraderSpec, ...], Field(min_length=1)]
    assets: RelativePath | None = Field(
        default=None, description="Hidden grader files, relative to the task file."
    )
    assets_mount: ContainerPath = "/grader"
    protected_paths: tuple[RelativePath, ...] = Field(
        default=(), description="Workspace paths restored to their original state before grading."
    )
    aggregation: Literal["all", "weighted"] = Field(
        default="all",
        description="'all': every grader must pass. 'weighted': weighted score >= threshold.",
    )
    pass_threshold: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def _unique_grader_names(self) -> GradingSpec:
        names = [grader.name or grader.type for grader in self.graders]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"grader names must be unique; duplicated: {', '.join(duplicates)}")
        return self


class ArtifactSpec(_Spec):
    """Extra files to collect from the agent's final workspace."""

    include: tuple[str, ...] = Field(default=(), description="Glob patterns, workspace-relative.")


# ------------------------------------------------------------------------------- task


class TaskSpec(_Spec):
    """A complete, validated task definition (schema version 1)."""

    schema_version: Literal["1"]
    id: str = Field(pattern=_TASK_ID_PATTERN, description="Stable, unique task identifier.")
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    instructions: str = Field(min_length=1, description="The problem statement given to agents.")
    tags: tuple[str, ...] = ()

    source: SourceSpec
    environment: EnvironmentSpec
    setup: tuple[SetupCommand, ...] = ()
    resources: ResourceSpec = ResourceSpec()
    timeouts: TimeoutSpec = TimeoutSpec()
    network: NetworkSpec = NetworkSpec()
    grading: GradingSpec
    artifacts: ArtifactSpec = ArtifactSpec()
