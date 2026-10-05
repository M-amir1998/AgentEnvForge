"""Human-readable rendering for the CLI (Rich). Contains no business logic."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from agentenvforge.artifacts import ArtifactRecord
from agentenvforge.diagnostics import CheckResult, CheckStatus
from agentenvforge.domain.events import EventType, ExecutionEvent
from agentenvforge.domain.failures import TaskValidationError
from agentenvforge.domain.results import RunResult
from agentenvforge.domain.status import RunStatus
from agentenvforge.persistence.repository import RunSummary
from agentenvforge.tasks.loader import LoadedTask
from agentenvforge.tasks.schema import GitSource, LocalSource

STAGE_LABELS: dict[RunStatus, str] = {
    RunStatus.VALIDATING: "Validation",
    RunStatus.PREPARING_SOURCE: "Source preparation",
    RunStatus.BUILDING_SANDBOX: "Sandbox build",
    RunStatus.STARTING_SANDBOX: "Sandbox start",
    RunStatus.RUNNING_AGENT: "Agent execution",
    RunStatus.CAPTURING_SOLUTION: "Solution capture",
    RunStatus.RUNNING_GRADER: "Grader",
    RunStatus.COLLECTING_ARTIFACTS: "Artifacts",
    RunStatus.CLEANING_UP: "Cleanup",
}
_STATUS_STYLE = {CheckStatus.OK: "green", CheckStatus.WARN: "yellow", CheckStatus.FAIL: "red"}


def _seconds(milliseconds: int | float | None) -> str:
    return "" if milliseconds is None else f"{milliseconds / 1000:.2f}s"


class ProgressPrinter:
    """Event sink printing one line per completed lifecycle stage as the run progresses."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._stage_started: datetime | None = None

    def handle(self, event: ExecutionEvent) -> None:
        """Print a stage line whenever the run leaves an active state."""
        if event.event_type is EventType.RUN_CREATED:
            self._console.print(
                f"[bold]AgentEnvForge[/bold]  run [cyan]{event.run_id}[/cyan]  "
                f"task [cyan]{escape(event.task_id)}[/cyan]  "
                f"agent {escape(str(event.data.get('agent')))}\n"
            )
        if event.event_type is not EventType.RUN_STATUS_CHANGED:
            return
        previous = RunStatus(str(event.data["previous"]))
        current = RunStatus(str(event.data["current"]))
        started, self._stage_started = self._stage_started, event.timestamp
        label = STAGE_LABELS.get(previous)
        if label is None or started is None:
            return
        failed = current is RunStatus.CLEANING_UP and previous is not RunStatus.COLLECTING_ARTIFACTS
        verdict = "[red]FAIL[/red]" if failed else "[green]PASS[/green]"
        duration = (event.timestamp - started).total_seconds() * 1000
        self._console.print(f"  {label:<20} {verdict}  {_seconds(duration):>8}")


def render_validation_error(console: Console, error: TaskValidationError) -> None:
    """Print every validation issue in ``field / reason / received`` form."""
    console.print(f"[bold red]TaskValidationError[/bold red]: {escape(error.source)}")
    for issue in error.issues:
        console.print(f"\n  [bold]field:[/bold]    {escape(issue.field)}")
        console.print(f"  [bold]reason:[/bold]   {escape(issue.reason)}")
        if issue.received is not None:
            console.print(f"  [bold]received:[/bold] {escape(issue.received)}")


def _source_location(source: LocalSource | GitSource) -> str:
    if isinstance(source, LocalSource):
        return source.path
    return f"{source.repository} @ {source.revision}"


def render_task(console: Console, task: LoadedTask, *, verbose: bool) -> None:
    """Summarise a validated task."""
    spec = task.spec
    console.print(f"[green]VALID[/green] {escape(str(task.path))}")
    if not verbose:
        for warning in task.warnings:
            console.print(f"  [yellow]warning:[/yellow] {escape(warning)}")
        return
    table = Table(show_header=False, box=None, padding=(0, 2))
    source = spec.source
    rows = [
        ("id", spec.id),
        ("name", spec.name),
        ("schema", spec.schema_version),
        ("source", f"{source.type}: {_source_location(source)}"),
        ("image", spec.environment.dockerfile or spec.environment.image or ""),
        ("workspace", spec.environment.workspace),
        ("resources", f"{spec.resources.cpus:g} cpu, {spec.resources.memory_mb} MiB, "
                      f"{spec.resources.pids_limit} pids"),
        ("network", "enabled" if spec.network.enabled else "disabled"),
        ("agent budget", f"{spec.timeouts.agent_seconds}s"),
        ("graders", ", ".join(g.name or g.type for g in spec.grading.graders)),
        ("protected", ", ".join(spec.grading.protected_paths) or "-"),
        ("hidden assets", spec.grading.assets or "-"),
        ("definition hash", task.definition_hash[:16]),
    ]  # fmt: skip
    for key, value in rows:
        table.add_row(f"[bold]{key}[/bold]", escape(str(value)))
    console.print(table)
    for warning in task.warnings:
        console.print(f"  [yellow]warning:[/yellow] {escape(warning)}")


def render_run_result(console: Console, result: RunResult, artifacts_location: str) -> None:
    """Final report of a run, for both success and failure."""
    console.print()
    if result.status is RunStatus.COMPLETED and result.grade is not None:
        grade = result.grade
        verdict = (
            "[bold green]SUCCESS[/bold green]" if grade.passed else "[bold red]FAILED[/bold red]"
        )
        console.print(f"Result: {verdict}  ({escape(grade.summary)})")
        console.print(f"Score:  {grade.score:.3f}")
        for component in grade.components:
            mark = "[green]pass[/green]" if component.passed else "[red]fail[/red]"
            console.print(f"  - {escape(component.grader)}: {mark} {escape(component.summary)}")
    elif result.failure is not None:
        failure = result.failure
        console.print(f"[bold red]Run {result.status.value.upper()}[/bold red]\n")
        console.print(f"Stage:   {STAGE_LABELS.get(failure.stage, failure.stage.value)}")
        console.print(f"Reason:  {failure.reason.value}")
        console.print(f"Error:   {escape(failure.message)}")
        for key, value in failure.details.items():
            text = str(value)
            if len(text) > 300:
                text = "..." + text[-300:]
            console.print(f"  {escape(key)}: {escape(text)}")
    if result.agent is not None:
        console.print(
            f"Agent:   {escape(result.agent.summary)} ({result.agent.command_count} commands)"
        )
    cleanup = "[green]SUCCESS[/green]" if result.cleanup.succeeded else "[red]FAILED[/red]"
    console.print(f"Cleanup: {cleanup}")
    for error in result.cleanup.errors:
        console.print(f"  [red]{escape(error)}[/red]")
    console.print(f"\nRun ID:     {result.run_id}")
    console.print(f"Artifacts:  {escape(artifacts_location)}")
    console.print(f"Duration:   {_seconds(result.duration_ms)}")


def render_runs(console: Console, runs: Sequence[RunSummary]) -> None:
    """Table of runs, most recent first."""
    table = Table("task", "agent", "status", "result", "score", "duration", "created (UTC)")
    # Run IDs must stay copyable, so they are never wrapped or truncated.
    table.add_column("run id", no_wrap=True, min_width=30)
    table.columns.insert(0, table.columns.pop())
    for run in runs:
        outcome = {True: "[green]pass[/green]", False: "[red]fail[/red]", None: "-"}[run.passed]
        table.add_row(
            run.run_id,
            escape(run.task_id),
            escape(run.agent),
            run.status.value,
            outcome,
            "-" if run.score is None else f"{run.score:.3f}",
            _seconds(run.duration_ms) or "-",
            run.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)


def render_stage_timings(console: Console, result: RunResult) -> None:
    """Per-stage durations of a finished run."""
    table = Table("stage", "duration")
    for timing in result.stage_timings:
        if timing.status in STAGE_LABELS:
            table.add_row(STAGE_LABELS[timing.status], _seconds(timing.duration_ms))
    console.print(table)


def render_events(console: Console, events: Sequence[ExecutionEvent]) -> None:
    """Chronological event log."""
    table = Table("#", "time (UTC)")
    # Event names are the key to the log; never wrap or truncate them.
    table.add_column("event", no_wrap=True, min_width=26)
    for header in ("status", "duration", "data"):
        table.add_column(header)
    for event in events:
        data = " ".join(f"{key}={value}" for key, value in event.data.items())
        table.add_row(
            str(event.sequence),
            event.timestamp.strftime("%H:%M:%S.%f")[:-3],
            event.event_type.value,
            event.status.value,
            _seconds(event.duration_ms),
            escape(data[:160]),
        )
    console.print(table)


def render_artifacts(console: Console, artifacts: Sequence[ArtifactRecord], location: str) -> None:
    """List of a run's artifacts."""
    console.print(f"Location: {escape(location)}")
    table = Table("name", "size", "sha256")
    for artifact in artifacts:
        table.add_row(escape(artifact.name), f"{artifact.size_bytes:,} B", artifact.sha256[:12])
    console.print(table)


def render_checks(console: Console, checks: Sequence[CheckResult]) -> None:
    """Doctor report."""
    for check in checks:
        style = _STATUS_STYLE[check.status]
        console.print(
            f"  {check.name:<16} [{style}]{check.status.value.upper():<4}[/{style}]  "
            f"{escape(check.detail)}"
        )
        if check.hint and check.status is not CheckStatus.OK:
            console.print(f"  {'':<16}       [dim]{escape(check.hint)}[/dim]")
