"""Writing an experiment's results directory.

``agentenv experiment run`` publishes when the experiment finishes, and ``agentenv
experiment report`` re-publishes from the database at any time, so the files are always a
pure function of the persisted facts::

    <output>/
      experiment.yaml    the definition as it was run
      manifest.json      reproducibility snapshot (versions, hashes, agent provenance, images)
      environment.json   hardware and software of the machine (nothing identifying)
      summary.json       every computed statistic (ExperimentSummary)
      runs.csv           one row per planned run
      failures.csv       failure breakdown (outcome, reason, stage)
      report.md          the human-readable report
      plots/*.png        charts (only when matplotlib is installed)
      artifacts/<run>/   per-run evidence (only with --export-artifacts)

Every file is sanitised (see :mod:`agentenvforge.reporting.sanitize`) so the directory can
be shared or committed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentenvforge.analysis.models import ExperimentSummary
from agentenvforge.artifacts import ArtifactStore
from agentenvforge.domain.failures import AgentEnvForgeError
from agentenvforge.execution.experiment_results import observations
from agentenvforge.experiments.schema import ExperimentSpec
from agentenvforge.persistence.experiment_repository import StoredExperiment
from agentenvforge.persistence.repository import RunRepository
from agentenvforge.reporting.export import (
    build_rows,
    export_run_evidence,
    write_failures_csv,
    write_json,
    write_runs_csv,
)
from agentenvforge.reporting.markdown import render_report
from agentenvforge.reporting.plots import render_plots
from agentenvforge.reporting.sanitize import PathSanitizer

DEFINITION_FILE = "experiment.yaml"
SUMMARY_FILE = "summary.json"
OWNED_FILES = (
    DEFINITION_FILE,
    "manifest.json",
    "environment.json",
    SUMMARY_FILE,
    "runs.csv",
    "failures.csv",
    "report.md",
)
OWNED_DIRECTORIES = ("plots", "artifacts")


class ResultsDirectoryError(AgentEnvForgeError):
    """The output directory holds another experiment's results (and overwriting was not
    requested), or it cannot be written."""


@dataclass(frozen=True, slots=True)
class PublishedResults:
    """What :func:`publish_results` wrote."""

    directory: Path
    files: tuple[str, ...]
    plots_written: bool
    """False when matplotlib is not installed (everything else is still written)."""
    artifacts_copied: int


def check_output_directory(directory: Path, *, experiment_id: str | None, overwrite: bool) -> None:
    """Refuse to mix results: a directory holding another experiment's ``summary.json`` is
    only reused with ``overwrite``. Checked before an experiment starts, not after it ran."""
    if directory.exists() and not directory.is_dir():
        raise ResultsDirectoryError(f"output path {directory} exists and is not a directory")
    previous = _previous_experiment(directory)
    if previous is not None and previous != experiment_id and not overwrite:
        raise ResultsDirectoryError(
            f"{directory} already holds the results of experiment {previous}; "
            "use --overwrite to replace them or choose another --output"
        )


def publish_results(
    stored: StoredExperiment,
    summary: ExperimentSummary,
    *,
    runs: RunRepository,
    artifacts: ArtifactStore,
    directory: Path,
    sanitizer: PathSanitizer,
    definition_source: Path | None = None,
    export_artifacts: bool = False,
) -> PublishedResults:
    """Write the results directory for ``stored`` (replacing files of a previous publish)."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        manifest = stored.manifest
        definition = _definition_text(directory, stored, definition_source)
        _remove_previous_outputs(directory)
        observed = observations(stored, runs)
        rows = build_rows(stored.record.experiment_id, manifest, observed, runs)

        (directory / DEFINITION_FILE).write_text(sanitizer.text(definition), encoding="utf-8")
        write_json(directory / "manifest.json", manifest.model_dump(mode="json"), sanitizer)
        environment = manifest.environment.model_dump(mode="json") if manifest.environment else {}
        write_json(directory / "environment.json", environment, sanitizer)
        write_json(directory / SUMMARY_FILE, summary.model_dump(mode="json"), sanitizer)
        write_runs_csv(directory / "runs.csv", rows, sanitizer)
        write_failures_csv(directory / "failures.csv", summary)

        copied = 0
        if export_artifacts:
            copied = export_run_evidence(directory / "artifacts", rows, artifacts, sanitizer)
        plots = render_plots(summary, observed, directory / "plots")

        files = [*OWNED_FILES] + (["artifacts/"] if copied else [])
        report = render_report(summary, manifest, plots=plots, output_dir=directory, files=files)
        (directory / "report.md").write_text(sanitizer.text(report), encoding="utf-8")
    except OSError as error:
        raise ResultsDirectoryError(f"could not write results to {directory}: {error}") from error
    return PublishedResults(
        directory=directory,
        files=tuple(files),
        plots_written=bool(plots),
        artifacts_copied=copied,
    )


def _previous_experiment(directory: Path) -> str | None:
    try:
        data = json.loads((directory / SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    experiment_id = data.get("experiment_id") if isinstance(data, dict) else None
    return experiment_id if isinstance(experiment_id, str) else None


def _remove_previous_outputs(directory: Path) -> None:
    """Remove only what a previous publish wrote, never unrelated files."""
    for name in OWNED_FILES:
        (directory / name).unlink(missing_ok=True)
    for name in OWNED_DIRECTORIES:
        if (directory / name).is_dir():
            shutil.rmtree(directory / name)


def _definition_text(directory: Path, stored: StoredExperiment, source: Path | None) -> str:
    """The definition exactly as it was run, comments included, from the first of:

    1. the original file, if it still defines the same experiment (it may have been edited
       since; then it is not used),
    2. the copy a previous publish of this experiment wrote,
    3. a reconstruction from the definition stored in the database.
    """
    manifest = stored.manifest
    if source is not None:
        try:
            text = source.read_text(encoding="utf-8")
            document = yaml.safe_load(text)
            if (
                ExperimentSpec.model_validate(document).model_dump(mode="json")
                == manifest.definition
            ):
                return text
        except (OSError, yaml.YAMLError, ValueError):
            pass  # unreadable or changed: fall through to the other sources
    previous_copy = directory / DEFINITION_FILE
    if _previous_experiment(directory) == stored.record.experiment_id and previous_copy.is_file():
        return previous_copy.read_text(encoding="utf-8")
    header = "# Reconstructed from the database (the original file was not available).\n"
    return header + yaml.safe_dump(manifest.definition, sort_keys=False, allow_unicode=True)
