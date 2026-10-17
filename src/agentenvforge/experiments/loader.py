"""Loading and validating experiment files.

Everything that can be checked without Docker or a model server is checked here, before an
experiment is created:

1. the document and schema (as for tasks: duplicate keys, unknown fields, version first);
2. every task file loads and validates (issues are reported under ``tasks[i]``), and no two
   tasks share an ID;
3. every agent spec parses and its adapter can be constructed; interactive agents (``manual``)
   are rejected because nobody can attend hundreds of runs.

Agent *availability* (is the model installed? is the server up?) is checked by the optional
adapter preflight hook, at ``experiment validate --preflight`` and when an experiment starts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agentenvforge.agents.base import AgentAdapter
from agentenvforge.agents.spec import (
    AgentEnvironment,
    AgentSpecModel,
    CommandAgentSpec,
    PythonAgentSpec,
    SimpleAgentSpec,
    build_agent,
    parse_agent_spec,
)
from agentenvforge.domain.failures import TaskValidationError, ValidationIssue
from agentenvforge.experiments.schema import (
    SUPPORTED_SCHEMA_VERSIONS,
    ExperimentAgent,
    ExperimentSpec,
)
from agentenvforge.tasks.documents import issues_from_pydantic, read_yaml_mapping
from agentenvforge.tasks.loader import LoadedTask, find_duplicate_ids, load_task
from agentenvforge.tasks.schema import LocalSource
from agentenvforge.workspace.fingerprint import hash_directory

_CONCURRENCY_WARNING_WORKERS = 8


@dataclass(frozen=True, slots=True)
class ResolvedAgent:
    """An experiment agent whose specification has been validated."""

    agent_id: str
    spec: AgentSpecModel
    base_dir: Path
    """Directory that relative paths in the spec (``files``, ``path``) resolve against."""
    source: str
    """Spec file path, or ``<experiment>#agents[i]`` for inline specs (used in messages)."""
    timeout_override: int | None
    environment: AgentEnvironment
    role: str = "subject"
    declared_as: str = "inline"
    """The spec path as written in the experiment file, or ``inline``."""

    def build(self) -> AgentAdapter:
        """Construct a fresh adapter. Experiments build one per run, so adapters need not be
        thread-safe and cannot leak state between repetitions."""
        return build_agent(
            self.spec, base_dir=self.base_dir, environment=self.environment, source=self.source
        )

    @property
    def canonical_spec(self) -> dict[str, Any]:
        """The validated spec as plain JSON data (recorded in the experiment manifest)."""
        return self.spec.model_dump(mode="json")

    @property
    def spec_hash(self) -> str:
        """SHA-256 of the canonical spec."""
        return _sha256_json(self.canonical_spec)

    @property
    def is_host_side(self) -> bool:
        """True for python adapters, whose agent loop runs in this process (on this CPU)."""
        return isinstance(self.spec, PythonAgentSpec)

    @property
    def code_hash(self) -> str | None:
        """Content hash of the agent's code, so a changed agent changes the experiment hash.

        Python adapters: the spec's ``path`` directory, or the imported module's file.
        Command agents: the ``files`` directory copied into the sandbox. Built-ins: none.
        """
        spec = self.spec
        if isinstance(spec, CommandAgentSpec):
            return hash_directory((self.base_dir / spec.files).resolve()) if spec.files else None
        if isinstance(spec, PythonAgentSpec):
            if spec.path is not None:
                return hash_directory((self.base_dir / spec.path).resolve())
            module = importlib.util.find_spec(spec.adapter.split(":", 1)[0])
            if module is not None and module.origin and Path(module.origin).is_file():
                return hashlib.sha256(Path(module.origin).read_bytes()).hexdigest()
        return None


@dataclass(frozen=True, slots=True)
class LoadedExperiment:
    """A validated experiment together with everything it references."""

    spec: ExperimentSpec
    path: Path
    root: Path
    tasks: tuple[LoadedTask, ...]
    agents: tuple[ResolvedAgent, ...]
    definition_hash: str
    """SHA-256 over the experiment definition and every referenced task and agent definition."""
    warnings: tuple[str, ...] = ()

    @property
    def task_ids(self) -> tuple[str, ...]:
        """Task IDs in experiment order."""
        return tuple(task.spec.id for task in self.tasks)

    @property
    def agent_ids(self) -> tuple[str, ...]:
        """Agent IDs in experiment order."""
        return tuple(agent.agent_id for agent in self.agents)

    def task(self, task_id: str) -> LoadedTask:
        """The task with ``task_id``."""
        return next(task for task in self.tasks if task.spec.id == task_id)

    def agent(self, agent_id: str) -> ResolvedAgent:
        """The agent with ``agent_id``."""
        return next(agent for agent in self.agents if agent.agent_id == agent_id)


def load_experiment(
    path: Path,
    environment: AgentEnvironment,
    *,
    cpu_count: Callable[[], int | None] = os.cpu_count,
) -> LoadedExperiment:
    """Load, validate and resolve the experiment at ``path``."""
    experiment_path = path.resolve()
    source = str(experiment_path)
    spec = _parse_spec(read_yaml_mapping(experiment_path), source=source)
    root = experiment_path.parent

    tasks, issues = _load_tasks(spec, root)
    for task_id, paths in find_duplicate_ids(tasks).items():
        issues.append(
            ValidationIssue(
                field="tasks",
                reason="two task files define the same task id",
                received=f"{task_id}: " + ", ".join(path.name for path in paths),
            )
        )
    agents = _resolve_agents(spec, root, environment, source, issues)
    if issues:
        raise TaskValidationError(source, issues)

    warnings = [f"{task.spec.id}: {warning}" for task in tasks for warning in task.warnings]
    warnings.extend(concurrency_warnings(agents, spec.execution.workers, cpu_count=cpu_count))
    return LoadedExperiment(
        spec=spec,
        path=experiment_path,
        root=root,
        tasks=tuple(tasks),
        agents=tuple(agents),
        definition_hash=_definition_hash(spec, tasks, agents),
        warnings=tuple(warnings),
    )


def concurrency_warnings(
    agents: Sequence[ResolvedAgent],
    workers: int,
    *,
    cpu_count: Callable[[], int | None] = os.cpu_count,
) -> list[str]:
    """Warnings about running ``workers`` runs at once with these agents on this machine."""
    warnings = []
    cpus = cpu_count() or 1
    if workers > min(_CONCURRENCY_WARNING_WORKERS, max(1, cpus // 2)):
        warnings.append(
            f"{workers} workers on a machine with {cpus} logical CPUs: every worker runs up to "
            "two containers at once; high concurrency can overload Docker and skew timings"
        )
    host_side = [agent.agent_id for agent in agents if agent.is_host_side]
    if workers > 1 and host_side:
        warnings.append(
            f"workers={workers} with host-side agents ({', '.join(host_side)}): their agent loops "
            "and model calls compete for this machine's CPU, which changes their timings and can "
            "change their results; compare such agents at equal workers, ideally 1"
        )
    return warnings


def _parse_spec(document: dict[str, object], *, source: str) -> ExperimentSpec:
    version = document.get("schema_version")
    if version is None:
        raise TaskValidationError(
            source, [ValidationIssue(field="schema_version", reason="required field is missing")]
        )
    if str(version) not in SUPPORTED_SCHEMA_VERSIONS:
        raise TaskValidationError(
            source,
            [
                ValidationIssue(
                    field="schema_version",
                    reason="unsupported schema version; supported: "
                    + ", ".join(SUPPORTED_SCHEMA_VERSIONS),
                    received=repr(version),
                )
            ],
        )
    try:
        return ExperimentSpec.model_validate({**document, "schema_version": str(version)})
    except ValidationError as error:
        raise TaskValidationError(source, issues_from_pydantic(error)) from error


def _load_tasks(spec: ExperimentSpec, root: Path) -> tuple[list[LoadedTask], list[ValidationIssue]]:
    tasks: list[LoadedTask] = []
    issues: list[ValidationIssue] = []
    for index, relative in enumerate(spec.tasks):
        try:
            tasks.append(load_task(root / relative))
        except TaskValidationError as error:
            issues.extend(
                issue.model_copy(update={"field": f"tasks[{index}] {issue.field}"})
                for issue in error.issues
            )
    return tasks, issues


def _resolve_agents(
    spec: ExperimentSpec,
    root: Path,
    environment: AgentEnvironment,
    source: str,
    issues: list[ValidationIssue],
) -> list[ResolvedAgent]:
    agents: list[ResolvedAgent] = []
    for index, entry in enumerate(spec.agents):
        try:
            resolved = _resolve_agent(index, entry, root, environment, source)
            resolved.build()  # constructing once surfaces import and configuration errors now
        except TaskValidationError as error:
            issues.extend(
                issue.model_copy(update={"field": f"agents[{index}] {issue.field}"})
                for issue in error.issues
            )
            continue
        if isinstance(resolved.spec, SimpleAgentSpec) and resolved.spec.type == "manual":
            issues.append(
                ValidationIssue(
                    field=f"agents[{index}]",
                    reason="interactive (manual) agents cannot run unattended in an experiment",
                    received=entry.id,
                )
            )
            continue
        agents.append(resolved)
    return agents


def _resolve_agent(
    index: int, entry: ExperimentAgent, root: Path, environment: AgentEnvironment, source: str
) -> ResolvedAgent:
    if entry.spec is not None:
        spec_path = (root / entry.spec).resolve()
        document = read_yaml_mapping(spec_path)
        return ResolvedAgent(
            agent_id=entry.id,
            spec=_with_options(parse_agent_spec(document, source=str(spec_path)), entry, source),
            base_dir=spec_path.parent,
            source=str(spec_path),
            timeout_override=entry.agent_timeout_seconds,
            environment=environment,
            role=entry.role,
            declared_as=entry.spec,
        )
    inline = {"name": entry.id, **(entry.agent or {})}
    inline_source = f"{source}#agents[{index}]"
    return ResolvedAgent(
        agent_id=entry.id,
        spec=_with_options(parse_agent_spec(inline, source=inline_source), entry, inline_source),
        base_dir=root,
        source=inline_source,
        timeout_override=entry.agent_timeout_seconds,
        environment=environment,
        role=entry.role,
    )


def _with_options(spec: AgentSpecModel, entry: ExperimentAgent, source: str) -> AgentSpecModel:
    """Apply the experiment's option overrides (python agents only)."""
    if not entry.options:
        return spec
    if not isinstance(spec, PythonAgentSpec):
        raise TaskValidationError(
            source,
            [
                ValidationIssue(
                    field="options",
                    reason="only python agents take options; put command settings in the spec",
                    received=spec.type,
                )
            ],
        )
    return spec.model_copy(update={"options": {**spec.options, **entry.options}})


def _sha256_json(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _task_content(task: LoadedTask) -> dict[str, str | None]:
    """A task's identity: its definition *and* the files it ships (the local source tree,
    the image build context, the hidden grader assets), so editing a hidden test or the
    buggy project changes the experiment's hash. A git source is identified by the
    repository and revision in its definition (each run records the resolved commit)."""
    spec = task.spec
    context = task.build_context()
    return {
        "id": spec.id,
        "definition_hash": task.definition_hash,
        "source_tree": (
            hash_directory(task.host_path(spec.source.path))
            if isinstance(spec.source, LocalSource)
            else None
        ),
        "build_context": hash_directory(context) if context is not None else None,
        "grader_assets": (
            hash_directory(task.host_path(spec.grading.assets)) if spec.grading.assets else None
        ),
    }


def _definition_hash(
    spec: ExperimentSpec, tasks: list[LoadedTask], agents: list[ResolvedAgent]
) -> str:
    """Identity of the *evaluation*: what is compared, with what, how often.

    Excludes presentation and execution choices (name, description, workers, output), so
    re-running the same evaluation with more workers keeps the same hash, while changing a
    task, an agent's spec or code, the repetitions or the seed changes it.
    """
    return _sha256_json(
        {
            "schema_version": spec.schema_version,
            "repetitions": spec.repetitions,
            "base_seed": spec.execution.base_seed,
            "tasks": [_task_content(task) for task in tasks],
            "agents": [
                {
                    "id": a.agent_id,
                    "role": a.role,
                    "spec_hash": a.spec_hash,
                    "code_hash": a.code_hash,
                    "agent_timeout_seconds": a.timeout_override,
                }
                for a in agents
            ],
        }
    )
