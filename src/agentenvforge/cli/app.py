"""The ``agentenv`` command-line interface.

Commands are thin: parse arguments, call application services, render results.

Exit codes (stable, documented in docs/reference/cli.md):

====  ==========================================================
0     success (for ``run``: completed and the grader passed)
1     the run completed but the solution failed grading
2     invalid input (task/agent specification, settings, usage)
3     the run failed for an infrastructure reason
130   cancelled (Ctrl+C / SIGTERM)
====  ==========================================================
"""

from __future__ import annotations

import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from agentenvforge._version import __version__
from agentenvforge.agents.base import AgentAdapter
from agentenvforge.agents.builtin import CommandAgent
from agentenvforge.agents.spec import AgentEnvironment, command_agent_from_string, resolve_agent
from agentenvforge.cli import render
from agentenvforge.cli.wiring import create_backend, open_services
from agentenvforge.config.settings import LogFormat, LogLevel, Settings, load_settings
from agentenvforge.diagnostics import CheckStatus, run_checks
from agentenvforge.domain.failures import (
    AgentEnvForgeError,
    RunCancelledError,
    TaskValidationError,
    ValidationIssue,
)
from agentenvforge.domain.status import RunStatus
from agentenvforge.execution.orchestrator import RunRequest
from agentenvforge.tasks.loader import discover_task_files, find_duplicate_ids, load_task
from agentenvforge.tasks.schema import TaskSpec
from agentenvforge.telemetry.logging import configure_logging

EXIT_OK = 0
EXIT_GRADE_FAILED = 1
EXIT_INVALID = 2
EXIT_INFRASTRUCTURE = 3
EXIT_CANCELLED = 130

app = typer.Typer(
    name="agentenv",
    help="Define, run, grade and reproduce isolated software-engineering tasks for AI agents.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    rich_markup_mode="rich",
)
task_app = typer.Typer(help="Validate and inspect task specifications.", no_args_is_help=True)
runs_app = typer.Typer(help="Inspect recorded runs.", no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(runs_app, name="runs")

console = Console()
error_console = Console(stderr=True)


@dataclass(slots=True)
class _State:
    settings: Settings


def _state(context: typer.Context) -> _State:
    state = context.find_root().obj
    if not isinstance(state, _State):  # pragma: no cover - guaranteed by the callback
        raise RuntimeError("CLI state was not initialised")
    return state


@app.callback()
def _main(
    context: typer.Context,
    home: Annotated[
        Path | None,
        typer.Option(help="State directory (database, artifacts). Env: AGENTENV_HOME."),
    ] = None,
    log_level: Annotated[
        LogLevel | None, typer.Option(help="Log level for stderr. Env: AGENTENV_LOG_LEVEL.")
    ] = None,
    log_format: Annotated[
        LogFormat | None, typer.Option(help="text or json. Env: AGENTENV_LOG_FORMAT.")
    ] = None,
) -> None:
    try:
        settings = load_settings(
            cli_overrides={"home": home, "log_level": log_level, "log_format": log_format}
        )
    except TaskValidationError as error:
        render.render_validation_error(error_console, error)
        raise typer.Exit(EXIT_INVALID) from error
    configure_logging(settings.log_level, settings.log_format, sys.stderr)
    context.obj = _State(settings=settings)


# ------------------------------------------------------------------- general


@app.command()
def version() -> None:
    """Print the AgentEnvForge version."""
    console.print(f"agentenvforge {__version__}")


@app.command()
def doctor(context: typer.Context) -> None:
    """Check that this machine can run AgentEnvForge tasks."""
    settings = _state(context).settings
    checks = run_checks(settings, lambda: create_backend(settings))
    render.render_checks(console, checks)
    if any(check.status is CheckStatus.FAIL for check in checks):
        raise typer.Exit(EXIT_INFRASTRUCTURE)


@app.command()
def cleanup(
    context: typer.Context,
    run_id: Annotated[str | None, typer.Option(help="Only remove sandboxes of this run.")] = None,
    remove_all: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Also remove sandboxes whose owner process is still running, belongs to "
            "another host, or is unknown (created by AgentEnvForge < 0.2).",
        ),
    ] = False,
) -> None:
    """Remove sandboxes left behind by crashed or killed processes.

    By default only sandboxes whose owning process has exited are removed, so runs and
    experiments executing in other terminals are never disturbed.
    """
    backend = create_backend(_state(context).settings)
    try:
        removed = backend.remove_orphaned_sandboxes(run_id, include_live=remove_all)
    except AgentEnvForgeError as error:
        error_console.print(f"[red]{escape(error.message)}[/red]")
        raise typer.Exit(EXIT_INFRASTRUCTURE) from error
    console.print(f"removed {len(removed)} sandbox(es)")
    for name in removed:
        console.print(f"  {name}")
    if run_id is None and not remove_all:
        console.print("[dim]sandboxes of live processes were kept; use --all to remove them[/dim]")


# --------------------------------------------------------------------- tasks


@task_app.command("validate")
def task_validate(
    paths: Annotated[list[Path], typer.Argument(help="Task files or directories to search.")],
) -> None:
    """Validate task files (and detect duplicate task IDs across them)."""
    files = [file for path in paths for file in discover_task_files(path)]
    if not files:
        error_console.print("[red]no task files found[/red]")
        raise typer.Exit(EXIT_INVALID)
    loaded, failed = [], 0
    for file in files:
        try:
            task = load_task(file)
        except TaskValidationError as error:
            render.render_validation_error(console, error)
            failed += 1
            continue
        loaded.append(task)
        render.render_task(console, task, verbose=False)
    for task_id, duplicates in find_duplicate_ids(loaded).items():
        render.render_validation_error(
            console,
            TaskValidationError(
                "task set",
                [ValidationIssue(field="id", reason="duplicate task ID", received=f"{task_id} in "
                                 + ", ".join(str(path) for path in duplicates))],
            ),
        )  # fmt: skip
        failed += 1
    if failed:
        raise typer.Exit(EXIT_INVALID)


@task_app.command("inspect")
def task_inspect(path: Annotated[Path, typer.Argument(help="Task file.")]) -> None:
    """Show the fully resolved task definition, including defaults."""
    try:
        task = load_task(path)
    except TaskValidationError as error:
        render.render_validation_error(console, error)
        raise typer.Exit(EXIT_INVALID) from error
    render.render_task(console, task, verbose=True)


@task_app.command("schema")
def task_schema() -> None:
    """Print the JSON Schema of the task specification."""
    console.print_json(data=TaskSpec.model_json_schema())


# ----------------------------------------------------------------------- run


def _resolve_agent(agent: str | None, agent_command: str | None) -> AgentAdapter:
    if (agent is None) == (agent_command is None):
        raise TaskValidationError(
            "command line",
            [ValidationIssue(field="--agent / --agent-command",
                             reason="specify exactly one: an agent spec file, a built-in agent "
                             "(manual, noop), or --agent-command")],
        )  # fmt: skip
    if agent_command is not None:
        return command_agent_from_string(agent_command)
    environment = AgentEnvironment(environ=os.environ, write=_print_plain, prompt=input)
    return resolve_agent(str(agent), environment)


def _print_plain(text: str) -> None:
    # Task instructions and attach hints are plain text: square brackets must not be
    # interpreted as Rich markup.
    console.print(text, markup=False, highlight=False)


def _raise_cancelled(signum: int, _frame: FrameType | None) -> None:
    raise RunCancelledError(f"received signal {signal.Signals(signum).name}")


@app.command()
def run(
    context: typer.Context,
    task_path: Annotated[Path, typer.Argument(metavar="TASK", help="Task file to run.")],
    agent: Annotated[
        str | None,
        typer.Option("--agent", "-a", help="Agent spec file, or a built-in: manual, noop."),
    ] = None,
    agent_command: Annotated[
        str | None,
        typer.Option(help="Run this command inside the sandbox as the agent (shlex-split)."),
    ] = None,
    keep_workspace: Annotated[
        bool, typer.Option(help="Keep host working directories for debugging.")
    ] = False,
    agent_timeout: Annotated[
        int | None,
        typer.Option(
            min=1,
            max=24 * 60 * 60,
            help="Override the task's agent time budget in seconds (recorded in the manifest).",
        ),
    ] = None,
) -> None:
    """Execute a task with an agent: sandbox, agent, grading, artifacts, cleanup."""
    settings = _state(context).settings
    try:
        task = load_task(task_path)
        adapter = _resolve_agent(agent, agent_command)
    except TaskValidationError as error:
        render.render_validation_error(error_console, error)
        raise typer.Exit(EXIT_INVALID) from error

    secrets = tuple(adapter.secrets.values()) if isinstance(adapter, CommandAgent) else ()
    services = open_services(settings)
    previous_handler = signal.signal(signal.SIGTERM, _raise_cancelled)
    try:
        orchestrator = services.orchestrator(
            create_backend(settings), sinks=[render.ProgressPrinter(console)]
        )
        result = orchestrator.execute(
            RunRequest(
                task=task,
                agent=adapter,
                secrets=secrets,
                keep_workspace=keep_workspace or settings.keep_workspace,
                agent_timeout_seconds=agent_timeout,
            )
        )
        render.render_run_result(console, result, services.artifacts.location(result.run_id))
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        services.close()

    if result.status is RunStatus.CANCELLED:
        raise typer.Exit(EXIT_CANCELLED)
    if result.status is RunStatus.FAILED_VALIDATION:
        raise typer.Exit(EXIT_INVALID)
    if result.status is not RunStatus.COMPLETED:
        raise typer.Exit(EXIT_INFRASTRUCTURE)
    if not result.passed:
        raise typer.Exit(EXIT_GRADE_FAILED)


# ---------------------------------------------------------------------- runs


@runs_app.command("list")
def runs_list(
    context: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 20,
    task_id: Annotated[str | None, typer.Option("--task", help="Filter by task ID.")] = None,
    status: Annotated[RunStatus | None, typer.Option(help="Filter by status.")] = None,
) -> None:
    """List recent runs."""
    services = open_services(_state(context).settings)
    try:
        render.render_runs(
            console, services.repository.list_runs(limit=limit, task_id=task_id, status=status)
        )
    finally:
        services.close()


@runs_app.command("show")
def runs_show(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    events: Annotated[bool, typer.Option(help="Also print the event log.")] = False,
) -> None:
    """Show a run's result, timings and reproducibility manifest."""
    services = open_services(_state(context).settings)
    try:
        stored = services.repository.get_run(run_id)
        if stored is None:
            error_console.print(f"[red]run {run_id} not found[/red]")
            raise typer.Exit(EXIT_INVALID)
        manifest = stored.manifest
        console.print(
            f"[bold]{run_id}[/bold]  task {stored.summary.task_id}  agent {stored.summary.agent}"
        )
        console.print(f"status: {stored.summary.status.value}")
        console.print(
            f"agentenvforge {manifest.agentenvforge_version}, task hash "
            f"{manifest.task_definition_hash[:16]}, "
            f"backend {manifest.backend} {manifest.backend_version}"
        )
        if manifest.source is not None:
            commit = manifest.source.resolved_commit or "-"
            console.print(
                f"source: {manifest.source.source_type} "
                f"tree {manifest.source.tree_hash[:16]} commit {commit}"
            )
        if manifest.image is not None:
            console.print(f"image: {manifest.image.reference} ({manifest.image.image_id[:19]})")
        if stored.result is not None:
            render.render_stage_timings(console, stored.result)
            render.render_run_result(console, stored.result, services.artifacts.location(run_id))
        else:
            console.print(
                "[yellow]run has no final result (still running, or interrupted)[/yellow]"
            )
        if events:
            render.render_events(console, services.repository.list_events(run_id))
    finally:
        services.close()


@runs_app.command("events")
def runs_events(context: typer.Context, run_id: Annotated[str, typer.Argument()]) -> None:
    """Print a run's event log."""
    services = open_services(_state(context).settings)
    try:
        render.render_events(console, services.repository.list_events(run_id))
    finally:
        services.close()


@runs_app.command("artifacts")
def runs_artifacts(context: typer.Context, run_id: Annotated[str, typer.Argument()]) -> None:
    """List a run's artifacts and where they are stored."""
    services = open_services(_state(context).settings)
    try:
        render.render_artifacts(
            console, services.repository.list_artifacts(run_id), services.artifacts.location(run_id)
        )
    finally:
        services.close()


def main() -> None:
    """Console-script entry point."""
    app()
