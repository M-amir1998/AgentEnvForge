"""Tar archives for moving directory trees in and out of sandboxes.

Sandboxes receive files by archive copy rather than host bind mounts (ADR-001): nothing on
the host is ever visible inside a container, and the same mechanism works against remote
Docker daemons. Archives produced *by a sandbox* are untrusted input, so extraction uses
the stdlib ``data`` filter (rejects absolute paths, ``..`` traversal, links escaping the
destination, device files) and enforces a total size limit.
"""

from __future__ import annotations

import io
import tarfile
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from agentenvforge.domain.failures import SandboxError

_SPOOL_IN_MEMORY_BYTES = 16 * 1024 * 1024
DEFAULT_EXTRACT_LIMIT_BYTES = 2 * 1024 * 1024 * 1024


class ArchiveTooLargeError(SandboxError):
    """An archive from a sandbox exceeded the extraction size limit."""


@dataclass(frozen=True, slots=True)
class Ownership:
    """Numeric owner applied to every archived entry."""

    uid: int
    gid: int

    @classmethod
    def parse(cls, value: str) -> Ownership:
        """Parse ``"uid:gid"`` (already validated by the task schema)."""
        uid, gid = value.split(":", maxsplit=1)
        return cls(int(uid), int(gid))


def _normalised_mode(member: tarfile.TarInfo) -> int:
    # Host permission bits are not meaningful inside the sandbox (and on Windows they do
    # not exist), so use predictable modes and keep only the "executable" signal.
    if member.isdir() or member.mode & 0o111:
        return 0o755
    return 0o644


def create_directory_archive(
    source: Path, *, arcname: str, owner: Ownership, exclude_names: Iterable[str] = ()
) -> IO[bytes]:
    """Archive ``source`` so that it extracts as ``arcname/...``.

    Returns a rewound binary file object; large trees spill to a temporary file instead of
    being held in memory.
    """
    excluded = frozenset(exclude_names)

    def _prepare(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if PurePosixPath(member.name).name in excluded:
            return None
        member.uid, member.gid = owner.uid, owner.gid
        member.uname = member.gname = ""
        member.mode = _normalised_mode(member)
        return member

    buffer = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY_BYTES)  # noqa: SIM115
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(source, arcname=arcname, recursive=True, filter=_prepare)
    buffer.seek(0)
    return buffer


def create_files_archive(
    files: Mapping[str, bytes], *, owner: Ownership, mode: int = 0o644
) -> IO[bytes]:
    """Archive in-memory ``files`` keyed by relative POSIX path."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directories: set[PurePosixPath] = set()
        for name in sorted(files):
            for parent in reversed(PurePosixPath(name).parents[:-1]):
                if parent not in directories:
                    directories.add(parent)
                    info = tarfile.TarInfo(parent.as_posix())
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    info.uid, info.gid = owner.uid, owner.gid
                    archive.addfile(info)
            content = files[name]
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(content), mode
            info.uid, info.gid = owner.uid, owner.gid
            archive.addfile(info, io.BytesIO(content))
    buffer.seek(0)
    return buffer


def extract_archive(
    stream: IO[bytes],
    destination: Path,
    *,
    strip_components: int = 0,
    max_total_bytes: int = DEFAULT_EXTRACT_LIMIT_BYTES,
) -> tuple[str, ...]:
    """Safely extract an untrusted tar ``stream`` into ``destination``.

    ``strip_components`` drops leading path elements, like ``tar --strip-components``;
    Docker archives of ``/workspace`` contain a top-level ``workspace/`` entry.

    Returns the names of entries that were *skipped* because they were unsafe (e.g. a
    symlink to ``/etc/passwd``). Skipping rather than failing matters: the archive content
    is controlled by the agent, and an agent must not be able to turn its own misbehaviour
    into an infrastructure failure of the run.
    """
    destination.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    skipped: list[str] = []

    def _filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        nonlocal total_bytes
        parts = PurePosixPath(member.name).parts[strip_components:]
        if not parts:
            return None
        total_bytes += member.size
        if total_bytes > max_total_bytes:
            raise ArchiveTooLargeError(
                f"archive exceeds the {max_total_bytes} byte extraction limit",
                details={"limit_bytes": max_total_bytes},
            )
        member = member.replace(name=PurePosixPath(*parts).as_posix(), deep=False)
        if member.islnk() and strip_components:
            link_parts = PurePosixPath(member.linkname).parts[strip_components:]
            member = member.replace(linkname=PurePosixPath(*link_parts).as_posix(), deep=False)
        try:
            return tarfile.data_filter(member, path)
        except tarfile.FilterError:
            skipped.append(member.name)
            return None

    try:
        with tarfile.open(fileobj=stream, mode="r|*") as archive:
            archive.extractall(destination, filter=_filter)  # noqa: S202 - data filter applied
    except tarfile.TarError as error:
        raise SandboxError(f"cannot extract sandbox archive: {error}") from error
    return tuple(skipped)


def read_single_file(stream: IO[bytes], *, max_bytes: int) -> bytes:
    """Return the content of the single regular file in a tar ``stream``."""
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            if member.size > max_bytes:
                raise ArchiveTooLargeError(
                    f"file {member.name!r} is {member.size} bytes; limit is {max_bytes}",
                    details={"limit_bytes": max_bytes, "size_bytes": member.size},
                )
            handle = archive.extractfile(member)
            if handle is None:
                break
            return handle.read()
    raise SandboxError("archive does not contain a regular file")
