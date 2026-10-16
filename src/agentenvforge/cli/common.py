"""State and conventions shared by every ``agentenv`` command module.

Exit codes (stable, documented in docs/reference/cli.md):

====  ===================================================================================
0     success (``run``: the grader passed; ``experiment run``: completed, results valid)
1     ``run``: the solution failed grading; ``experiment run``: completed, but the results
      have validity warnings (excluded runs, a failing reference, a passing baseline)
2     invalid input (task/agent/experiment specification, settings, usage)
3     infrastructure failure (``experiment run``: the experiment FAILED)
130   cancelled (Ctrl+C / SIGTERM)
====  ===================================================================================
"""

from __future__ import annotations

from dataclasses import dataclass

import typer
from rich.console import Console

from agentenvforge.config.settings import Settings

EXIT_OK = 0
EXIT_GRADE_FAILED = 1
EXIT_RESULTS_INVALID = 1
EXIT_INVALID = 2
EXIT_INFRASTRUCTURE = 3
EXIT_CANCELLED = 130

console = Console()
error_console = Console(stderr=True)


@dataclass(slots=True)
class CliState:
    """What the root callback resolved for every command."""

    settings: Settings


def cli_state(context: typer.Context) -> CliState:
    """The state the root callback stored on the context."""
    state = context.find_root().obj
    if not isinstance(state, CliState):  # pragma: no cover - guaranteed by the callback
        raise RuntimeError("CLI state was not initialised")
    return state


def print_plain(text: str) -> None:
    """Print text verbatim: task instructions and agent output are plain text, so square
    brackets must not be interpreted as Rich markup."""
    console.print(text, markup=False, highlight=False)
