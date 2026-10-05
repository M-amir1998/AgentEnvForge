"""Preparing a task's source tree on the host.

The prepared tree is the *pristine* workspace: it is copied into the agent sandbox, used
as the baseline for the solution diff, and used to restore protected paths before
grading.

Git checkouts run on the host but never execute repository-controlled code: hooks are
disabled, the ``ext::`` transport (which can run arbitrary commands) is blocked, and
credential prompts are suppressed so a private URL fails fast instead of hanging.
The *resolved* commit SHA is always recorded, because branch names are mutable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentenvforge.domain.failures import SourcePreparationError
from agentenvforge.tasks.loader import LoadedTask
from agentenvforge.tasks.schema import GitSource, LocalSource
from agentenvforge.workspace.fingerprint import DEFAULT_IGNORED_NAMES, hash_directory

_COPY_IGNORE = shutil.ignore_patterns(*(DEFAULT_IGNORED_NAMES - {".git"}), "*.pyc")
_GIT_SAFETY_OPTIONS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.ext.allow=never",
    "-c", "advice.detachedHead=false",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class PreparedSource:
    """Provenance of a prepared source tree (recorded in the run manifest)."""

    path: Path
    source_type: str
    tree_hash: str
    repository: str | None = None
    requested_revision: str | None = None
    resolved_commit: str | None = None


def prepare_source(
    task: LoadedTask, destination: Path, *, timeout_seconds: float
) -> PreparedSource:
    """Materialise the task source at ``destination`` (which must not exist yet)."""
    source = task.spec.source
    if isinstance(source, LocalSource):
        return _copy_local(task.host_path(source.path), destination)
    return _checkout_git(source, destination, timeout_seconds=timeout_seconds)


def _copy_local(source_dir: Path, destination: Path) -> PreparedSource:
    try:
        # symlinks=True copies links as links: a link pointing outside the tree is
        # archived as a link, never as the content of the host file it points to.
        shutil.copytree(source_dir, destination, symlinks=True, ignore=_COPY_IGNORE)
    except OSError as error:
        raise SourcePreparationError(
            f"cannot copy source directory {source_dir}: {error}"
        ) from error
    return PreparedSource(
        path=destination, source_type="local", tree_hash=hash_directory(destination)
    )


def _run_git(arguments: list[str], *, cwd: Path | None, timeout_seconds: float) -> str:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
    command = ["git", *_GIT_SAFETY_OPTIONS, *arguments]
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=environment, capture_output=True, text=True,
            timeout=timeout_seconds, check=False,
        )  # fmt: skip
    except FileNotFoundError as error:
        raise SourcePreparationError("git is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise SourcePreparationError(
            f"git {arguments[0]} exceeded {timeout_seconds:g}s",
            details={"timeout_seconds": timeout_seconds},
        ) from error
    if completed.returncode != 0:
        raise SourcePreparationError(
            f"git {arguments[0]} failed: {completed.stderr.strip()[-1000:]}",
            details={"exit_code": completed.returncode},
        )
    return completed.stdout.strip()


def _checkout_git(
    source: GitSource, destination: Path, *, timeout_seconds: float
) -> PreparedSource:
    clone_dir = (
        destination.with_name(destination.name + "-clone") if source.subdirectory else destination
    )
    _run_git(
        ["clone", "--no-checkout", "--quiet", source.repository, str(clone_dir)],
        cwd=None,
        timeout_seconds=timeout_seconds,
    )
    _run_git(
        ["checkout", "--quiet", "--detach", source.revision],
        cwd=clone_dir,
        timeout_seconds=timeout_seconds,
    )
    resolved = _run_git(["rev-parse", "HEAD"], cwd=clone_dir, timeout_seconds=timeout_seconds)

    if source.subdirectory:
        subdirectory = (clone_dir / source.subdirectory).resolve()
        if not subdirectory.is_relative_to(clone_dir.resolve()) or not subdirectory.is_dir():
            raise SourcePreparationError(
                f"subdirectory {source.subdirectory!r} does not exist at {resolved}"
            )
        _copy_local(subdirectory, destination)
        remove_tree(clone_dir)

    return PreparedSource(
        path=destination,
        source_type="git",
        tree_hash=hash_directory(destination),
        repository=source.repository,
        requested_revision=source.revision,
        resolved_commit=resolved,
    )


def remove_tree(path: Path) -> None:
    """Delete a directory tree, including read-only files (git objects on Windows)."""

    def _make_writable_and_retry(function: object, failed_path: str, _error: BaseException) -> None:
        Path(failed_path).chmod(0o700)
        if callable(function):
            function(failed_path)

    if path.exists():
        shutil.rmtree(path, onexc=_make_writable_and_retry)
