"""The human-readable experiment report (``report.md``).

Written for a reader who was not there: what was compared, on what machine, with which
caveats, and what came out, with sample sizes next to every number. It never names a "best"
agent: overlapping intervals at these sample sizes do not support rankings.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agentenvforge.analysis.aggregate import MIN_RUNS_FOR_P95
from agentenvforge.analysis.models import (
    AgentSummary,
    CellSummary,
    Distribution,
    ExperimentSummary,
    Rate,
)
from agentenvforge.analysis.outcomes import Outcome
from agentenvforge.experiments.manifest import ExperimentManifest, HostEnvironment

_YES_NO = {True: "yes", False: "**no**", None: "-"}
_PROVENANCE_KEYS = (
    "model_digest",
    "quantization",
    "parameter_size",
    "temperature",
    "ollama_version",
)
_GIB = 1024**3


def render_report(
    summary: ExperimentSummary,
    manifest: ExperimentManifest,
    *,
    plots: Sequence[Path],
    output_dir: Path,
    files: Sequence[str],
) -> str:
    """Render ``report.md``. ``plots`` and ``files`` are linked relative to ``output_dir``."""
    lines = [
        *_overview(summary, manifest),
        *_validity(summary),
        *(_environment(manifest.environment) if manifest.environment else []),
        *_setup(manifest),
        *_by_agent(summary),
        *_by_task(summary),
        *_run_time(summary),
        *_failures(summary),
    ]
    if plots:
        lines += ["## Plots", ""]
        lines += [f"![{p.stem}]({p.relative_to(output_dir).as_posix()})" for p in plots]
        lines += [""]
    lines += _method()
    lines += ["## Files", "", *[f"- [`{name}`]({name})" for name in files], ""]
    lines += ["Individual runs remain inspectable with `agentenv runs show <run id>`.", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------------------ sections


def _overview(summary: ExperimentSummary, manifest: ExperimentManifest) -> list[str]:
    lines = [f"# Experiment report: {manifest.name or manifest.spec_id}", ""]
    if manifest.description:
        lines += [manifest.description.strip(), ""]
    planned = (
        f"{summary.counts.planned} = {len(manifest.tasks)} tasks x {len(manifest.agents)} "
        f"agents x {manifest.repetitions} repetitions"
    )
    running = f"{_seconds(summary.running_seconds)} s"
    if summary.runs_per_hour:
        running += f" ({summary.runs_per_hour:.1f} runs/hour)"
    definition = f"`{manifest.spec_id}` (hash `{manifest.definition_hash[:16]}`)"
    return lines + _table(
        ["", ""],
        [
            ["Experiment ID", f"`{summary.experiment_id}`"],
            ["Definition", definition],
            ["Status", summary.status + (" (partial)" if summary.partial else "")],
            ["Planned runs", planned],
            ["Workers", str(summary.workers)],
            ["Running time", running],
            ["AgentEnvForge", manifest.agentenvforge_version],
            ["Generated", summary.generated_at.strftime("%Y-%m-%d %H:%M UTC")],
        ],
    )


def _validity(summary: ExperimentSummary) -> list[str]:
    lines = ["## Validity", ""]
    if summary.warnings:
        return [*lines, *[f"- **Warning:** {warning}" for warning in summary.warnings], ""]
    return [
        *lines,
        "No validity warnings: every planned run executed, no run failed for reasons "
        "outside the agents' control, and every reference/baseline check held.",
        "",
    ]


def _environment(environment: HostEnvironment) -> list[str]:
    docker = environment.sandbox_details
    docker_memory = docker.get("memory_bytes", "")
    sandbox = (
        f"{environment.sandbox_backend or '-'} {environment.sandbox_backend_version or ''} "
        f"({docker.get('operating_system', '-')}, {docker.get('cpus', '-')} CPUs, "
        f"{_gib(int(docker_memory)) if docker_memory.isdigit() else '-'})"
    )
    cpu = f"{environment.cpu_model or '-'}, {environment.logical_cpus or '-'} logical CPUs"
    return [
        "## Environment",
        "",
        *_table(
            ["", ""],
            [
                ["OS", f"{environment.os} {environment.os_release} ({environment.architecture})"],
                ["CPU", cpu],
                ["Memory", _gib(environment.memory_bytes) if environment.memory_bytes else "-"],
                ["GPU", "not used (CPU-only execution)"],
                ["Python", environment.python_version],
                ["Sandbox backend", sandbox],
            ],
        ),
    ]


def _setup(manifest: ExperimentManifest) -> list[str]:
    lines = ["## Setup", "", "### Tasks", ""]
    lines += _table(
        ["Task", "Definition", "Image"],
        [
            [f"`{t.task_id}`", f"`{t.path}`", f"`{(t.image_id or '-')[:19]}`"]
            for t in manifest.tasks
        ],
    )
    lines += ["### Agents", ""]
    lines += _table(
        ["Agent", "Role", "Adapter", "Provenance", "Time budget"],
        [
            [
                f"`{a.agent_id}`",
                a.role,
                f"`{a.adapter}`",
                ", ".join(
                    f"{key}={a.provenance[key]}"
                    for key in _PROVENANCE_KEYS
                    if a.provenance.get(key) is not None
                )
                or "-",
                f"{a.agent_timeout_seconds} s" if a.agent_timeout_seconds else "task default",
            ]
            for a in manifest.agents
        ],
    )
    last_seed = manifest.base_seed + manifest.repetitions - 1
    return [
        *lines,
        f"Repetitions use seeds `{manifest.base_seed}` .. `{last_seed}` (offered to agents "
        "that support seeding). Runs were scheduled in interleaved rounds with the agent "
        "order shuffled per block.",
        "",
    ]


def _by_agent(summary: ExperimentSummary) -> list[str]:
    lines = [
        "## Results by agent",
        "",
        "Success = passed / scored runs. Intervals are 95% Wilson score intervals, pooled "
        "over this experiment's tasks, so they describe *these tasks*, not general "
        "capability. Reference and baseline agents validate the tasks and are not compared "
        "with subjects.",
        "",
    ]
    lines += _table(
        [
            "Agent",
            "Role",
            "Model / adapter",
            "Scored",
            "Success [95% CI]",
            "Mean over tasks",
            "pass@k",
            "Timeouts",
            "Excluded",
        ],
        [
            [
                f"`{a.agent_id}`",
                a.role,
                f"`{a.model or a.adapter}`",
                str(a.counts.scored),
                _rate(a.success) + (" (identical repetitions)" if a.deterministic else ""),
                _pct(a.task_mean_success),
                _pass_at_k(a),
                str(a.counts.agent_timeout),
                str(a.counts.unscored),
            ]
            for a in summary.agents
        ],
    )
    tasks = [task.task_id for task in summary.tasks]
    cells = {(c.agent_id, c.task_id): c for c in summary.cells}
    lines += ["## Results by agent and task", "", "Passed / scored runs.", ""]
    lines += _table(
        ["Agent", *[f"`{t}`" for t in tasks]],
        [
            [f"`{a.agent_id}`", *[_cell(cells.get((a.agent_id, t))) for t in tasks]]
            for a in summary.agents
        ],
    )
    return lines


def _by_task(summary: ExperimentSummary) -> list[str]:
    return [
        "## Results by task (subject agents)",
        "",
        *_table(
            ["Task", "Success [95% CI]", "Reference passed every run", "Baseline failed every run"],
            [
                [
                    f"`{t.task_id}`",
                    _rate(t.success),
                    _YES_NO[t.reference_passed_all],
                    _YES_NO[t.baseline_failed_all],
                ]
                for t in summary.tasks
            ],
        ),
    ]


def _run_time(summary: ExperimentSummary) -> list[str]:
    lines = [
        "## Run time",
        "",
        "Seconds over scored runs. `>=`: some runs hit their time budget, so the value is a "
        "lower bound.",
        "",
    ]
    lines += _table(
        ["Agent", "n", "Mean", "Median", "p95", "Min", "Max"],
        [[f"`{a.agent_id}`", *_distribution(a.run_seconds)] for a in summary.agents],
    )
    usage = [(a.agent_id, a.tokens) for a in summary.agents if a.tokens is not None]
    if usage:
        lines += ["### Model usage", ""]
        lines += _table(
            ["Agent", "Prompt tokens", "Completion tokens", "Generation speed"],
            [
                [
                    f"`{agent_id}`",
                    f"{tokens.prompt_tokens:,}",
                    f"{tokens.completion_tokens:,}",
                    f"{tokens.generation_tokens_per_second:.1f} tokens/s"
                    if tokens.generation_tokens_per_second
                    else "-",
                ]
                for agent_id, tokens in usage
            ],
        )
    return lines


def _failures(summary: ExperimentSummary) -> list[str]:
    lines = ["## Failure analysis", ""]
    if summary.failures:
        lines += _table(
            ["Outcome", "Reason", "Stage", "Runs"],
            [[f.outcome, f.reason, f.stage or "-", str(f.count)] for f in summary.failures],
        )
    else:
        lines += ["No run failed.", ""]
    counts = summary.counts
    return lines + _table(
        ["Outcome", "Runs"],
        [[o.value, str(counts.count(o))] for o in Outcome if counts.count(o)],
    )


def _method() -> list[str]:
    return [
        "## Method",
        "",
        "- A run is **scored** once its agent started (unless cancelled). Failures before the "
        "agent started, sandbox start failures, cancellations and unavailable model servers "
        "are excluded from success rates and reported separately.",
        "- Failures after the agent started that prevent grading (e.g. the solution breaks "
        "the test configuration) count as failures (`post_agent_error`).",
        "- Cells whose repetitions produced the same outcome *and* the same patch are marked "
        "identical and count as one observation (no interval, no pass@k): a deterministic "
        "agent looks like this, and so does a sampled one that converges on the same patch.",
        "- pass@k uses the unbiased estimator of Chen et al. (2021), averaged over tasks.",
        f"- p95 is reported only for at least {MIN_RUNS_FOR_P95} runs.",
        "",
    ]


# ------------------------------------------------------------------------------- helpers


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |"

    return [line(headers), "|" + "---|" * len(headers), *[line(row) for row in rows], ""]


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _gib(value: int) -> str:
    return f"{value / _GIB:.1f} GiB"


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _rate(rate: Rate) -> str:
    if rate.estimate is None:
        return "- (no scored runs)"
    text = f"{rate.successes}/{rate.trials} = {rate.estimate:.0%}"
    if rate.ci_low is not None and rate.ci_high is not None:
        text += f" [{rate.ci_low:.0%}, {rate.ci_high:.0%}]"
    return text


def _distribution(dist: Distribution | None) -> list[str]:
    if dist is None:
        return ["0", "-", "-", "-", "-", "-"]
    bound = ">=" if dist.censored else ""
    p95 = bound + _seconds(dist.p95) if dist.p95 is not None else f"- (n<{MIN_RUNS_FOR_P95})"
    return [
        str(dist.count),
        bound + _seconds(dist.mean),
        _seconds(dist.median),
        p95,
        _seconds(dist.minimum),
        bound + _seconds(dist.maximum),
    ]


def _cell(cell: CellSummary | None) -> str:
    if cell is None:
        return "-"
    text = f"{cell.success.successes}/{cell.success.trials}"
    return text + (" (identical)" if cell.identical_repetitions else "")


def _pass_at_k(agent: AgentSummary) -> str:
    if agent.pass_at_k is None:
        return "-"
    if agent.pass_at_k.value is None:
        return f"- ({agent.pass_at_k.note})"
    return f"pass@{agent.pass_at_k.k} = {agent.pass_at_k.value:.2f}"
