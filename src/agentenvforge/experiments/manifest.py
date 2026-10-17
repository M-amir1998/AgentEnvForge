"""The reproducibility snapshot of an experiment.

Together with each run's own :class:`~agentenvforge.domain.manifest.RunManifest` (image ID,
resolved commit, source tree hash, seed), the experiment manifest answers "exactly what was
compared, with what, where?". It is persisted when the experiment is created and completed
with environment and agent provenance during preparation.

It deliberately contains no host name, user name, home path, MAC address or serial number.
Paths are stored as written in the experiment file (relative), never resolved.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agentenvforge.domain.types import Metadata


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class TaskReference(_Record):
    """A task as it was used by the experiment."""

    task_id: str
    path: str
    """As written in the experiment file (relative to it)."""
    schema_version: str
    definition_hash: str
    image_reference: str | None = None
    """Image prepared for the task before scheduling (every run of the task uses it)."""
    image_id: str | None = None
    image_cached: bool | None = None
    image_prepare_seconds: float | None = None


class AgentReference(_Record):
    """An agent as it was configured for the experiment."""

    agent_id: str
    role: str = "subject"
    adapter: str
    """The adapter's own name (e.g. ``ollama:qwen2.5-coder:7b``)."""
    source: str
    """The spec path as written in the experiment file, or ``inline``."""
    spec: dict[str, object]
    """The full validated agent specification (secret names only, never values)."""
    spec_hash: str
    code_hash: str | None = None
    """Content hash of the agent's code (python adapter directory or command agent files)."""
    agent_timeout_seconds: int | None = None
    provenance: Metadata = {}
    """Returned by the adapter's preflight check (e.g. model digest, server version)."""


class HostEnvironment(_Record):
    """Where the experiment ran. Hardware and software facts only; nothing identifying."""

    os: str
    os_release: str
    architecture: str
    python_version: str
    cpu_model: str | None = None
    logical_cpus: int | None = None
    memory_bytes: int | None = None
    sandbox_backend: str | None = None
    sandbox_backend_version: str | None = None
    sandbox_details: dict[str, str] = {}


class ExperimentManifest(_Record):
    """Everything needed to understand, compare and re-run an experiment."""

    manifest_version: int = 1
    agentenvforge_version: str
    experiment_schema_version: str
    spec_id: str
    name: str | None = None
    description: str = ""
    definition_hash: str
    definition: dict[str, object]
    """The full validated experiment definition."""
    tasks: tuple[TaskReference, ...]
    agents: tuple[AgentReference, ...]
    repetitions: int
    workers: int
    base_seed: int
    matrix_order: str = "interleaved-rounds, agent order shuffled per block (seed = base_seed)"
    planned_runs: int
    environment: HostEnvironment | None = None
    """Captured when the experiment starts preparing."""
