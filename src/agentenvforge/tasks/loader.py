"""Loading and validating task files.

Validation happens in three passes, all before any expensive work:

1. **document** - the file is readable YAML, a mapping, without duplicate keys;
2. **schema** - the ``schema_version`` is supported and the content matches the model;
3. **filesystem** - every referenced host path exists, stays inside the task directory,
   and hidden grader assets are not inside the agent-visible source tree.

Any problem raises :class:`~agentenvforge.domain.failures.TaskValidationError` listing
*all* issues found in that pass, so authors can fix them in one iteration.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from agentenvforge.domain.failures import TaskValidationError, ValidationIssue
from agentenvforge.tasks.documents import issues_from_pydantic, read_yaml_mapping
from agentenvforge.tasks.schema import SUPPORTED_SCHEMA_VERSIONS, LocalSource, TaskSpec

TASK_FILE_NAMES = ("task.yaml", "task.yml")


@dataclass(frozen=True, slots=True)
class LoadedTask:
    """A validated task together with where it came from."""

    spec: TaskSpec
    path: Path
    """Absolute path of the task file."""
    root: Path
    """Directory containing the task file; all host paths are relative to it."""
    definition_hash: str
    """SHA-256 of the canonical JSON form of :attr:`spec` (formatting-insensitive)."""
    warnings: tuple[str, ...] = ()

    def host_path(self, relative: str) -> Path:
        """Resolve a validated host path from the specification."""
        return (self.root / relative).resolve()

    def build_context(self) -> Path | None:
        """The Dockerfile's build context directory (``None`` for a prebuilt image)."""
        environment = self.spec.environment
        if environment.dockerfile is None:
            return None
        if environment.context:
            return self.host_path(environment.context)
        return self.host_path(environment.dockerfile).parent


def load_task(path: Path) -> LoadedTask:
    """Load, validate and return the task at ``path``."""
    task_path = path.resolve()
    document = read_yaml_mapping(task_path)
    spec = _parse_spec(document, source=str(task_path))
    root = task_path.parent
    issues = list(_filesystem_issues(spec, root))
    if issues:
        raise TaskValidationError(str(task_path), issues)
    return LoadedTask(
        spec=spec,
        path=task_path,
        root=root,
        definition_hash=definition_hash(spec),
        warnings=tuple(_reproducibility_warnings(spec)),
    )


def definition_hash(spec: TaskSpec) -> str:
    """Return a stable hash of the task definition, independent of YAML formatting."""
    canonical = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def discover_task_files(path: Path) -> list[Path]:
    """Return task files at ``path``: the file itself, or every task file below a directory."""
    if path.is_file():
        return [path]
    found: list[Path] = []
    for name in TASK_FILE_NAMES:
        found.extend(path.rglob(name))
    return sorted(found)


def find_duplicate_ids(tasks: Iterable[LoadedTask]) -> dict[str, list[Path]]:
    """Return task IDs that are defined by more than one file."""
    by_id: dict[str, list[Path]] = defaultdict(list)
    for task in tasks:
        by_id[task.spec.id].append(task.path)
    return {task_id: paths for task_id, paths in by_id.items() if len(paths) > 1}


def _parse_spec(document: dict[str, object], *, source: str) -> TaskSpec:
    # Check the version first: if it is unsupported, field errors from the v1 model would
    # be misleading noise.
    version = document.get("schema_version")
    if version is None:
        raise TaskValidationError(
            source,
            [ValidationIssue(field="schema_version", reason="required field is missing")],
        )
    if str(version) not in SUPPORTED_SCHEMA_VERSIONS:
        raise TaskValidationError(
            source,
            [
                ValidationIssue(
                    field="schema_version",
                    reason=f"unsupported schema version; supported: "
                    f"{', '.join(SUPPORTED_SCHEMA_VERSIONS)}",
                    received=repr(version),
                )
            ],
        )
    # YAML authors often write `schema_version: 1`; accept it as the string "1".
    document = {**document, "schema_version": str(version)}
    try:
        return TaskSpec.model_validate(document)
    except ValidationError as error:
        raise TaskValidationError(source, issues_from_pydantic(error)) from error


def _host_path_issue(
    root: Path, relative: str, field: str, *, expect_directory: bool
) -> ValidationIssue | None:
    resolved = (root / relative).resolve()
    # Resolving follows symlinks, so this also rejects a symlink pointing outside the task.
    if not resolved.is_relative_to(root.resolve()):
        return ValidationIssue(
            field=field, reason="must stay inside the task directory", received=relative
        )
    if expect_directory and not resolved.is_dir():
        return ValidationIssue(field=field, reason="directory does not exist", received=relative)
    if not expect_directory and not resolved.is_file():
        return ValidationIssue(field=field, reason="file does not exist", received=relative)
    return None


def _filesystem_issues(spec: TaskSpec, root: Path) -> Iterable[ValidationIssue]:
    checks: list[tuple[str, str, bool]] = []
    if isinstance(spec.source, LocalSource):
        checks.append((spec.source.path, "source.path", True))
    environment = spec.environment
    if environment.dockerfile is not None:
        checks.append((environment.dockerfile, "environment.dockerfile", False))
    if environment.context is not None:
        checks.append((environment.context, "environment.context", True))
    if spec.grading.assets is not None:
        checks.append((spec.grading.assets, "grading.assets", True))

    for relative, field, expect_directory in checks:
        issue = _host_path_issue(root, relative, field, expect_directory=expect_directory)
        if issue is not None:
            yield issue

    for index, pattern in enumerate(spec.artifacts.include):
        if pattern.startswith("/") or ".." in pattern.split("/"):
            yield ValidationIssue(
                field=f"artifacts.include[{index}]",
                reason="must be a workspace-relative glob without '..'",
                received=pattern,
            )

    if environment.dockerfile is not None and environment.context is not None:
        dockerfile = (root / environment.dockerfile).resolve()
        if not dockerfile.is_relative_to((root / environment.context).resolve()):
            yield ValidationIssue(
                field="environment.dockerfile",
                reason="must be inside environment.context",
                received=environment.dockerfile,
            )

    if isinstance(spec.source, LocalSource) and spec.grading.assets is not None:
        source_dir = (root / spec.source.path).resolve()
        assets_dir = (root / spec.grading.assets).resolve()
        if assets_dir.is_relative_to(source_dir):
            yield ValidationIssue(
                field="grading.assets",
                reason="must not be inside source.path, or hidden grader files "
                "would be visible to the agent",
                received=spec.grading.assets,
            )


def _reproducibility_warnings(spec: TaskSpec) -> Iterable[str]:
    if not isinstance(spec.source, LocalSource) and not spec.source.is_pinned:
        yield (
            f"source.revision {spec.source.revision!r} is not a full commit SHA; the "
            "resolved commit is recorded, but re-runs may check out different code"
        )
    if spec.environment.image is not None and not spec.environment.is_image_pinned:
        yield (
            f"environment.image {spec.environment.image!r} is not pinned by digest "
            "(@sha256:...); the image may change between runs"
        )
    if spec.network.enabled:
        yield "network access is enabled for task sandboxes"
