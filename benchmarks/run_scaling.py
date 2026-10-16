"""Scaling benchmark: the infrastructure experiment at several worker counts.

    python benchmarks/run_scaling.py [--workers 1 2 4] [--trials 3] [--output DIR]

Each trial is one complete ``agentenv experiment run`` (a separate process, as a user would
run it). Trials are interleaved (trial 1 at every worker count, then trial 2, ...) with the
worker order shuffled per trial, so drift on the machine (thermal throttling, background
load, Docker caches) spreads over all worker counts instead of biasing one.

Measured per trial: wall time of the whole command, running time (the experiment's RUNNING
phase: first run started to last run finished, excluding image preparation), per-run
durations, whether every run's cleanup succeeded, whether results were valid (oracle passed
and noop failed every run), and containers left behind afterwards.

Reported per worker count: the median (and range) over trials of the running time and
throughput, and of the speedup *within each trial* (that trial's 1-worker running time
divided by its N-worker running time). Each trial runs every worker count back to back, so
the within-trial ratio cancels drift between trials, which a ratio of medians would not.
Parallel efficiency = speedup / N. The median per-run duration grows with contention.

Results go to ``--output`` (default ``benchmarks/results/scaling``): ``scaling.json`` (every
trial and the aggregates), ``scaling.md`` and ``scaling.png``. ``--render-only`` recomputes
the aggregates and rewrites the Markdown and the plot from an existing ``scaling.json``.
Runs are recorded in a temporary AGENTENV_HOME unless ``--home`` is given, so the user's own
state is untouched.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE / "infrastructure" / "experiment.yaml"
RUN_ID_LABEL = "io.agentenvforge.run-id"


@dataclass(frozen=True)
class Trial:
    trial: int
    workers: int
    exit_code: int
    wall_seconds: float
    running_seconds: float | None
    planned: int
    passed: int
    failed: int
    excluded: int
    results_valid: bool
    cleanup_ok: bool
    leftover_containers: int | None
    run_seconds_median: float | None
    run_seconds_max: float | None


def leftovers(run_ids: list[str]) -> int | None:
    try:
        import docker  # noqa: PLC0415 - only needed for the check
        from docker.errors import DockerException  # noqa: PLC0415
    except ImportError:
        return None
    try:
        client = docker.from_env()
        return sum(
            len(client.containers.list(all=True, filters={"label": f"{RUN_ID_LABEL}={run_id}"}))
            for run_id in run_ids
        )
    except DockerException:
        return None


def run_trial(
    experiment: Path, trial: int, workers: int, scratch: Path, env: dict[str, str]
) -> Trial:
    output = scratch / f"w{workers}-t{trial}"
    command = [
        sys.executable,
        "-m",
        "agentenvforge",
        "experiment",
        "run",
        str(experiment),
        "--workers",
        str(workers),
        "--output",
        str(output),
    ]
    started = time.perf_counter()
    completed = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    wall = time.perf_counter() - started
    if not (output / "summary.json").is_file():
        sys.stderr.write(completed.stdout + completed.stderr)
        raise SystemExit(f"trial {trial} (workers={workers}) produced no results")
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    with (output / "runs.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    durations = [float(row["duration_seconds"]) for row in rows if row["duration_seconds"]]
    counts = summary["counts"]
    scored = sum(
        counts[k]
        for k in (
            "passed",
            "failed",
            "agent_timeout",
            "solution_timeout",
            "agent_error",
            "post_agent_error",
        )
    )
    return Trial(
        trial=trial,
        workers=workers,
        exit_code=completed.returncode,
        wall_seconds=round(wall, 2),
        running_seconds=summary["running_seconds"],
        planned=len(rows),
        passed=counts["passed"],
        failed=scored - counts["passed"],
        excluded=len(rows) - scored,
        results_valid=summary["results_valid"],
        cleanup_ok=all(row["cleanup_ok"] == "True" for row in rows),
        leftover_containers=leftovers([row["run_id"] for row in rows if row["run_id"]]),
        run_seconds_median=statistics.median(durations) if durations else None,
        run_seconds_max=max(durations) if durations else None,
    )


def within_trial_speedups(trials: list[Trial]) -> dict[tuple[int, int], float]:
    """(trial, workers) -> running time at the fewest workers / running time at ``workers``,
    both from the same trial."""
    fewest = min(t.workers for t in trials)
    base = {t.trial: t.running_seconds for t in trials if t.workers == fewest}
    return {
        (t.trial, t.workers): base_seconds / t.running_seconds
        for t in trials
        if t.running_seconds and (base_seconds := base.get(t.trial))
    }


def aggregate(trials: list[Trial]) -> list[dict[str, Any]]:
    by_workers: dict[int, list[Trial]] = {}
    for trial in trials:
        by_workers.setdefault(trial.workers, []).append(trial)
    ratios = within_trial_speedups(trials)
    rows = []
    for workers, group in sorted(by_workers.items()):
        running = statistics.median(t.running_seconds for t in group if t.running_seconds)
        speedups = [ratios[(t.trial, workers)] for t in group if (t.trial, workers) in ratios]
        speedup = statistics.median(speedups)
        rows.append(
            {
                "workers": workers,
                "trials": len(group),
                "running_seconds_median": round(running, 1),
                "running_seconds_min": round(min(t.running_seconds or 0 for t in group), 1),
                "running_seconds_max": round(max(t.running_seconds or 0 for t in group), 1),
                "wall_seconds_median": round(statistics.median(t.wall_seconds for t in group), 1),
                "runs_per_hour_median": round(group[0].planned / running * 3600),
                "speedup": round(speedup, 2),
                "speedup_min": round(min(speedups), 2),
                "speedup_max": round(max(speedups), 2),
                "efficiency": round(speedup / workers, 2),
                "run_seconds_median": round(
                    statistics.median(t.run_seconds_median or 0 for t in group), 2
                ),
                "all_results_valid": all(t.results_valid for t in group),
                "all_cleanup_ok": all(t.cleanup_ok for t in group),
                "leftover_containers": sum(t.leftover_containers or 0 for t in group),
            }
        )
    return rows


def write_markdown(path: Path, result: dict[str, Any]) -> None:
    env = result["environment"]
    lines = [
        "# Scaling benchmark: batch execution overhead",
        "",
        f"Measured {result['date']} with AgentEnvForge {result['agentenvforge_version']}: "
        f"{result['planned_runs']} runs per trial (`{result['experiment']}`), "
        f"{result['trials_per_setting']} interleaved trial(s) per worker count.",
        "",
        f"Machine: {env.get('cpu_model', '-')}, {env.get('logical_cpus', '-')} logical CPUs, "
        f"{env.get('memory_gib', '-')} GiB RAM, {env.get('os', '-')}; Docker "
        f"{env.get('docker_version', '-')} ({env.get('docker_os', '-')}, "
        f"{env.get('docker_cpus', '-')} CPUs). CPU only.",
        "",
        "| Workers | Running time (median, min-max) | Throughput | Speedup (median, min-max) "
        "| Efficiency | Median run | Valid | Cleanup | Leftovers |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in result["aggregate"]:
        lines.append(
            f"| {row['workers']} | {row['running_seconds_median']} s "
            f"({row['running_seconds_min']}-{row['running_seconds_max']}) "
            f"| {row['runs_per_hour_median']} runs/h | {row['speedup']}x "
            f"({row['speedup_min']}-{row['speedup_max']}) | "
            f"{row['efficiency']:.0%} | {row['run_seconds_median']} s | "
            f"{'yes' if row['all_results_valid'] else 'NO'} | "
            f"{'ok' if row['all_cleanup_ok'] else 'FAILED'} | {row['leftover_containers']} |"
        )
    lines += [
        "",
        "![Speedup by workers](scaling.png)",
        "",
        "Every trial, in execution order:",
        "",
        "| Trial | Workers | Running time | Wall time | Speedup in trial | Median run | Valid |",
        "|---|---|---|---|---|---|---|",
    ]
    ratios = result["within_trial_speedups"]
    for trial in result["trials"]:
        ratio = ratios.get(f"{trial['trial']}/{trial['workers']}")
        lines.append(
            f"| {trial['trial']} | {trial['workers']} | {trial['running_seconds']:.1f} s | "
            f"{trial['wall_seconds']:.1f} s | {f'{ratio:.2f}x' if ratio else '-'} | "
            f"{trial['run_seconds_median']:.2f} s | {'yes' if trial['results_valid'] else 'NO'} |"
        )
    lines += [
        "",
        "- *Running time*: first run started to last run finished (image preparation excluded).",
        "- *Speedup*: per trial, the 1-worker running time divided by the N-worker running "
        "time of the same trial; the table shows the median and range over trials. Within a "
        "trial the worker counts ran back to back, so drift between trials (see the trial "
        "table) cancels out.",
        "- *Median run*: median duration of a single run; it grows with N because concurrent "
        "runs share the CPU, disk and the Docker daemon.",
        "- *Valid*: every oracle run passed and every noop run failed, at every worker count.",
        "- The agents are deterministic and near-instant, so this measures AgentEnvForge and "
        "Docker overhead, not agent performance. Results depend on the machine.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_plot(path: Path, trials: list[Trial], rows: list[dict[str, Any]]) -> bool:
    try:
        from matplotlib.figure import Figure  # noqa: PLC0415 - optional dependency
    except ImportError:
        return False
    ratios = within_trial_speedups(trials)
    figure = Figure(figsize=(6.0, 3.6), dpi=150, facecolor="#fcfcfb")
    axes = figure.add_subplot()
    axes.set_facecolor("#fcfcfb")
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    workers = [r["workers"] for r in rows]
    axes.plot(workers, workers, color="#9a9993", linewidth=1, linestyle="--")
    axes.text(workers[-1], workers[-1], " ideal", va="center", fontsize=8, color="#52514e")
    axes.scatter(
        [workers for _, workers in ratios],
        list(ratios.values()),
        s=18,
        color="#2a78d6",
        alpha=0.5,
        edgecolors="#fcfcfb",
        linewidths=0.6,
        zorder=3,
    )
    axes.plot(workers, [r["speedup"] for r in rows], color="#2a78d6", linewidth=2, zorder=4)
    for row in rows:
        axes.annotate(
            f"{row['speedup']}x",
            (row["workers"], row["speedup"]),
            textcoords="offset points",
            xytext=(6, -12),
            fontsize=8,
            color="#0b0b0b",
        )
    axes.set_xticks(workers)
    axes.set_ylim(0, max(workers) * 1.08)
    axes.set_xlabel("workers (runs executing at once)", fontsize=8, color="#52514e")
    axes.set_ylabel("speedup vs 1 worker", fontsize=8, color="#52514e")
    axes.tick_params(colors="#52514e", labelsize=8)
    axes.grid(axis="y", color="#e4e3df", linewidth=0.8)
    axes.set_axisbelow(True)
    axes.set_title(
        "Speedup within each trial (dots) and its median (line)",
        loc="left",
        fontsize=9,
        color="#0b0b0b",
    )
    figure.tight_layout()
    figure.savefig(path, facecolor="#fcfcfb")
    return True


def host_environment(scratch: Path) -> dict[str, Any]:
    trial_dir = next(scratch.iterdir())
    env = json.loads((trial_dir / "environment.json").read_text(encoding="utf-8"))
    manifest = json.loads((trial_dir / "manifest.json").read_text(encoding="utf-8"))
    details = env.get("sandbox_details", {})
    memory = env.get("memory_bytes")
    return {
        "os": f"{env.get('os')} {env.get('os_release')}",
        "cpu_model": env.get("cpu_model"),
        "logical_cpus": env.get("logical_cpus"),
        "memory_gib": round(memory / 1024**3, 1) if memory else None,
        "python": env.get("python_version"),
        "docker_version": env.get("sandbox_backend_version"),
        "docker_os": details.get("operating_system"),
        "docker_cpus": details.get("cpus"),
        "agentenvforge_version": manifest.get("agentenvforge_version"),
    }


def publish(output: Path, document: dict[str, Any], trials: list[Trial]) -> None:
    """(Re)compute the aggregates and write scaling.json, scaling.md and scaling.png."""
    rows = aggregate(trials)
    ratios = within_trial_speedups(trials)
    document = {
        **document,
        "aggregate": rows,
        "within_trial_speedups": {f"{t}/{w}": round(r, 3) for (t, w), r in ratios.items()},
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "scaling.json").write_text(json.dumps(document, indent=2) + "\n", "utf-8")
    write_markdown(output / "scaling.md", document)
    write_plot(output / "scaling.png", trials, rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--experiment", type=Path, default=EXPERIMENT)
    parser.add_argument("--output", type=Path, default=HERE / "results" / "scaling")
    parser.add_argument("--home", type=Path, default=None, help="AGENTENV_HOME for the runs.")
    parser.add_argument("--seed", type=int, default=0, help="Seed of the worker-order shuffle.")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Recompute the aggregates and rewrite scaling.md/.png from --output/scaling.json.",
    )
    args = parser.parse_args()
    if args.render_only:
        document = json.loads((args.output / "scaling.json").read_text(encoding="utf-8"))
        publish(args.output, document, [Trial(**t) for t in document["trials"]])
        return 0

    scratch = Path(tempfile.mkdtemp(prefix="agentenv-scaling-"))
    home = args.home or scratch / "home"
    env = {**os.environ, "AGENTENV_HOME": str(home)}
    rng = random.Random(args.seed)  # noqa: S311 - ordering, not security
    trials: list[Trial] = []
    try:
        for trial in range(1, args.trials + 1):
            order = list(args.workers)
            rng.shuffle(order)
            for workers in order:
                result = run_trial(args.experiment, trial, workers, scratch / "trials", env)
                trials.append(result)
                print(
                    f"trial {trial} workers={workers}: running {result.running_seconds:.1f}s, "
                    f"wall {result.wall_seconds:.1f}s, valid={result.results_valid}, "
                    f"cleanup_ok={result.cleanup_ok}, leftovers={result.leftover_containers}",
                    flush=True,
                )
        environment = host_environment(scratch / "trials")
        document = {
            "benchmark": "scaling",
            "date": datetime.now(UTC).strftime("%Y-%m-%d"),
            "agentenvforge_version": environment.pop("agentenvforge_version"),
            "experiment": args.experiment.resolve().relative_to(HERE.parent).as_posix(),
            "planned_runs": trials[0].planned,
            "trials_per_setting": args.trials,
            "worker_order_seed": args.seed,
            "environment": environment,
            "trials": [asdict(t) for t in trials],
        }
        publish(args.output, document, trials)
        print(f"results: {args.output}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    ok = all(t.exit_code == 0 and t.cleanup_ok and not t.leftover_containers for t in trials)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
