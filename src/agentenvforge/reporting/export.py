"""Machine-readable experiment outputs.

``runs.csv`` is deliberately flat (one row per planned run, one value per cell, empty cells
for missing values) so it loads directly into pandas, R or a spreadsheet; the canonical full
representation stays in ``summary.json``, the per-run artifacts and the database.
"""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from agentenvforge.analysis.aggregate import RunObservation
from agentenvforge.analysis.models import ExperimentSummary
from agentenvforge.artifacts import ArtifactStore
from agentenvforge.domain.failures import ArtifactError
from agentenvforge.domain.status import RunStatus
from agentenvforge.experiments.manifest import ExperimentManifest
from agentenvforge.persistence.repository import RunRepository
from agentenvforge.reporting.sanitize import PathSanitizer

EVIDENCE_ARTIFACTS = ("result.json", "grade.json", "solution.patch", "agent/transcript.jsonl")
"""Per-run files copied by ``--export-artifacts``: small, and free of host paths except in
error messages, which are sanitised like everything else."""


@dataclass(frozen=True, slots=True)
class RunRow:
    """One row of ``runs.csv``."""

    experiment_id: str
    position: int
    run_id: str | None
    task_id: str
    agent_id: str
    role: str
    model: str | None
    repetition: int
    seed: int
    status: str | None
    outcome: str
    scored: bool
    success: bool | None
    score: float | None
    duration_seconds: float | None
    agent_seconds: float | None
    agent_budget_seconds: int | None
    failure_reason: str | None
    failure_stage: str | None
    started_at: str | None
    finished_at: str | None
    cleanup_ok: bool | None
    prompt_tokens: int | None
    completion_tokens: int | None
    image_id: str | None
    patch_sha256: str | None


def build_rows(
    experiment_id: str,
    manifest: ExperimentManifest,
    observations: list[RunObservation],
    runs: RunRepository,
) -> list[RunRow]:
    """Flatten every planned run into a :class:`RunRow`."""
    agents = {agent.agent_id: agent for agent in manifest.agents}
    rows = []
    for observation in sorted(observations, key=lambda o: o.position):
        agent = agents.get(observation.agent_id)
        result = observation.result
        outcome = observation.outcome
        stored = runs.get_run(observation.run_id) if observation.run_id else None
        metadata = result.agent.metadata if result and result.agent else {}
        agent_stage = (
            next((t for t in result.stage_timings if t.status is RunStatus.RUNNING_AGENT), None)
            if result
            else None
        )
        prompt, completion = metadata.get("prompt_tokens"), metadata.get("completion_tokens")
        model = (agent.provenance.get("model") if agent else None) or (
            metadata.get("model") if isinstance(metadata.get("model"), str) else None
        )
        rows.append(
            RunRow(
                experiment_id=experiment_id,
                position=observation.position,
                run_id=observation.run_id,
                task_id=observation.task_id,
                agent_id=observation.agent_id,
                role=agent.role if agent else "subject",
                model=str(model) if model else None,
                repetition=observation.repetition,
                seed=observation.seed,
                status=result.status.value if result else None,
                outcome=outcome.value,
                scored=outcome.is_scored,
                success=(outcome.value == "passed") if outcome.is_scored else None,
                score=result.score if result else None,
                duration_seconds=result.duration_ms / 1000 if result else None,
                agent_seconds=agent_stage.duration_ms / 1000 if agent_stage else None,
                agent_budget_seconds=agent.agent_timeout_seconds if agent else None,
                failure_reason=result.failure.reason.value if result and result.failure else None,
                failure_stage=result.failure.stage.value if result and result.failure else None,
                started_at=result.created_at.isoformat() if result else None,
                finished_at=result.finished_at.isoformat() if result else None,
                cleanup_ok=result.cleanup.succeeded if result else None,
                prompt_tokens=prompt if isinstance(prompt, int) else None,
                completion_tokens=completion if isinstance(completion, int) else None,
                image_id=(
                    stored.manifest.image.image_id if stored and stored.manifest.image else None
                ),
                patch_sha256=observation.patch_sha256,
            )
        )
    return rows


def write_json(path: Path, payload: object, sanitizer: PathSanitizer) -> Path:
    """Write sanitised, pretty JSON (NaN/Infinity are refused: they are not valid JSON)."""
    path.write_text(
        json.dumps(sanitizer.data(payload), indent=2, allow_nan=False, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def write_runs_csv(path: Path, rows: list[RunRow], sanitizer: PathSanitizer) -> Path:
    """Write ``runs.csv``."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[f.name for f in fields(RunRow)])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: "" if value is None else sanitizer.data(value)
                    for key, value in asdict(row).items()
                }
            )
    return path


def write_failures_csv(path: Path, summary: ExperimentSummary) -> Path:
    """Write the failure breakdown (outcome, reason, stage, count)."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["outcome", "reason", "stage", "count"])
        for failure in summary.failures:
            writer.writerow([failure.outcome, failure.reason, failure.stage or "", failure.count])
    return path


def export_run_evidence(
    destination: Path,
    rows: list[RunRow],
    artifacts: ArtifactStore,
    sanitizer: PathSanitizer,
) -> int:
    """Copy each run's small evidence files to ``destination/<run_id>/``; return the count."""
    copied = 0
    for row in rows:
        if row.run_id is None:
            continue
        for name in EVIDENCE_ARTIFACTS:
            try:
                content = artifacts.get(row.run_id, name)
            except ArtifactError:  # absent artifacts are normal (e.g. no transcript)
                continue
            target = destination / row.run_id / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not name.endswith((".json", ".jsonl")):
                target.write_bytes(content)  # the patch stays byte-exact (workspace paths only)
                copied += 1
                continue
            text = content.decode("utf-8", errors="replace")
            if name.endswith(".jsonl"):
                lines = text.splitlines()
                text = "".join(f"{_sanitized_json(line, sanitizer)}\n" for line in lines)
            else:
                text = _sanitized_json(text, sanitizer, indent=2) + "\n"
            target.write_text(text, encoding="utf-8")
            copied += 1
    return copied


def _sanitized_json(text: str, sanitizer: PathSanitizer, *, indent: int | None = None) -> str:
    """Sanitise a JSON document structurally: inside serialised JSON, Windows paths are
    escaped (C:\\\\Users) and a text replacement of the native spelling would miss them."""
    try:
        data = json.loads(text)
    except ValueError:
        return sanitizer.text(text)  # not JSON after all: best effort on the text
    return json.dumps(sanitizer.data(data), indent=indent, ensure_ascii=False)


def copy_definition(source: Path, destination: Path) -> Path:
    """Keep the experiment file exactly as it was run."""
    shutil.copyfile(source, destination)
    return destination
