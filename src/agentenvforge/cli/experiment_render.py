"""Rendering of experiments for the CLI (Rich). Contains no business logic."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from agentenvforge.analysis.models import AgentSummary, ExperimentSummary, Rate
from agentenvforge.analysis.outcomes import Outcome
from agentenvforge.domain.results import RunResult
from agentenvforge.execution.experiments import Progress
from agentenvforge.experiments.loader import LoadedExperiment
from agentenvforge.experiments.matrix import RunCombination, expand_matrix
from agentenvforge.persistence.experiment_repository import ExperimentRecord, StoredExperiment

_OUTCOME_STYLE = {
    Outcome.PASSED: "green",
    Outcome.FAILED: "red",
    Outcome.AGENT_TIMEOUT: "yellow",
    Outcome.SOLUTION_TIMEOUT: "yellow",
    Outcome.AGENT_ERROR: "red",
    Outcome.POST_AGENT_ERROR: "red",
}
"""Scored outcomes; everything else (excluded from success rates) is shown dimmed."""


_OUTCOME_WIDTH = max(len(outcome.value) for outcome in Outcome)
_TOTALS_EVERY = 10
_REASON_CHARACTERS = 100


def _outcome(outcome: Outcome) -> str:
    # Padded before styling, so the markup does not count towards the column width.
    label = outcome.value.replace("_", " ").ljust(_OUTCOME_WIDTH)
    return f"[{_OUTCOME_STYLE.get(outcome, 'magenta')}]{label}[/]"


def _rate(rate: Rate) -> str:
    if rate.estimate is None:
        return "-"
    return f"{rate.successes}/{rate.trials} {rate.estimate:.0%}"


def _interval(rate: Rate, *, identical: bool) -> str:
    if identical:
        return "identical"
    if rate.ci_low is None or rate.ci_high is None:
        return "-"
    return f"{rate.ci_low:.0%}-{rate.ci_high:.0%}"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, rest = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{rest:02d}s" if minutes else f"{seconds:.1f}s"


# ------------------------------------------------------------------------------- planning


def render_plan(
    console: Console,
    loaded: LoadedExperiment,
    *,
    workers: int,
    warnings: Sequence[str],
    output: Path | str,
    verbose: bool,
) -> None:
    """What an experiment will do (``validate``, ``inspect`` and the start of ``run``)."""
    spec = loaded.spec
    planned = len(loaded.tasks) * len(loaded.agents) * spec.repetitions
    console.print(
        f"[bold]experiment[/bold] [cyan]{escape(spec.id)}[/cyan]"
        + (f"  {escape(spec.name)}" if spec.name else "")
    )
    console.print(
        f"  {len(loaded.tasks)} task(s) x {len(loaded.agents)} agent(s) x {spec.repetitions} "
        f"repetition(s) = [bold]{planned}[/bold] planned runs, {workers} worker(s)"
    )
    console.print(
        f"  definition hash {loaded.definition_hash[:16]}, base seed {spec.execution.base_seed}"
    )
    console.print(f"  results: {escape(str(output))}", soft_wrap=True)
    for warning in warnings:
        console.print(f"  [yellow]warning:[/yellow] {escape(warning)}")
    if not verbose:
        return

    tasks = Table("task", "file", "agent budget", "graders", title="Tasks", title_justify="left")
    for task, relative in zip(loaded.tasks, spec.tasks, strict=True):
        tasks.add_row(
            escape(task.spec.id),
            escape(str(relative)),
            f"{task.spec.timeouts.agent_seconds}s",
            ", ".join(grader.type for grader in task.spec.grading.graders),
        )
    console.print(tasks)
    agents = Table(
        "agent",
        "role",
        "kind",
        "declared as",
        "budget override",
        "spec hash",
        "code hash",
        title="Agents",
        title_justify="left",
    )
    for agent in loaded.agents:
        agents.add_row(
            escape(agent.agent_id),
            agent.role,
            agent.spec.type,
            escape(agent.declared_as),
            f"{agent.timeout_override}s" if agent.timeout_override else "-",
            agent.spec_hash[:12],
            (agent.code_hash or "-")[:12],
        )
    console.print(agents)
    order = Table(
        "#",
        "repetition",
        "task",
        "agent",
        "seed",
        title="Run order (first 12)",
        title_justify="left",
    )
    combinations = expand_matrix(
        loaded.task_ids,
        loaded.agent_ids,
        spec.repetitions,
        base_seed=spec.execution.base_seed,
    )
    for combination in list(combinations)[:12]:
        order.add_row(
            str(combination.position),
            str(combination.repetition),
            escape(combination.task_id),
            escape(combination.agent_id),
            str(combination.seed),
        )
    console.print(order)


# ------------------------------------------------------------------------------- progress


class ExperimentProgressPrinter:
    """Experiment observer printing one line per finished planned run."""

    def __init__(self, console: Console) -> None:
        self._console = console

    def preparing(self, message: str) -> None:
        """A preparation step started."""
        self._console.print(f"[dim]  {escape(message)} ...[/dim]")

    def run_finished(
        self,
        combination: RunCombination,
        run_id: str | None,
        outcome: Outcome,
        result: RunResult | None,
        progress: Progress,
    ) -> None:
        """Print the run's outcome (and why it failed); totals every few runs."""
        width = len(str(progress.planned))
        seconds = result.duration_ms / 1000 if result is not None else None
        self._console.print(
            f"  [{progress.finished:>{width}}/{progress.planned}] {_outcome(outcome)} "
            f"{_duration(seconds):>7}  {escape(combination.agent_id)} / "
            f"{escape(combination.task_id)} #{combination.repetition}",
            soft_wrap=True,
        )
        failure = result.failure if result is not None else None
        if failure is not None and outcome is not Outcome.FAILED:
            message = " ".join(failure.message.split())[:_REASON_CHARACTERS]
            self._console.print(f"[dim]      {escape(message)} ({run_id})[/dim]", soft_wrap=True)
        if progress.finished % _TOTALS_EVERY == 0 or progress.finished == progress.planned:
            self._console.print(
                f"[dim]      {progress.passed} passed, {progress.failed} failed, "
                f"{progress.excluded} excluded, {progress.active} active, "
                f"{_duration(progress.elapsed_seconds)} elapsed[/dim]"
            )

    def cancelling(self, reason: str) -> None:
        """Cancellation was requested."""
        self._console.print(
            f"[yellow]cancelling ({escape(reason)}): no new runs start; active runs stop and "
            "clean up[/yellow]"
        )


# -------------------------------------------------------------------------------- results


def render_summary(console: Console, summary: ExperimentSummary) -> None:
    """Per-agent results and validity warnings."""
    table = Table(
        title=f"Experiment {summary.experiment_id} ({summary.status})",
        title_justify="left",
        box=box.SIMPLE_HEAD,
        pad_edge=False,
        padding=(0, 1, 0, 0),
    )
    # Only model names fold on narrow terminals; agent IDs and numbers stay whole.
    table.add_column("agent", no_wrap=True)
    table.add_column("model", overflow="fold")
    for header in ("scored", "success", "95% CI", "timeout", "excluded", "median"):
        table.add_column(header, no_wrap=True)
    for agent in summary.agents:
        table.add_row(*_agent_row(agent))
    console.print(table)
    counts = summary.counts
    console.print(
        f"{counts.planned} planned: {counts.passed} passed, {counts.scored - counts.passed} "
        f"failed (scored), {counts.unscored} excluded; running time "
        f"{_duration(summary.running_seconds)}"
    )
    for warning in summary.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")
    if summary.results_valid:
        console.print("[green]results valid[/green] (no validity warnings)")


def _agent_row(agent: AgentSummary) -> list[str]:
    median = agent.run_seconds.median if agent.run_seconds else None
    name = escape(agent.agent_id)
    return [
        name if agent.role == "subject" else f"{name} [dim]({agent.role})[/dim]",
        escape(agent.model or agent.adapter),
        str(agent.counts.scored),
        _rate(agent.success),
        _interval(agent.success, identical=agent.deterministic),
        str(agent.counts.agent_timeout),
        str(agent.counts.unscored),
        _duration(median),
    ]


def render_experiment_list(
    console: Console, records: Sequence[ExperimentRecord], statuses: Sequence[str]
) -> None:
    """Table of experiments, most recent first."""
    # Compact enough for 80 columns; IDs stay whole so they can be copied.
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, padding=(0, 1, 0, 0))
    table.add_column("experiment id", no_wrap=True, min_width=30)
    table.add_column("definition", no_wrap=True, overflow="ellipsis", min_width=10)
    table.add_column("status", no_wrap=True, min_width=len("interrupted"))
    table.add_column("runs", justify="right", no_wrap=True, min_width=4)
    table.add_column("created (UTC)", no_wrap=True)
    for record, status in zip(records, statuses, strict=True):
        table.add_row(
            record.experiment_id,
            escape(record.spec_id),
            status,
            str(record.planned_runs),
            record.created_at.strftime("%m-%d %H:%M"),
        )
    console.print(table)


def render_experiment_detail(
    console: Console, stored: StoredExperiment, summary: ExperimentSummary
) -> None:
    """Everything about one experiment (``experiments show``)."""
    record, manifest = stored.record, stored.manifest
    console.print(
        f"[bold]{record.experiment_id}[/bold]  {escape(record.spec_id)}  {summary.status}"
    )
    console.print(
        f"agentenvforge {manifest.agentenvforge_version}, definition hash "
        f"{manifest.definition_hash[:16]}, {manifest.repetitions} repetition(s), "
        f"{record.workers} worker(s), base seed {manifest.base_seed}"
    )
    if record.definition_path:
        console.print(f"definition: {escape(record.definition_path)}")
    console.print(f"results: {escape(record.output_dir or '- (not published)')}")
    history = "  ".join(
        f"{change.status.value} {change.entered_at.strftime('%H:%M:%S')}"
        for change in stored.history
    )
    console.print(f"history: {history}")
    started = sum(1 for entry in stored.entries if entry.run_id)
    console.print(f"planned runs: {len(stored.entries)}, started: {started}")
    if manifest.environment is not None:
        env = manifest.environment
        console.print(
            f"environment: {env.os} {env.os_release}, {escape(env.cpu_model or '-')}, "
            f"{env.logical_cpus} CPUs, Python {env.python_version}, "
            f"{env.sandbox_backend} {env.sandbox_backend_version}"
        )
    for agent in manifest.agents:
        provenance = ", ".join(
            f"{key}={value}" for key, value in agent.provenance.items() if value is not None
        )
        console.print(
            f"  agent {escape(agent.agent_id)} ({agent.role}): {escape(agent.adapter)}"
            + (f"  [dim]{escape(provenance)}[/dim]" if provenance else "")
        )
    render_summary(console, summary)
