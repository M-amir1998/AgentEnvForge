"""Deriving the sandbox image description of a task.

Shared by the run orchestrator (which prepares the image of the run it executes) and the
experiment executor (which prepares every task's image once, before scheduling runs), so
both always agree on the cache key and therefore on the image a run uses.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from agentenvforge.sandbox.base import ImageSpec
from agentenvforge.tasks.loader import LoadedTask
from agentenvforge.workspace.fingerprint import hash_directory


@dataclass(frozen=True, slots=True)
class TaskImage:
    """What to ask the sandbox backend for, plus provenance for the manifest."""

    spec: ImageSpec
    build_context_hash: str | None
    """Content hash of the Dockerfile build context (``None`` for prebuilt images)."""


def task_image(task: LoadedTask) -> TaskImage:
    """Return the image specification of ``task``.

    For Dockerfile tasks the cache key covers the build context's content and the Dockerfile
    path, so an unchanged context reuses a previously built image.
    """
    environment = task.spec.environment
    timeout = task.spec.timeouts.build_seconds
    if environment.dockerfile is None:
        spec = ImageSpec(
            task_id=task.spec.id,
            dockerfile=None,
            context=None,
            image=environment.image,
            cache_key="",
            timeout_seconds=timeout,
        )
        return TaskImage(spec=spec, build_context_hash=None)

    dockerfile = task.host_path(environment.dockerfile)
    context = task.host_path(environment.context) if environment.context else dockerfile.parent
    context_hash = hash_directory(context)
    cache_key = hashlib.sha256(
        f"{context_hash}:{dockerfile.relative_to(context).as_posix()}".encode()
    ).hexdigest()
    spec = ImageSpec(
        task_id=task.spec.id,
        dockerfile=dockerfile,
        context=context,
        image=None,
        cache_key=cache_key,
        timeout_seconds=timeout,
    )
    return TaskImage(spec=spec, build_context_hash=context_hash)
