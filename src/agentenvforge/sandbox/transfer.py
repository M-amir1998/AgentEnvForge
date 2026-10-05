"""File transfer helpers built on the minimal :class:`~.base.Sandbox` protocol."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from agentenvforge.sandbox.base import Sandbox
from agentenvforge.workspace.archive import (
    Ownership,
    create_directory_archive,
    create_files_archive,
    extract_archive,
    read_single_file,
)

DEFAULT_READ_LIMIT_BYTES = 10 * 1024 * 1024


def upload_directory(
    sandbox: Sandbox, source: Path, destination: PurePosixPath, *, owner: Ownership
) -> None:
    """Copy the host directory ``source`` so it becomes ``destination`` in the sandbox."""
    archive = create_directory_archive(source, arcname=destination.name, owner=owner)
    with archive:
        sandbox.put_archive(destination.parent, archive)


def upload_files(
    sandbox: Sandbox,
    files: dict[str, bytes],
    destination: PurePosixPath,
    *,
    owner: Ownership,
    mode: int = 0o644,
) -> None:
    """Write in-memory ``files`` (relative names) below ``destination`` in the sandbox."""
    prefixed = {f"{destination.name}/{name}": content for name, content in files.items()}
    archive = create_files_archive(prefixed, owner=owner, mode=mode)
    sandbox.put_archive(destination.parent, archive)


def download_directory(
    sandbox: Sandbox, source: PurePosixPath, destination: Path
) -> tuple[str, ...]:
    """Copy the sandbox directory ``source`` into the host directory ``destination``.

    Returns names of unsafe entries that were skipped (see :func:`extract_archive`).
    """
    with sandbox.get_archive(source) as archive:
        return extract_archive(archive, destination, strip_components=1)


def read_file(
    sandbox: Sandbox, path: PurePosixPath, *, max_bytes: int = DEFAULT_READ_LIMIT_BYTES
) -> bytes:
    """Return the content of one file in the sandbox."""
    with sandbox.get_archive(path) as archive:
        return read_single_file(archive, max_bytes=max_bytes)
