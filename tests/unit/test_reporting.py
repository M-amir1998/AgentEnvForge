"""Experiment result files: content, sanitisation, regeneration and plots."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from agentenvforge.domain.clock import utc_now
from agentenvforge.execution.experiment_results import summarize_experiment
from agentenvforge.persistence.experiment_repository import StoredExperiment
from agentenvforge.reporting.export import RunRow
from agentenvforge.reporting.plots import plotting_available
from agentenvforge.reporting.publish import (
    OWNED_FILES,
    PublishedResults,
    ResultsDirectoryError,
    check_output_directory,
    publish_results,
)
from agentenvforge.reporting.sanitize import PathSanitizer
from tests.unit.test_experiment_execution import CANCEL_AFTER, GAUGE, Harness


@pytest.fixture
def executed(tmp_path: Path) -> tuple[Harness, StoredExperiment, Path]:
    GAUGE.active = GAUGE.peak = 0
    CANCEL_AFTER.clear()
    harness = Harness(tmp_path)
    definition = harness.experiment(["solver", "noop", "crasher"], 2, 2)
    outcome, _ = harness.execute(definition, workers=2)
    stored = harness.experiments.get_experiment(outcome.experiment_id)
    assert stored is not None
    return harness, stored, definition


def publish(
    harness: Harness,
    stored: StoredExperiment,
    directory: Path,
    *,
    definition: Path | None = None,
    export_artifacts: bool = False,
) -> PublishedResults:
    summary = summarize_experiment(stored, harness.runs, generated_at=utc_now())
    return publish_results(
        stored,
        summary,
        runs=harness.runs,
        artifacts=harness.artifacts,
        directory=directory,
        sanitizer=PathSanitizer(experiment_root=harness.tmp, agentenv_home=harness.tmp / "home"),
        definition_source=definition,
        export_artifacts=export_artifacts,
    )


def all_text(directory: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in directory.rglob("*")
        if path.is_file() and path.suffix != ".png"
    )


def test_every_result_file_is_written_and_consistent(
    executed: tuple[Harness, StoredExperiment, Path],
) -> None:
    harness, stored, definition = executed
    output = harness.tmp / "results"
    published = publish(harness, stored, output, definition=definition)

    for name in OWNED_FILES:
        assert (output / name).is_file(), name
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["experiment_id"] == stored.record.experiment_id
    assert summary["counts"]["passed"] == 2 and summary["counts"]["agent_error"] == 2

    with (output / "runs.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == [field.name for field in fields(RunRow)]
    assert len(rows) == 6 and [int(row["position"]) for row in rows] == list(range(6))
    assert {row["outcome"] for row in rows if row["agent_id"] == "crasher"} == {"agent_error"}
    assert {row["success"] for row in rows if row["agent_id"] == "solver"} == {"True"}
    assert all(row["run_id"].startswith("run_") for row in rows)

    with (output / "failures.csv").open(encoding="utf-8", newline="") as handle:
        failures = list(csv.DictReader(handle))
    assert {(f["outcome"], f["count"]) for f in failures} >= {("agent_error", "2")}

    report = (output / "report.md").read_text(encoding="utf-8")
    for heading in ("## Validity", "## Results by agent", "## Run time", "## Failure analysis"):
        assert heading in report
    assert "`solver`" in report and "2/2" in report
    assert (output / "experiment.yaml").read_text(encoding="utf-8") == definition.read_text(
        encoding="utf-8"
    )
    assert json.loads((output / "environment.json").read_text(encoding="utf-8"))["os"]
    assert published.files[: len(OWNED_FILES)] == OWNED_FILES


def test_result_files_contain_no_host_paths(
    executed: tuple[Harness, StoredExperiment, Path],
) -> None:
    harness, stored, _ = executed
    # Agents' own artifacts can mention host paths, e.g. a CLI reporting its working
    # directory. Inside JSON, Windows paths are escaped (C:\\Users\\...).
    run_id = str(stored.entries[0].run_id)
    leaky = {"type": "system", "cwd": str(harness.tmp / "scratch"), "home": str(Path.home())}
    harness.artifacts.put(run_id, "agent/transcript.jsonl", (json.dumps(leaky) + "\n").encode())
    output = harness.tmp / "results"
    publish(harness, stored, output, export_artifacts=True)
    text = all_text(output)
    for path in (str(harness.tmp), harness.tmp.as_posix(), str(Path.home())):
        for secret in (path, json.dumps(path)[1:-1]):  # raw and JSON-escaped spellings
            if sys.platform == "win32":
                assert secret.lower() not in text.lower(), secret
            else:
                assert secret not in text, secret
    exported = output / "artifacts" / run_id / "agent" / "transcript.jsonl"
    assert json.loads(exported.read_text(encoding="utf-8"))["home"] == "~"


def test_republishing_replaces_own_files_and_keeps_unrelated_ones(
    executed: tuple[Harness, StoredExperiment, Path],
) -> None:
    harness, stored, _ = executed
    output = harness.tmp / "results"
    publish(harness, stored, output, export_artifacts=True)
    (output / "notes.txt").write_text("mine", encoding="utf-8")
    assert (output / "artifacts").is_dir()

    published = publish(harness, stored, output)  # regeneration, without artifacts this time
    assert (output / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert not (output / "artifacts").exists() and published.artifacts_copied == 0
    assert (output / "experiment.yaml").read_text(encoding="utf-8").startswith("# Reconstructed")


def test_exported_evidence_contains_each_runs_result(
    executed: tuple[Harness, StoredExperiment, Path],
) -> None:
    harness, stored, _ = executed
    output = harness.tmp / "results"
    published = publish(harness, stored, output, export_artifacts=True)
    run_ids = {entry.run_id for entry in stored.entries}
    assert {path.name for path in (output / "artifacts").iterdir()} == run_ids
    assert published.artifacts_copied >= len(run_ids)
    for run_id in run_ids:
        assert (output / "artifacts" / str(run_id) / "result.json").is_file()


def test_output_directory_of_another_experiment_needs_overwrite(
    executed: tuple[Harness, StoredExperiment, Path],
) -> None:
    harness, stored, _ = executed
    output = harness.tmp / "results"
    check_output_directory(output, experiment_id=None, overwrite=False)  # absent: fine
    publish(harness, stored, output)
    with pytest.raises(ResultsDirectoryError, match="--overwrite"):
        check_output_directory(output, experiment_id=None, overwrite=False)
    check_output_directory(output, experiment_id=stored.record.experiment_id, overwrite=False)
    check_output_directory(output, experiment_id=None, overwrite=True)

    blocker = harness.tmp / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ResultsDirectoryError, match="not a directory"):
        check_output_directory(blocker, experiment_id=None, overwrite=True)


@pytest.mark.skipif(not plotting_available(), reason="matplotlib is not installed")
def test_plots_are_valid_png_files_linked_from_the_report(
    executed: tuple[Harness, StoredExperiment, Path],
) -> None:
    harness, stored, _ = executed
    output = harness.tmp / "results"
    published = publish(harness, stored, output)
    plots = sorted((output / "plots").glob("*.png"))
    assert published.plots_written
    assert [p.name for p in plots] == [
        "outcomes_by_agent.png",
        "run_time_by_agent.png",
        "success_by_agent.png",
        "success_matrix.png",
    ]
    report = (output / "report.md").read_text(encoding="utf-8")
    for plot in plots:
        assert plot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert f"(plots/{plot.name})" in report


def test_sanitizer_rewrites_nested_paths_most_specific_first(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / "experiments" / "e1"
    sanitizer = PathSanitizer(experiment_root=root, agentenv_home=home)
    data = {
        "a": f"{root.resolve()}{'/' if sys.platform != 'win32' else chr(92)}x.yaml",
        "b": [f"see {home.resolve().as_posix()}/runs", 3, None],
        "c": (f"{Path.home()}/elsewhere",),
    }
    cleaned = sanitizer.data(data)
    assert isinstance(cleaned, dict)
    assert str(cleaned["a"]).startswith(".") and "x.yaml" in str(cleaned["a"])
    assert cleaned["b"] == ["see $AGENTENV_HOME/runs", 3, None]
    assert cleaned["c"] == ["~/elsewhere"]
    assert sanitizer.text("benchmarks and words") == "benchmarks and words"


def test_original_definition_is_copied_only_while_it_is_unchanged(
    executed: tuple[Harness, StoredExperiment, Path],
) -> None:
    harness, stored, definition = executed
    original = definition.read_text(encoding="utf-8")
    output = harness.tmp / "results"
    publish(harness, stored, output, definition=definition)
    assert (output / "experiment.yaml").read_text(encoding="utf-8") == original

    definition.write_text(original.replace("repetitions: 2", "repetitions: 3"), encoding="utf-8")
    publish(harness, stored, output, definition=definition)  # edited since: keep the copy
    assert (output / "experiment.yaml").read_text(encoding="utf-8") == original

    fresh = harness.tmp / "fresh"
    publish(harness, stored, fresh, definition=definition)  # no copy to fall back on
    reconstructed = (fresh / "experiment.yaml").read_text(encoding="utf-8")
    assert reconstructed.startswith("# Reconstructed") and "repetitions: 2" in reconstructed
