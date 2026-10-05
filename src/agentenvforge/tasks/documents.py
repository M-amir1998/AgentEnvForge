"""Reading specification documents (YAML) and converting errors into validation issues.

Shared by the task loader and the agent-spec loader so both report problems in the same
actionable ``field / reason / received`` format.
"""

from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agentenvforge.domain.failures import TaskValidationError, ValidationIssue

MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_RECEIVED_CHARS = 80

# Tag values of discriminated unions that pydantic inserts into error locations
# (e.g. ``source.git.revision``). They are noise for users, who wrote ``source.revision``.
_UNION_TAGS = frozenset({"local", "git", "pytest", "command", "manual", "noop", "python"})


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last.

    A duplicated ``timeout_seconds`` key that quietly overrides the first would be a
    reproducibility bug that is very hard to spot in review.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                mark = key_node.start_mark
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r} (line {mark.line + 1})", mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Parse ``path`` as a YAML mapping or raise :class:`TaskValidationError`."""
    source = str(path)
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise TaskValidationError(
                source,
                [ValidationIssue(field="<file>", reason=f"larger than {MAX_DOCUMENT_BYTES} bytes")],
            )
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TaskValidationError(
            source, [ValidationIssue(field="<file>", reason="file does not exist")]
        ) from None
    except (OSError, UnicodeDecodeError) as error:
        raise TaskValidationError(
            source, [ValidationIssue(field="<file>", reason=f"cannot read file: {error}")]
        ) from error

    try:
        document = yaml.load(text, Loader=_StrictSafeLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as error:
        raise TaskValidationError(
            source, [ValidationIssue(field="<yaml>", reason=f"invalid YAML: {error}")]
        ) from error

    if not isinstance(document, dict):
        raise TaskValidationError(
            source,
            [
                ValidationIssue(
                    field="<document>",
                    reason="top level must be a mapping",
                    received=type(document).__name__,
                )
            ],
        )
    return {str(key): value for key, value in document.items()}


def _format_location(location: tuple[int | str, ...]) -> str:
    parts: list[str] = []
    for index, element in enumerate(location):
        previous = location[index - 1] if index else None
        is_union_tag = (
            isinstance(element, str)
            and element in _UNION_TAGS
            and (previous is None or isinstance(previous, int) or previous in {"source", "spec"})
        )
        if is_union_tag:
            continue
        if isinstance(element, int):
            parts.append(f"[{element}]")
        else:
            parts.append(f".{element}" if parts else element)
    return "".join(parts) or "<document>"


def _format_received(value: object) -> str:
    text = repr(value)
    if len(text) > _MAX_RECEIVED_CHARS:
        text = text[: _MAX_RECEIVED_CHARS - 3] + "..."
    return text


def issues_from_pydantic(error: ValidationError) -> list[ValidationIssue]:
    """Convert a pydantic error into user-facing issues, one per problem."""
    issues = []
    for detail in error.errors(include_url=False):
        reason = detail["msg"].removeprefix("Value error, ")
        received = None if detail["type"] == "missing" else _format_received(detail["input"])
        location = tuple(detail["loc"])
        if detail["type"] == "extra_forbidden":
            reason = "unknown field (check for typos)"
        if detail["type"] == "union_tag_invalid":
            # Point at the discriminator field itself (e.g. `source.type`), not its parent.
            discriminator = str(detail.get("ctx", {}).get("discriminator", "type")).strip("'")
            location = (*location, discriminator)
            received = _format_received(detail.get("ctx", {}).get("tag"))
        issues.append(
            ValidationIssue(field=_format_location(location), reason=reason, received=received)
        )
    return issues
