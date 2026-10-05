"""Sandbox backends: the isolation boundary between task code and the host.

Public extension point: :class:`~agentenvforge.sandbox.base.SandboxBackend` and
:class:`~agentenvforge.sandbox.base.Sandbox`. The Docker implementation lives in
:mod:`agentenvforge.sandbox.docker_backend` and is imported lazily by callers so that
importing this package never requires a Docker installation.
"""

from agentenvforge.sandbox.base import (
    BackendInfo,
    ImageSpec,
    PreparedImage,
    Sandbox,
    SandboxBackend,
    SandboxConfig,
    SandboxRole,
)

__all__ = [
    "BackendInfo",
    "ImageSpec",
    "PreparedImage",
    "Sandbox",
    "SandboxBackend",
    "SandboxConfig",
    "SandboxRole",
]
