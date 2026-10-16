"""Experiment specification, schema version 1.

An experiment file names *which* tasks and agents to combine and how many times; everything
about *how* a single run executes stays in the task and agent specifications it references::

    schema_version: "1"
    id: local-coder-comparison
    tasks:                                   # task files, relative to this file
      - ../examples/off-by-one/task.yaml
    agents:
      - id: qwen-7b
        spec: ../examples/agents/ollama/qwen2.5-coder-7b.yaml   # an agent spec file ...
        agent_timeout_seconds: 600                              # optional budget override
        options: {temperature: 0.2}                             # optional option overrides
      - id: noop
        agent: {type: noop}                                     # ... or an inline spec
    repetitions: 3
    execution:
      workers: 1
      base_seed: 0
    output:
      directory: results

The same compatibility policy as task files applies (ADR-0002): optional fields may be
added within version 1; anything else requires version 2.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from agentenvforge.domain.types import JSONScalar
from agentenvforge.tasks.schema import MAX_TIMEOUT_SECONDS

SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = ("1",)

MAX_WORKERS = 64
"""Sanity bound on concurrency. Each worker runs one task at a time, which means up to two
containers (agent and grader) plus any model calls; far beyond what a laptop sustains."""
MAX_PLANNED_RUNS = 100_000
"""Sanity bound on matrix size: every planned run is persisted when the experiment starts."""

_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"
_AGENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

type AgentRole = Literal["subject", "baseline", "reference"]


def _validate_portable_path(value: str) -> str:
    """A relative POSIX path; ``..`` is allowed (experiments often live beside the tasks)."""
    if not value or value.strip() != value:
        raise ValueError("must be a non-empty path without surrounding whitespace")
    if "\\" in value:
        raise ValueError("must use forward slashes")
    if PurePosixPath(value).is_absolute() or (len(value) > 1 and value[1] == ":"):
        raise ValueError(
            "must be relative to the experiment file (absolute paths are not portable)"
        )
    return PurePosixPath(value).as_posix()


PortablePath = Annotated[str, AfterValidator(_validate_portable_path)]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExperimentAgent(_Spec):
    """One agent under evaluation: an agent spec file, or the same content inline."""

    id: str = Field(
        pattern=_AGENT_ID_PATTERN, description="Agent identifier within the experiment."
    )
    spec: PortablePath | None = Field(default=None, description="Agent spec file path.")
    agent: dict[str, Any] | None = Field(
        default=None, description="Inline agent spec (same fields as an agent spec file)."
    )
    agent_timeout_seconds: int | None = Field(
        default=None,
        gt=0,
        le=MAX_TIMEOUT_SECONDS,
        description="Overrides each task's timeouts.agent_seconds for this agent (recorded).",
    )
    options: dict[str, JSONScalar] | None = Field(
        default=None,
        description="Python agents only: overrides individual entries of the spec's options "
        "(e.g. temperature), so one agent spec serves several configurations. The merged "
        "spec is what is hashed and recorded.",
    )
    role: AgentRole = Field(
        default="subject",
        description="'subject': an agent under evaluation. 'baseline' (e.g. noop, expected to "
        "fail) and 'reference' (e.g. an oracle, expected to pass) validate the tasks and are "
        "kept out of every comparison between subjects.",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ExperimentAgent:
        if (self.spec is None) == (self.agent is None):
            raise ValueError("exactly one of 'spec' or 'agent' must be set")
        return self


class ExecutionSpec(_Spec):
    """How the matrix is executed."""

    workers: int = Field(
        default=1, ge=1, le=MAX_WORKERS, description="Maximum number of runs executing at once."
    )
    base_seed: int = Field(
        default=0,
        ge=0,
        le=2**31 - 1,
        description="Repetition r receives seed base_seed + r - 1 (for agents that support seeds).",
    )


class OutputSpec(_Spec):
    """Where result files are written (not part of the experiment's identity)."""

    directory: PortablePath | None = Field(
        default=None,
        description="Relative to the experiment file. A later execution replaces the results "
        "in it only with --overwrite. Default: <AGENTENV_HOME>/experiments/<experiment id>/.",
    )


class ExperimentSpec(_Spec):
    """A complete, validated experiment definition (schema version 1)."""

    schema_version: Literal["1"]
    id: str = Field(
        pattern=_ID_PATTERN, description="Stable identifier of this experiment definition."
    )
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str = ""
    tags: tuple[str, ...] = ()
    tasks: Annotated[tuple[PortablePath, ...], Field(min_length=1)]
    agents: Annotated[tuple[ExperimentAgent, ...], Field(min_length=1)]
    repetitions: int = Field(ge=1, le=10_000)
    execution: ExecutionSpec = ExecutionSpec()
    output: OutputSpec = OutputSpec()

    @model_validator(mode="after")
    def _unique_and_bounded(self) -> ExperimentSpec:
        agent_ids = [agent.id for agent in self.agents]
        duplicated = sorted({i for i in agent_ids if agent_ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"agent ids must be unique; duplicated: {', '.join(duplicated)}")
        duplicated_tasks = sorted({t for t in self.tasks if self.tasks.count(t) > 1})
        if duplicated_tasks:
            raise ValueError(
                f"task paths must be unique; duplicated: {', '.join(duplicated_tasks)}"
            )
        planned = len(self.tasks) * len(self.agents) * self.repetitions
        if planned > MAX_PLANNED_RUNS:
            raise ValueError(f"{planned} planned runs exceed the limit of {MAX_PLANNED_RUNS}")
        return self

    @property
    def planned_runs(self) -> int:
        """Number of runs in the matrix."""
        return len(self.tasks) * len(self.agents) * self.repetitions
