"""Host-side handling of the agent's solution between the agent and grading stages."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from agentenvforge.workspace.fingerprint import iter_tree_files

MAX_INCLUDED_FILE_BYTES = 10 * 1024 * 1024
MAX_INCLUDED_TOTAL_BYTES = 100 * 1024 * 1024


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def build_grading_tree(
    final: Path, baseline: Path, destination: Path, protected_paths: tuple[str, ...]
) -> list[str]:
    """Create the workspace that is graded: the agent's result with protected paths reset.

    Every protected path is replaced by its baseline version (or removed if it did not
    exist originally). This is the first line of defence against the most common form of
    reward hacking: editing the tests instead of fixing the code.

    Returns the protected paths that differed and were restored.
    """
    shutil.copytree(final, destination, symlinks=True)
    restored: list[str] = []
    root = destination.resolve()
    for relative in protected_paths:
        target, original = destination / relative, baseline / relative
        # Guard against an agent-created symlink redirecting the restore outside the tree.
        if not target.parent.resolve().is_relative_to(root):
            continue
        if _same_content(target, original):
            continue
        _remove(target)
        if original.is_dir():
            shutil.copytree(original, target, symlinks=True)
        elif original.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
        restored.append(relative)
    return restored


def _same_content(left: Path, right: Path) -> bool:
    if left.is_dir() and right.is_dir():
        left_files, right_files = list(iter_tree_files(left)), list(iter_tree_files(right))
        return left_files == right_files and all(
            (left / name).read_bytes() == (right / name).read_bytes() for name in left_files
        )
    if left.is_file() and right.is_file() and not (left.is_symlink() or right.is_symlink()):
        return left.read_bytes() == right.read_bytes()
    return not left.exists() and not right.exists() and not left.is_symlink()


@dataclass(frozen=True, slots=True)
class IncludedFile:
    """A workspace file selected by the task's ``artifacts.include`` patterns."""

    name: str
    content: bytes


def collect_included_files(
    root: Path, patterns: tuple[str, ...]
) -> tuple[list[IncludedFile], list[str]]:
    """Return files matching ``patterns`` within size limits, and names skipped for size."""
    selected: list[IncludedFile] = []
    skipped: list[str] = []
    total = 0
    matches = sorted({path for pattern in patterns for path in root.glob(pattern)})
    for path in matches:
        if path.is_symlink() or not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if size > MAX_INCLUDED_FILE_BYTES or total + size > MAX_INCLUDED_TOTAL_BYTES:
            skipped.append(name)
            continue
        total += size
        selected.append(IncludedFile(name=name, content=path.read_bytes()))
    return selected, skipped
