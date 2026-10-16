"""Agent specification files and adapter construction.

Agents are described separately from tasks so the same task can evaluate many agents::

    # agents/reference.yaml
    schema_version: "1"
    type: command                 # command | manual | noop | python
    name: reference-solver
    command: ["python", "/agent/solve.py"]
    files: reference              # copied to /agent in the sandbox
    secrets: [OPENAI_API_KEY]     # host variables passed through, redacted from records

    # A host-side adapter (e.g. an LLM tool-use loop) from any importable module:
    type: python
    name: my-llm-agent
    adapter: my_package.agents:build   # callable(**options) -> AgentAdapter
    options: {model: some-model}
"""

from __future__ import annotations

import importlib
import shlex
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agentenvforge.agents.base import AgentAdapter
from agentenvforge.agents.builtin import CommandAgent, ManualAgent, NoopAgent
from agentenvforge.domain.failures import TaskValidationError, ValidationIssue
from agentenvforge.domain.types import JSONScalar
from agentenvforge.tasks.documents import issues_from_pydantic, read_yaml_mapping
from agentenvforge.tasks.schema import Argv, RelativePath

BUILTIN_AGENTS = ("manual", "noop")
_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
_LABEL = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class _AgentSpecBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    name: str = Field(pattern=_LABEL)


class CommandAgentSpec(_AgentSpecBase):
    """An external program run inside the agent sandbox."""

    type: Literal["command"]
    command: Argv
    files: RelativePath | None = None
    environment: dict[Annotated[str, Field(pattern=_ENV_NAME)], str] = Field(default_factory=dict)
    secrets: tuple[Annotated[str, Field(pattern=_ENV_NAME)], ...] = ()


class PythonAgentSpec(_AgentSpecBase):
    """A host-side adapter loaded from ``module:callable``.

    ``path`` (relative to the spec file) is searched for the module first, so an agent
    can live next to its spec without being installed. Loading an adapter executes its
    code on the host: only run agent specs you trust (task files can never do this).
    """

    type: Literal["python"]
    adapter: str = Field(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
    path: RelativePath | None = None
    options: dict[str, JSONScalar] = Field(default_factory=dict)


class SimpleAgentSpec(_AgentSpecBase):
    """A built-in adapter without options."""

    type: Literal["manual", "noop"]


AgentSpec = Annotated[
    CommandAgentSpec | PythonAgentSpec | SimpleAgentSpec, Field(discriminator="type")
]


type AgentSpecModel = CommandAgentSpec | PythonAgentSpec | SimpleAgentSpec
"""Any validated agent specification."""

_AGENT_SPEC_ADAPTER: TypeAdapter[CommandAgentSpec | PythonAgentSpec | SimpleAgentSpec] = (
    TypeAdapter(AgentSpec)
)


@dataclass(frozen=True, slots=True)
class AgentEnvironment:
    """Process-edge inputs needed to construct adapters (kept explicit for testability)."""

    environ: Mapping[str, str]
    write: Callable[[str], None]
    prompt: Callable[[str], str]


def resolve_agent(reference: str, environment: AgentEnvironment) -> AgentAdapter:
    """Build an adapter from a built-in name (``manual``, ``noop``) or a spec file path."""
    if reference in BUILTIN_AGENTS:
        return build_agent(
            SimpleAgentSpec.model_validate({"type": reference, "name": reference}),
            base_dir=Path.cwd(),
            environment=environment,
            source=reference,
        )
    path = Path(reference).resolve()
    spec = parse_agent_spec(read_yaml_mapping(path), source=str(path))
    return build_agent(spec, base_dir=path.parent, environment=environment, source=str(path))


def parse_agent_spec(document: Mapping[str, object], *, source: str) -> AgentSpecModel:
    """Validate an agent specification document (from a file or inline in an experiment)."""
    try:
        return _AGENT_SPEC_ADAPTER.validate_python(dict(document))
    except ValidationError as error:
        raise TaskValidationError(source, issues_from_pydantic(error)) from error


def adapter_secrets(adapter: AgentAdapter) -> tuple[str, ...]:
    """Secret values an adapter carries, which every record of its runs must redact."""
    if isinstance(adapter, CommandAgent):
        return tuple(adapter.secrets.values())
    return ()


def command_agent_from_string(command: str, *, label: str = "inline") -> CommandAgent:
    """Build a :class:`CommandAgent` from a shell-like string (split with ``shlex``)."""
    argv = tuple(shlex.split(command))
    if not argv:
        raise TaskValidationError(
            "--agent-command", [ValidationIssue(field="command", reason="must not be empty")]
        )
    return CommandAgent(label=label, command=argv)


def build_agent(
    spec: AgentSpecModel,
    *,
    base_dir: Path,
    environment: AgentEnvironment,
    source: str,
) -> AgentAdapter:
    """Construct the adapter described by ``spec``; relative paths resolve against ``base_dir``."""
    if isinstance(spec, CommandAgentSpec):
        return _build_command_agent(spec, base_dir, environment, source)
    if isinstance(spec, PythonAgentSpec):
        return _build_python_agent(spec, base_dir, source)
    if spec.type == "manual":
        return ManualAgent(write=environment.write, wait_for_confirmation=environment.prompt)
    return NoopAgent()


def _build_command_agent(
    spec: CommandAgentSpec, base_dir: Path, environment: AgentEnvironment, source: str
) -> CommandAgent:
    issues: list[ValidationIssue] = []
    files: Path | None = None
    if spec.files is not None:
        files = (base_dir / spec.files).resolve()
        if not files.is_relative_to(base_dir.resolve()):
            issues.append(
                ValidationIssue(
                    field="files", reason="must stay inside the spec directory", received=spec.files
                )
            )
        elif not files.is_dir():
            issues.append(
                ValidationIssue(
                    field="files", reason="directory does not exist", received=spec.files
                )
            )
    secrets: dict[str, str] = {}
    for name in spec.secrets:
        value = environment.environ.get(name)
        if not value:
            issues.append(
                ValidationIssue(
                    field="secrets", reason="environment variable is not set", received=name
                )
            )
        else:
            secrets[name] = value
    if issues:
        raise TaskValidationError(source, issues)
    return CommandAgent(
        label=spec.name,
        command=spec.command,
        files=files,
        environment=dict(spec.environment),
        secrets=secrets,
    )


def _invalid(source: str, field: str, reason: str, received: str) -> TaskValidationError:
    return TaskValidationError(
        source, [ValidationIssue(field=field, reason=reason, received=received)]
    )


def _build_python_agent(spec: PythonAgentSpec, base_dir: Path, source: str) -> AgentAdapter:
    module_name, attribute = spec.adapter.split(":", maxsplit=1)
    search_dir: Path | None = None
    if spec.path is not None:
        search_dir = (base_dir / spec.path).resolve()
        if not search_dir.is_relative_to(base_dir.resolve()) or not search_dir.is_dir():
            raise _invalid(
                source, "path", "must be an existing directory inside the spec directory", spec.path
            )
        if str(search_dir) not in sys.path:
            sys.path.insert(0, str(search_dir))
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise _invalid(source, "adapter", f"cannot import module: {error}", spec.adapter) from error
    if search_dir is not None:
        _ensure_module_from(module, search_dir, source, spec.adapter)
    try:
        factory = getattr(module, attribute)
        adapter = factory(**spec.options)
    # Any failure of third-party construction code is a configuration problem of this spec;
    # it must be reported as such, with the cause chained, not escape unclassified.
    except Exception as error:  # re-raised as a classified validation error
        raise _invalid(
            source,
            "adapter",
            f"cannot construct adapter: {type(error).__name__}: {error}",
            spec.adapter,
        ) from error
    if not isinstance(adapter, AgentAdapter):
        raise _invalid(
            source,
            "adapter",
            "factory did not return an AgentAdapter "
            "(needs a `name` property and a `run(context)` method)",
            spec.adapter,
        )
    return adapter


def _ensure_module_from(module: ModuleType, search_dir: Path, source: str, adapter: str) -> None:
    """Reject a module that was imported earlier from somewhere else.

    Python caches modules by name: two agent directories that both contain ``agent.py``
    would otherwise silently share whichever was imported first.
    """
    origin = getattr(module, "__file__", None)
    if origin is None or not Path(origin).resolve().is_relative_to(search_dir):
        raise _invalid(
            source,
            "adapter",
            f"module name {module.__name__!r} is already loaded from {origin}; give adapter "
            "modules unique names",
            adapter,
        )
