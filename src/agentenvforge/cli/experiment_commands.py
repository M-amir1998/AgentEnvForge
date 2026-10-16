"""``agentenv experiment ...`` (definitions) and ``agentenv experiments ...`` (records).

Cancellation: the first Ctrl+C (or SIGTERM, or Ctrl+Break on Windows) stops scheduling and
cancels the active runs, which clean up and end CANCELLED; completed runs are kept and the
results are still published. The handler never raises, because an exception at an
arbitrary point of the main thread could leave runs executing unattended. A third press
exits immediately; sandboxes that were still running are then removed by ``agentenv
cleanup`` (their owner process is gone).
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
from rich.markup import escape

from agentenvforge.agents.base import SupportsPreflight
from agentenvforge.agents.spec import AgentEnvironment
from agentenvforge.cancellation import CancellationToken
from agentenvforge.cli import experiment_render, render
from agentenvforge.cli.common import (
    EXIT_CANCELLED,
    EXIT_INFRASTRUCTURE,
    EXIT_INVALID,
    EXIT_RESULTS_INVALID,
    cli_state,
    console,
    error_console,
    print_plain,
)
from agentenvforge.cli.wiring import Services, open_services
from agentenvforge.config.settings import Settings
from agentenvforge.domain.clock import utc_now
from agentenvforge.domain.failures import AgentEnvForgeError, TaskValidationError
from agentenvforge.execution.experiment_results import effective_status, resummarize
from agentenvforge.experiments.loader import (
    LoadedExperiment,
    concurrency_warnings,
    load_experiment,
)
from agentenvforge.experiments.schema import MAX_WORKERS
from agentenvforge.experiments.status import ExperimentStatus
from agentenvforge.persistence.experiment_repository import StoredExperiment
from agentenvforge.reporting.publish import (
    PublishedResults,
    check_output_directory,
    publish_results,
)
from agentenvforge.reporting.sanitize import PathSanitizer

experiment_app = typer.Typer(
    help="Validate, inspect, run and report batch experiments (tasks x agents x repetitions).",
    no_args_is_help=True,
)
experiments_app = typer.Typer(help="Inspect recorded experiments.", no_args_is_help=True)

ExperimentFile = Annotated[Path, typer.Argument(metavar="EXPERIMENT", help="Experiment file.")]
WorkersOption = Annotated[
    int | None,
    typer.Option(
        min=1,
        max=MAX_WORKERS,
        help="Runs executed at the same time (overrides execution.workers).",
    ),
]
OutputOption = Annotated[
    Path | None,
    typer.Option(
        "--output",
        "-o",
        help="Results directory (overrides output.directory; default: "
        "<AGENTENV_HOME>/experiments/<experiment id>).",
    ),
]
OverwriteOption = Annotated[
    bool, typer.Option(help="Replace the results of another experiment in the output directory.")
]
ExportArtifactsOption = Annotated[
    bool,
    typer.Option(
        help="Also copy each run's evidence (result, grade, patch, transcript) into the results."
    ),
]


# --------------------------------------------------------------------------- helpers


def _load(path: Path) -> LoadedExperiment:
    environment = AgentEnvironment(environ=os.environ, write=print_plain, prompt=input)
    try:
        return load_experiment(path, environment)
    except TaskValidationError as error:
        render.render_validation_error(error_console, error)
        raise typer.Exit(EXIT_INVALID) from error


def _warnings(loaded: LoadedExperiment, workers: int) -> list[str]:
    """The loader's task warnings plus the concurrency warnings for the effective workers."""
    task_warnings = [
        f"{task.spec.id}: {warning}" for task in loaded.tasks for warning in task.warnings
    ]
    return task_warnings + concurrency_warnings(loaded.agents, workers)


def _output_directory(loaded: LoadedExperiment, output: Path | None) -> Path | None:
    if output is not None:
        return output.resolve()
    if loaded.spec.output.directory is not None:
        return (loaded.root / loaded.spec.output.directory).resolve()
    return None


def _default_directory(settings: Settings, experiment_id: str) -> Path:
    return (settings.home / "experiments" / experiment_id).resolve()


def _preflight(loaded: LoadedExperiment) -> bool:
    """Check every agent that supports it; print the provenance or the problem."""
    ready = True
    for agent in loaded.agents:
        adapter = agent.build()
        if not isinstance(adapter, SupportsPreflight):
            continue
        try:
            provenance = adapter.preflight()
        except AgentEnvForgeError as error:
            error_console.print(
                f"[red]agent {escape(agent.agent_id)} is not ready:[/red] {escape(error.message)}"
            )
            ready = False
            continue
        details = ", ".join(f"{k}={v}" for k, v in provenance.items() if v is not None)
        console.print(
            f"  [green]ready[/green] {escape(agent.agent_id)}  [dim]{escape(details)}[/dim]"
        )
    return ready


def _publish(
    services: Services,
    stored: StoredExperiment,
    directory: Path,
    *,
    definition: Path | None,
    export_artifacts: bool,
) -> PublishedResults:
    summary = resummarize(stored, services.repository, generated_at=utc_now())
    root = definition.parent if definition is not None else None
    published = publish_results(
        stored,
        summary,
        runs=services.repository,
        artifacts=services.artifacts,
        directory=directory,
        sanitizer=PathSanitizer(experiment_root=root, agentenv_home=services.settings.home),
        definition_source=definition,
        export_artifacts=export_artifacts,
    )
    services.experiments.store_summary(stored.record.experiment_id, summary, str(directory))
    return published


def _print_published(published: PublishedResults) -> None:
    console.print(f"results: {escape(str(published.directory))}", soft_wrap=True)
    console.print(f"  report: {escape(str(published.directory / 'report.md'))}", soft_wrap=True)
    if not published.plots_written:
        console.print(
            "  [dim]plots skipped: install the plots extra (pip install "
            "'agentenvforge\\[plots]') to get them[/dim]"
        )


class _Interrupts:
    """Signal handling for ``experiment run`` (see the module docstring)."""

    _SIGNALS = tuple(
        getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, name)
    )

    def __init__(self, token: CancellationToken) -> None:
        self._token = token
        self._presses = 0
        self._previous: dict[int, object] = {}

    def __enter__(self) -> _Interrupts:
        for signum in self._SIGNALS:
            self._previous[signum] = signal.signal(signum, self._handle)
        return self

    def __exit__(self, *_: object) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        self._presses += 1
        if self._presses == 1:
            self._token.cancel(f"received {signal.Signals(signum).name}")
            error_console.print(
                "[yellow]stopping: active runs are cancelled and cleaned up, completed runs are "
                "kept (press Ctrl+C twice more to exit immediately)[/yellow]"
            )
        elif self._presses == 2:
            error_console.print(
                "[yellow]still cleaning up active runs; press Ctrl+C once more to exit now "
                "(then run `agentenv cleanup` to remove leftover sandboxes)[/yellow]"
            )
        else:
            raise SystemExit(EXIT_CANCELLED)


# --------------------------------------------------------------------- experiment ...


@experiment_app.command("validate")
def experiment_validate(
    path: ExperimentFile,
    preflight: Annotated[
        bool,
        typer.Option(help="Also check that every agent is ready (e.g. model server and model)."),
    ] = False,
) -> None:
    """Validate an experiment file and everything it references."""
    loaded = _load(path)
    workers = loaded.spec.execution.workers
    experiment_render.render_plan(
        console,
        loaded,
        workers=workers,
        warnings=_warnings(loaded, workers),
        output=_output_directory(loaded, None) or "<AGENTENV_HOME>/experiments/<experiment id>",
        verbose=False,
    )
    if preflight and not _preflight(loaded):
        raise typer.Exit(EXIT_INFRASTRUCTURE)
    console.print("[green]valid[/green]")


@experiment_app.command("inspect")
def experiment_inspect(path: ExperimentFile, workers: WorkersOption = None) -> None:
    """Show the resolved experiment: tasks, agents, hashes, warnings and the run order."""
    loaded = _load(path)
    effective = workers or loaded.spec.execution.workers
    experiment_render.render_plan(
        console,
        loaded,
        workers=effective,
        warnings=_warnings(loaded, effective),
        output=_output_directory(loaded, None) or "<AGENTENV_HOME>/experiments/<experiment id>",
        verbose=True,
    )


@experiment_app.command("run")
def experiment_run(
    context: typer.Context,
    path: ExperimentFile,
    workers: WorkersOption = None,
    output: OutputOption = None,
    overwrite: OverwriteOption = False,
    export_artifacts: ExportArtifactsOption = False,
) -> None:
    """Execute every planned run with bounded concurrency, then write the results."""
    settings = cli_state(context).settings
    loaded = _load(path)
    effective = workers or loaded.spec.execution.workers
    directory = _output_directory(loaded, output)
    try:
        if directory is not None:
            check_output_directory(directory, experiment_id=None, overwrite=overwrite)
    except AgentEnvForgeError as error:
        error_console.print(f"[red]{escape(error.message)}[/red]")
        raise typer.Exit(EXIT_INVALID) from error
    experiment_render.render_plan(
        console,
        loaded,
        workers=effective,
        warnings=_warnings(loaded, effective),
        output=directory or "<AGENTENV_HOME>/experiments/<experiment id>",
        verbose=False,
    )

    services = open_services(settings)
    try:
        token = CancellationToken()
        with _Interrupts(token):
            outcome = services.experiment_executor().execute(
                loaded,
                workers=effective,
                cancellation=token,
                observer=experiment_render.ExperimentProgressPrinter(console),
                output_dir=str(directory) if directory else None,
            )
        stored = services.experiments.get_experiment(outcome.experiment_id)
        if stored is None:  # pragma: no cover - the executor created it
            raise RuntimeError(f"experiment {outcome.experiment_id} is missing")
        experiment_render.render_summary(console, outcome.summary)
        try:
            published = _publish(
                services,
                stored,
                directory or _default_directory(settings, outcome.experiment_id),
                definition=loaded.path,
                export_artifacts=export_artifacts,
            )
        except AgentEnvForgeError as error:
            error_console.print(
                f"[red]{escape(error.message)}[/red]\nThe results are stored; write them with "
                f"`agentenv experiment report {outcome.experiment_id} --output DIR`."
            )
            raise typer.Exit(EXIT_INFRASTRUCTURE) from error
        _print_published(published)
    finally:
        services.close()

    if outcome.status is ExperimentStatus.CANCELLED:
        raise typer.Exit(EXIT_CANCELLED)
    if outcome.status is ExperimentStatus.FAILED:
        error_console.print(f"[red]experiment failed:[/red] {escape(outcome.failure or '')}")
        raise typer.Exit(EXIT_INFRASTRUCTURE)
    if not outcome.summary.results_valid:
        raise typer.Exit(EXIT_RESULTS_INVALID)


@experiment_app.command("report")
def experiment_report(
    context: typer.Context,
    experiment_id: Annotated[str, typer.Argument(help="Experiment ID.")],
    output: OutputOption = None,
    overwrite: OverwriteOption = False,
    export_artifacts: ExportArtifactsOption = False,
) -> None:
    """(Re)generate an experiment's results directory from the database."""
    settings = cli_state(context).settings
    services = open_services(settings)
    try:
        stored = _get(services, experiment_id)
        record = stored.record
        if output is not None:
            directory = output.resolve()
        elif record.output_dir:
            directory = Path(record.output_dir)
        else:
            directory = _default_directory(settings, experiment_id)
        definition = Path(record.definition_path) if record.definition_path else None
        try:
            check_output_directory(directory, experiment_id=experiment_id, overwrite=overwrite)
            published = _publish(
                services,
                stored,
                directory,
                definition=definition,
                export_artifacts=export_artifacts,
            )
        except AgentEnvForgeError as error:
            error_console.print(f"[red]{escape(error.message)}[/red]")
            raise typer.Exit(EXIT_INVALID) from error
        _print_published(published)
    finally:
        services.close()


# -------------------------------------------------------------------- experiments ...


def _get(services: Services, experiment_id: str) -> StoredExperiment:
    stored = services.experiments.get_experiment(experiment_id)
    if stored is None:
        error_console.print(f"[red]experiment {escape(experiment_id)} not found[/red]")
        raise typer.Exit(EXIT_INVALID)
    return stored


@experiments_app.command("list")
def experiments_list(
    context: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 20,
) -> None:
    """List recent experiments."""
    services = open_services(cli_state(context).settings)
    try:
        records = services.experiments.list_experiments(limit=limit)
        experiment_render.render_experiment_list(
            console, records, [effective_status(record) for record in records]
        )
    finally:
        services.close()


@experiments_app.command("show")
def experiments_show(
    context: typer.Context,
    experiment_id: Annotated[str, typer.Argument(help="Experiment ID.")],
) -> None:
    """Show an experiment's state, provenance and (current) results."""
    services = open_services(cli_state(context).settings)
    try:
        stored = _get(services, experiment_id)
        summary = resummarize(stored, services.repository, generated_at=utc_now())
        experiment_render.render_experiment_detail(console, stored, summary)
    finally:
        services.close()
