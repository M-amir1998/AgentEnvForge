"""Computing what an agent changed.

The diff is computed on the host by comparing the pristine source tree with the agent's
final workspace. Doing it here, instead of running ``git diff`` in the sandbox, means task
images do not need git, and a misbehaving agent cannot tamper with the comparison (for
example by rewriting git history inside the sandbox).

The produced patch uses git's extended format so ``git apply`` can replay it on a clean
checkout, which is the basis for reproducing and re-grading solutions.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from agentenvforge.workspace.fingerprint import iter_tree_files

_BINARY_SNIFF_BYTES = 8000


@dataclass(frozen=True, slots=True)
class WorkspaceDiff:
    """Changes between two directory trees."""

    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    patch: str

    @property
    def changed_files(self) -> tuple[str, ...]:
        """Every path that differs, sorted."""
        return tuple(sorted((*self.added, *self.modified, *self.deleted)))

    @property
    def is_empty(self) -> bool:
        """True when the trees are identical."""
        return not (self.added or self.modified or self.deleted)


def _content(path: Path) -> bytes:
    # Symlinks are compared by target and never followed: following a link could read
    # files outside the tree.
    if path.is_symlink():
        return b"symlink:" + str(path.readlink()).encode("utf-8")
    return path.read_bytes()


def _read_text(path: Path | None) -> str | None:
    """Return file text, or ``None`` if it is binary or a symlink (absent -> empty)."""
    if path is None:
        return ""
    if path.is_symlink():
        return None
    data = path.read_bytes()
    if b"\0" in data[:_BINARY_SNIFF_BYTES]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _file_patch(name: str, before: Path | None, after: Path | None) -> str:
    header = [f"diff --git a/{name} b/{name}\n"]
    if before is None:
        header.append("new file mode 100644\n")
    elif after is None:
        header.append("deleted file mode 100644\n")
    old_label = f"a/{name}" if before is not None else "/dev/null"
    new_label = f"b/{name}" if after is not None else "/dev/null"

    old_text, new_text = _read_text(before), _read_text(after)
    if old_text is None or new_text is None:
        return "".join(header) + f"Binary files {old_label} and {new_label} differ\n"

    lines = []
    for line in difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=old_label,
        tofile=new_label,
    ):
        # git requires an explicit marker when the final line has no newline; without it
        # `git apply` would silently add one and the replayed file would differ.
        lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(header + lines)


def diff_trees(before_root: Path, after_root: Path) -> WorkspaceDiff:
    """Compare two trees, ignoring VCS metadata and caches."""
    before = {path.as_posix() for path in iter_tree_files(before_root)}
    after = {path.as_posix() for path in iter_tree_files(after_root)}

    added = sorted(after - before)
    deleted = sorted(before - after)
    modified = sorted(
        name
        for name in before & after
        if _content(before_root / name) != _content(after_root / name)
    )

    patches: Iterable[str] = (
        _file_patch(
            name,
            None if name in added else before_root / name,
            None if name in deleted else after_root / name,
        )
        for name in sorted((*added, *modified, *deleted))
    )
    return WorkspaceDiff(
        added=tuple(added), modified=tuple(modified), deleted=tuple(deleted), patch="".join(patches)
    )
