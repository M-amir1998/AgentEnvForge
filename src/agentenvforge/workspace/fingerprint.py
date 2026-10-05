"""Content fingerprints of directory trees.

Fingerprints serve two purposes: they key the sandbox image cache (an unchanged build
context reuses its image) and they are recorded in run manifests so a later run can prove
it used byte-identical inputs.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

DEFAULT_IGNORED_NAMES = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis"}
)
DEFAULT_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})


def is_ignored(relative: Path, ignored_names: frozenset[str] = DEFAULT_IGNORED_NAMES) -> bool:
    """Return whether a tree-relative path is a cache/VCS artefact excluded from comparisons."""
    return any(part in ignored_names for part in relative.parts) or (
        relative.suffix in DEFAULT_IGNORED_SUFFIXES
    )


def iter_tree_files(
    root: Path, ignored_names: frozenset[str] = DEFAULT_IGNORED_NAMES
) -> Iterator[Path]:
    """Yield root-relative paths of files and symlinks below ``root``, in sorted order.

    Symlinked directories are reported as links and never descended into, so traversal
    cannot leave the tree or loop forever.
    """
    found: list[Path] = []
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        base = Path(directory)
        subdirectories[:] = [name for name in subdirectories if name not in ignored_names]
        linked_directories = [name for name in subdirectories if (base / name).is_symlink()]
        for name in (*files, *linked_directories):
            relative = (base / name).relative_to(root)
            if not is_ignored(relative, ignored_names):
                found.append(relative)
    yield from sorted(found)


def hash_directory(root: Path, ignored_names: frozenset[str] = DEFAULT_IGNORED_NAMES) -> str:
    """Return a SHA-256 over every file's relative path, kind and content.

    The hash depends only on content, not on timestamps, permissions or traversal order.
    """
    digest = hashlib.sha256()
    for relative in iter_tree_files(root, ignored_names):
        path = root / relative
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        if path.is_symlink():
            digest.update(b"L" + str(path.readlink()).encode("utf-8") + b"\0")
        else:
            digest.update(b"F" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
