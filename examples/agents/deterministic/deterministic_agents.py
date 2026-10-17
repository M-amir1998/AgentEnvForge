"""Deterministic host-side adapters for infrastructure tests and benchmarks.

These are not AI agents. They produce known outcomes so that batch behaviour (failure
isolation, classification, aggregation) can be verified without any model.
"""

from __future__ import annotations

from agentenvforge.agents.base import AgentContext
from agentenvforge.domain.results import AgentResult


class CrashingAgent:
    """An adapter whose ``run`` always raises, simulating a buggy agent integration."""

    @property
    def name(self) -> str:
        """``crasher``."""
        return "crasher"

    def run(self, context: AgentContext) -> AgentResult:
        """Raise immediately; the orchestrator must classify this as an agent error."""
        raise RuntimeError(f"deliberate adapter crash in run {context.run_id}")


def build_crasher() -> CrashingAgent:
    """Factory referenced by ``crasher.yaml``."""
    return CrashingAgent()
