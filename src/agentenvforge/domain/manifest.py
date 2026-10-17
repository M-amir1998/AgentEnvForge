"""The reproducibility manifest recorded for every run.

The manifest answers "exactly what ran?": which AgentEnvForge version, which task
definition (by hash), which source commit and tree, which image ID, which limits, which
agent. Comparing two manifests shows whether two runs are comparable.

Sections are filled in as the run progresses; a run that failed early has a partial
manifest, which is still useful for diagnosing the failure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SourceProvenance(BaseModel):
    """Where the workspace came from."""

    model_config = ConfigDict(frozen=True)

    source_type: str
    tree_hash: str
    repository: str | None = None
    requested_revision: str | None = None
    resolved_commit: str | None = None


class ImageProvenance(BaseModel):
    """Which sandbox image was used."""

    model_config = ConfigDict(frozen=True)

    reference: str
    image_id: str
    cached: bool
    build_context_hash: str | None = None
    repo_digests: tuple[str, ...] = ()


class RunManifest(BaseModel):
    """Everything needed to reproduce (or audit) a run."""

    model_config = ConfigDict(frozen=True)

    manifest_version: int = 1
    agentenvforge_version: str
    python_version: str
    platform: str
    task_id: str
    task_path: str
    task_schema_version: str
    task_definition_hash: str
    task_definition: dict[str, object]
    """The full validated task, so the run is interpretable even if the file changes."""
    agent: str
    backend: str | None = None
    backend_version: str | None = None
    source: SourceProvenance | None = None
    image: ImageProvenance | None = None
    grader_assets_hash: str | None = None
    overrides: dict[str, int | float | str | bool] = {}
    """Run-time deviations from the task definition (e.g. a longer agent time budget)."""
    seed: int | None = None
    """Seed offered to the agent (``None``: the agent used its own configuration)."""
    experiment: ExperimentLink | None = None
    """Set when the run belongs to an experiment; makes the run self-describing on its own."""
    agent_spec: dict[str, object] | None = None
    """The validated agent specification, when known (secret *names* only, never values)."""


class ExperimentLink(BaseModel):
    """Where a run sits in an experiment's matrix."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str
    position: int
    agent_id: str
    repetition: int
