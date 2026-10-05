"""Artifact storage (ADR-004).

Artifacts are the bulky outputs of a run: logs, command output, the solution patch,
grader reports, the manifest. They are stored by an :class:`ArtifactStore` - a local
directory today, an object store (S3, MinIO, GCS) later - while the database only keeps
small :class:`ArtifactRecord` rows that reference them. Relational databases are a poor
place for multi-megabyte blobs, and keeping blobs out makes the database portable.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from agentenvforge.domain.clock import utc_now
from agentenvforge.domain.failures import ArtifactError
from agentenvforge.domain.ids import new_artifact_id


class ArtifactRecord(BaseModel):
    """Metadata about one stored artifact."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str
    run_id: str
    name: str
    """Relative POSIX path within the run, e.g. ``grader/pytest/junit.xml``."""
    size_bytes: int
    sha256: str
    created_at: datetime


def validate_artifact_name(name: str) -> str:
    """Reject names that could escape the run's artifact namespace."""
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ArtifactError(f"invalid artifact name {name!r}: must be a relative path without '..'")
    return path.as_posix()


@runtime_checkable
class ArtifactStore(Protocol):
    """Public extension point: where artifact content lives."""

    def put(self, run_id: str, name: str, content: bytes) -> ArtifactRecord:
        """Store ``content`` under ``name`` for ``run_id``, replacing any previous version."""
        ...

    def get(self, run_id: str, name: str) -> bytes:
        """Return the content of an artifact; raises :class:`ArtifactError` if missing."""
        ...

    def location(self, run_id: str, name: str | None = None) -> str:
        """Human-readable location (path or URI) of a run's artifacts or one artifact."""
        ...


class LocalArtifactStore:
    """Stores artifacts below ``root/<run_id>/<name>`` on the local filesystem."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, run_id: str, name: str) -> Path:
        validate_artifact_name(run_id)
        return self._root / run_id / validate_artifact_name(name)

    def put(self, run_id: str, name: str, content: bytes) -> ArtifactRecord:
        """Write atomically (temp file + rename) so readers never see partial artifacts."""
        target = self._path(run_id, name)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            Path(temporary).replace(target)
        except OSError as error:
            raise ArtifactError(f"cannot store artifact {name!r} for {run_id}: {error}") from error
        return ArtifactRecord(
            artifact_id=new_artifact_id(),
            run_id=run_id,
            name=validate_artifact_name(name),
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=utc_now(),
        )

    def get(self, run_id: str, name: str) -> bytes:
        """Read an artifact's content."""
        try:
            return self._path(run_id, name).read_bytes()
        except OSError as error:
            raise ArtifactError(f"cannot read artifact {name!r} for {run_id}: {error}") from error

    def location(self, run_id: str, name: str | None = None) -> str:
        """Filesystem path of the run directory or of one artifact."""
        return str(
            self._path(run_id, name) if name else self._root / validate_artifact_name(run_id)
        )
