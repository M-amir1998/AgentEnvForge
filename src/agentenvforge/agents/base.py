"""The agent extension point (ADR-003).

AgentEnvForge is agent-agnostic infrastructure. Anything that can act on a sandbox
can be an agent: a shell script, a SWE-agent process, an LLM tool-use loop running in
this Python process, or a human. All of them integrate through :class:`AgentAdapter`.

Adapters run on the host and act on the sandbox exclusively through
:class:`~agentenvforge.sandbox.session.SandboxSession`. That keeps every action
observable (each command becomes an event) and bounded by the run's time budget,
whatever the adapter's implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

from agentenvforge.domain.results import AgentResult
from agentenvforge.domain.types import Metadata
from agentenvforge.sandbox.session import SandboxSession

type ArtifactWriter = Callable[[str, bytes], None]
"""Saves ``content`` as a run artifact named ``name`` (e.g. ``agent/transcript.jsonl``)."""


@dataclass(frozen=True, slots=True)
class AgentTask:
    """The task as presented to an agent: only agent-visible information."""

    task_id: str
    name: str
    instructions: str
    workspace: PurePosixPath


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Everything an adapter receives for one run."""

    run_id: str
    task: AgentTask
    session: SandboxSession
    time_budget_seconds: float
    save_artifact: ArtifactWriter
    seed: int | None = None
    """Seed for agents that sample stochastically (e.g. LLM decoding). Experiments vary it
    between repetitions so repeated attempts are independent samples; ``None`` means the
    agent keeps its own configuration."""


@runtime_checkable
class AgentAdapter(Protocol):
    """Public extension point: integrate an agent with AgentEnvForge.

    ``run`` should return when the agent considers itself done. Returning an
    :class:`AgentResult` with ``outcome=FAILED`` means "the agent ran but gave up/crashed";
    its workspace is still graded. Raise :class:`~agentenvforge.domain.failures.AgentError`
    only if the adapter could not run the agent at all.
    """

    @property
    def name(self) -> str:
        """Identifier recorded with every run (e.g. ``command:my-solver``)."""
        ...

    def run(self, context: AgentContext) -> AgentResult:
        """Act on ``context.session`` until done, then report the outcome."""
        ...


@runtime_checkable
class SupportsPreflight(Protocol):
    """Optional extension: adapters that depend on an external service can check it early.

    ``preflight`` is called once per agent before an experiment (or a single run) starts, so
    a missing model or an unreachable server fails fast with an actionable message instead
    of failing every run. It raises :class:`~agentenvforge.domain.failures.AgentError` when
    the agent cannot work, and returns provenance worth recording (versions, model digests).
    """

    def preflight(self) -> Metadata:
        """Check availability; return provenance metadata."""
        ...
