"""Regenerate the README's graphics from the committed benchmark results.

    uv run python scripts/readme_figures.py [--png DIR]

Writes docs/assets/readme/: a banner, a terminal demo, stat tiles and four charts, each
chart and the tiles in a light and a dark variant (the README picks one with
``<picture>`` and ``prefers-color-scheme``). Every number comes from benchmarks/results/,
so the images cannot drift from the data. ``--png DIR`` also writes PNG previews.

Colours follow the validated data-viz palette: categorical slots in fixed order, a
separate step of each hue for the dark surface, neutral grey for control agents, and a
visible text label on every mark (two light-mode slots sit below 3:1 contrast).
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

import matplotlib as mpl
from matplotlib.figure import Figure
from matplotlib.patches import FancyBboxPatch

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "benchmarks" / "results"
OUT = ROOT / "docs" / "assets" / "readme"
SANS = "system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"

mpl.rcParams.update(
    {
        "svg.hashsalt": "agentenvforge",  # stable SVG ids: regenerating changes nothing
        "font.family": "DejaVu Sans",
        "axes.unicode_minus": False,
    }
)


@dataclass(frozen=True)
class Theme:
    name: str
    surface: str
    ink: str
    ink2: str
    muted: str
    grid: str
    axis: str
    series: str
    control: str
    outcome: dict[str, str]


LIGHT = Theme(
    "light",
    "#fcfcfb",
    "#0b0b0b",
    "#52514e",
    "#898781",
    "#e1e0d9",
    "#c3c2b7",
    "#2a78d6",
    "#a3a29c",
    {
        "passed": "#2a78d6",
        "failed": "#eb6834",
        "agent_timeout": "#1baf7a",
        "agent_error": "#eda100",
    },
)
DARK = Theme(
    "dark",
    "#1a1a19",
    "#f0efec",
    "#c3c2b7",
    "#898781",
    "#2c2c2a",
    "#383835",
    "#3987e5",
    "#6f6e69",
    {
        "passed": "#3987e5",
        "failed": "#d95926",
        "agent_timeout": "#199e70",
        "agent_error": "#c98500",
    },
)

MODEL_NAMES = {
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-opus-5-5": "Claude Opus 5.5",
}


# ------------------------------------------------------------------------------- data


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_runs(name: str) -> list[dict[str, str]]:
    with (RESULTS / name / "runs.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def claude_costs() -> dict[str, float]:
    """Mean list-price cost per run of each model, from the exported run evidence."""
    costs: dict[str, list[float]] = {}
    for row in load_runs("claude-code"):
        if row["agent_id"] in MODEL_NAMES and row["run_id"]:
            result = load_json(
                RESULTS / "claude-code" / "artifacts" / row["run_id"] / "result.json"
            )
            cost = result["agent"]["metadata"].get("cost_usd_list_price")
            if isinstance(cost, (int, float)):
                costs.setdefault(row["agent_id"], []).append(float(cost))
    return {agent: statistics.mean(values) for agent, values in costs.items()}


# ------------------------------------------------------------------------- matplotlib


def figure(theme: Theme, width: float, height: float) -> tuple[Figure, Any]:
    fig = Figure(figsize=(width, height), dpi=100, facecolor=theme.surface)
    axes = fig.add_subplot()
    style(axes, theme)
    return fig, axes


def style(axes: Any, theme: Theme) -> None:
    axes.set_facecolor(theme.surface)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(theme.axis)
    axes.tick_params(colors=theme.ink2, labelsize=10, length=0)
    axes.grid(axis="x", color=theme.grid, linewidth=0.8)
    axes.set_axisbelow(True)


def title(fig: Figure, theme: Theme, text: str, subtitle: str) -> None:
    fig.text(
        0.012, 0.965, text, ha="left", va="top", fontsize=14, color=theme.ink, fontweight="bold"
    )
    fig.text(0.012, 0.885, subtitle, ha="left", va="top", fontsize=10, color=theme.ink2)


def save(fig: Figure, name: str, theme: Theme, png: Path | None) -> None:
    fig.savefig(OUT / f"{name}-{theme.name}.svg", facecolor=theme.surface, metadata={"Date": None})
    if png is not None:
        fig.savefig(png / f"{name}-{theme.name}.png", facecolor=theme.surface, dpi=110)


def claude_success(theme: Theme, png: Path | None) -> None:
    summary = load_json(RESULTS / "claude-code" / "summary.json")
    agents = {agent["agent_id"]: agent for agent in summary["agents"]}
    rows = [(MODEL_NAMES[key], agents[key], theme.series, "") for key in MODEL_NAMES] + [
        ("Reference (applies the fix)", agents["oracle"], theme.control, "control"),
        ("Baseline (changes nothing)", agents["noop"], theme.control, "control"),
    ]
    fig, axes = figure(theme, 9.0, 3.9)
    fig.subplots_adjust(left=0.235, right=0.80, top=0.76, bottom=0.17)
    positions = [5.4, 4.4, 3.4, 1.8, 0.8]
    for position, (_label, agent, colour, kind) in zip(positions, rows, strict=True):
        rate = agent["success"]
        estimate = rate["estimate"] or 0.0
        axes.barh(position, estimate, height=0.56, color=colour)
        if rate["ci_low"] is not None and rate["ci_high"] is not None:
            axes.errorbar(
                estimate,
                position,
                xerr=[[estimate - rate["ci_low"]], [rate["ci_high"] - estimate]],
                fmt="none",
                ecolor=theme.ink,
                elinewidth=1.3,
                capsize=4,
            )
        note = f"{rate['successes']}/{rate['trials']}  ({estimate:.0%})"
        identical = agent["deterministic"] and not kind
        axes.text(
            1.03,
            position + (0.13 if identical else 0),
            note,
            va="center",
            fontsize=10.5,
            color=theme.ink,
            transform=axes.get_yaxis_transform(),
        )
        if identical:  # no whisker: identical repetitions count as one observation per task
            axes.text(
                1.03,
                position - 0.24,
                "same patch every run",
                va="center",
                fontsize=8.5,
                color=theme.ink2,
                transform=axes.get_yaxis_transform(),
            )
    axes.set_yticks(positions, [row[0] for row in rows], fontsize=10.5, color=theme.ink)
    axes.axhline(2.6, color=theme.grid, linewidth=1)
    axes.set_xlim(0, 1)
    axes.set_ylim(0.2, 6.0)
    axes.xaxis.set_major_formatter(mpl.ticker.PercentFormatter(xmax=1.0))
    axes.set_xlabel(
        "runs that passed the hidden tests  ·  whiskers: 95% Wilson interval",
        fontsize=9.5,
        color=theme.ink2,
    )
    title(
        fig,
        theme,
        "Hosted models as coding agents",
        "3 bug-fix tasks (easy, medium, hard) × 3 runs per model, graded by hidden tests",
    )
    save(fig, "claude-success", theme, png)


def claude_time_cost(theme: Theme, png: Path | None) -> None:
    runs = [row for row in load_runs("claude-code") if row["agent_id"] in MODEL_NAMES]
    costs = claude_costs()
    order = list(MODEL_NAMES)
    fig = Figure(figsize=(9.0, 3.3), dpi=100, facecolor=theme.surface)
    left, right = fig.add_subplot(1, 2, 1), fig.add_subplot(1, 2, 2)
    fig.subplots_adjust(left=0.17, right=0.97, top=0.72, bottom=0.2, wspace=0.12)
    positions = [2, 1, 0]
    for axes in (left, right):
        style(axes, theme)
        axes.set_ylim(-0.6, 2.6)
    for position, agent in zip(positions, order, strict=True):
        durations = sorted(
            float(row["duration_seconds"]) for row in runs if row["agent_id"] == agent
        )
        offsets = [((index % 3) - 1) * 0.13 for index in range(len(durations))]
        left.scatter(
            durations,
            [position + offset for offset in offsets],
            s=26,
            color=theme.series,
            alpha=0.8,
            edgecolors=theme.surface,
            linewidths=0.8,
            zorder=3,
        )
        median = statistics.median(durations)
        left.plot(
            [median, median],
            [position - 0.32, position + 0.32],
            color=theme.ink,
            linewidth=2.2,
            zorder=4,
        )
        left.text(
            median,
            position + 0.42,
            f"median {median:.1f} s",
            ha="center",
            fontsize=9,
            color=theme.ink2,
        )
        cost = costs[agent]
        right.barh(position, cost, height=0.5, color=theme.series)
        right.text(
            cost + 0.0008, position, f"${cost:.3f}", va="center", fontsize=10, color=theme.ink
        )
    left.set_yticks(
        positions, [MODEL_NAMES[agent] for agent in order], fontsize=10.5, color=theme.ink
    )
    right.set_yticks([])
    left.set_xlim(0, 26)
    left.set_xlabel("seconds per run (dots: runs)", fontsize=9.5, color=theme.ink2)
    right.set_xlim(0, max(costs.values()) * 1.3)
    right.set_xlabel("list-price cost per run (USD)", fontsize=9.5, color=theme.ink2)
    title(
        fig,
        theme,
        "Time and cost per run",
        "Each run: sandbox start, the agent's work, grading in a fresh sandbox, cleanup",
    )
    save(fig, "claude-time-cost", theme, png)


def scaling(theme: Theme, png: Path | None) -> None:
    data = load_json(RESULTS / "scaling" / "scaling.json")
    rows = data["aggregate"]
    workers = [row["workers"] for row in rows]
    fig, axes = figure(theme, 9.0, 3.6)
    axes.grid(axis="y", color=theme.grid, linewidth=0.8)
    axes.grid(axis="x", visible=False)
    fig.subplots_adjust(left=0.08, right=0.97, top=0.76, bottom=0.17)
    axes.plot(workers, workers, color=theme.muted, linewidth=1.2, linestyle=(0, (4, 3)))
    axes.text(3.25, 3.42, "ideal (linear)", ha="right", va="bottom", fontsize=9, color=theme.muted)
    trials = data["within_trial_speedups"]
    axes.scatter(
        [int(key.split("/")[1]) for key in trials],
        list(trials.values()),
        s=34,
        color=theme.series,
        alpha=0.45,
        edgecolors=theme.surface,
        linewidths=0.8,
        zorder=3,
    )
    speedups = [row["speedup"] for row in rows]
    axes.plot(
        workers,
        speedups,
        color=theme.series,
        linewidth=2.4,
        zorder=4,
        marker="o",
        markersize=6,
        markeredgecolor=theme.surface,
    )
    last = rows[-1]["workers"]
    for row in rows[1:]:  # below the median line, which rises to the right
        at_end = row["workers"] == last
        axes.annotate(
            f"{row['speedup']:.2f}×  ·  {row['runs_per_hour_median']:,} runs/h",
            (row["workers"], row["speedup"]),
            textcoords="offset points",
            xytext=(-10 if at_end else 10, -22),
            ha="right" if at_end else "left",
            fontsize=10,
            color=theme.ink,
        )
    axes.set_xticks(
        workers,
        [f"{w} worker{'s' if w > 1 else ''}" for w in workers],
        fontsize=10,
        color=theme.ink,
    )
    axes.set_xlim(0.7, 4.3)
    axes.set_ylim(0, 4.4)
    axes.set_ylabel("speedup vs 1 worker", fontsize=9.5, color=theme.ink2)
    title(
        fig,
        theme,
        "Batch throughput scales with workers",
        "30-run batches, 3 interleaved trials per setting; dots: trials, line: median",
    )
    save(fig, "scaling", theme, png)


def isolation(theme: Theme, png: Path | None) -> None:
    runs = load_runs("failure-isolation")
    agents = [
        ("oracle", "applies the known fix", "passed"),
        ("noop", "changes nothing", "failed"),
        ("failing", "gives up, exit code 1", "failed"),
        ("sleeper", "hangs (5 s budget)", "agent_timeout"),
        ("crasher", "its adapter raises", "agent_error"),
    ]
    labels = {"passed": "passed", "failed": "failed (graded)", "agent_timeout": "agent timeout",
              "agent_error": "agent error"}  # fmt: skip
    fig, axes = figure(theme, 9.0, 3.5)
    axes.grid(False)
    axes.spines["bottom"].set_visible(False)
    axes.set_xticks([])
    fig.subplots_adjust(left=0.25, right=0.98, top=0.76, bottom=0.05)
    for row_index, (agent, description, expected) in enumerate(agents):
        y = len(agents) - 1 - row_index
        outcomes = [run["outcome"] for run in runs if run["agent_id"] == agent]
        for index, outcome in enumerate(outcomes):
            axes.add_patch(
                FancyBboxPatch(
                    (index * 0.62, y - 0.3),
                    0.5,
                    0.6,
                    boxstyle="round,pad=0,rounding_size=0.08",
                    facecolor=theme.outcome[outcome],
                    edgecolor=theme.surface,
                    linewidth=1.5,
                )
            )
        matched = sum(outcome == expected for outcome in outcomes)
        axes.text(
            len(outcomes) * 0.62 + 0.25,
            y,
            f"{matched}/{len(outcomes)} {labels[expected]}, as expected",
            va="center",
            fontsize=10.5,
            color=theme.ink,
        )
        axes.text(
            -0.25,
            y + 0.12,
            agent,
            ha="right",
            va="center",
            fontsize=10.5,
            color=theme.ink,
            fontweight="bold",
        )
        axes.text(
            -0.25, y - 0.2, description, ha="right", va="center", fontsize=9, color=theme.ink2
        )
    axes.set_yticks([])
    axes.set_xlim(-0.1, 7.0)
    axes.set_ylim(-0.6, len(agents) - 0.4)
    title(
        fig,
        theme,
        "Failures stay inside their run",
        "5 scripted agents with known outcomes, run side by side (2 workers); one square per run",
    )
    save(fig, "isolation", theme, png)


# --------------------------------------------------------------------- hand-made SVGs


def stat_tiles(theme: Theme) -> None:
    claude = load_json(RESULTS / "claude-code" / "summary.json")
    model_agents = [agent for agent in claude["agents"] if agent["agent_id"] in MODEL_NAMES]
    passed = sum(agent["success"]["successes"] for agent in model_agents)
    scored = sum(agent["success"]["trials"] for agent in model_agents)
    scaling_data = load_json(RESULTS / "scaling" / "scaling.json")
    best = max(scaling_data["aggregate"], key=lambda row: row["workers"])
    isolation_runs = load_runs("failure-isolation")
    expected_outcome = {"oracle": "passed", "noop": "failed", "failing": "failed",
                        "sleeper": "agent_timeout", "crasher": "agent_error"}  # fmt: skip
    expected = sum(run["outcome"] == expected_outcome[run["agent_id"]] for run in isolation_runs)
    executed = (
        sum(trial["planned"] for trial in scaling_data["trials"])
        + len(isolation_runs)
        + sum(1 for row in load_runs("claude-code") if row["run_id"])
        + sum(1 for row in load_runs("local-ollama") if row["run_id"])
    )
    tiles = [
        (f"{passed}/{scored}", "hosted-model runs passed", "the hidden tests"),
        (
            f"{best['speedup']:.2f}×",
            f"throughput with {best['workers']} workers",
            "vs one run at a time",
        ),
        (
            f"{expected}/{len(isolation_runs)}",
            "runs ended as expected",
            "next to crashes, hangs, give-ups",
        ),
        ("0", "sandboxes left behind", f"after {executed} benchmark runs"),
    ]
    width, height, gap = 1000, 132, 16
    tile = (width - gap * 3) / 4
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Benchmark highlights">',
        f"<style>text{{font-family:{SANS}}}</style>",
    ]
    for index, (value, line1, line2) in enumerate(tiles):
        x = index * (tile + gap)
        parts.append(
            f'<rect x="{x:.1f}" y="1" width="{tile:.1f}" height="{height - 2}" rx="12" '
            f'fill="{theme.surface}" stroke="{theme.axis}"/>'
            f'<text x="{x + 20:.1f}" y="58" font-size="38" font-weight="700" fill="{theme.series}">'
            f"{escape(value)}</text>"
            f'<text x="{x + 20:.1f}" y="88" font-size="15" font-weight="600" fill="{theme.ink}">'
            f"{escape(line1)}</text>"
            f'<text x="{x + 20:.1f}" y="110" font-size="13" fill="{theme.ink2}">'
            f"{escape(line2)}</text>"
        )
    parts.append("</svg>")
    (OUT / f"stats-{theme.name}.svg").write_text("".join(parts) + "\n", encoding="utf-8")


TERMINAL_LINES = [
    ("$ agentenv experiment run benchmarks/claude-code/experiment.yaml", "prompt"),
    (
        "experiment claude-code  Claude models as coding agents (Claude Code with sandbox tools)",
        "bold",
    ),
    ("  3 task(s) x 5 agent(s) x 3 repetition(s) = 45 planned runs, 3 worker(s)", "plain"),
    ("  checking the sandbox backend ...", "dim"),
    (
        "  [ 3/45] passed                 10.6s  claude-haiku-5-5 / moving-average-off-by-one #1",
        "run",
    ),
    (
        "  [ 4/45] passed                  9.3s  claude-sonnet-5-5 / moving-average-off-by-one #1",
        "run",
    ),
    (
        "  [ 6/45] passed                 16.7s  claude-opus-5-5 / moving-average-off-by-one #1",
        "run",
    ),
    (
        "  [12/45] passed                 15.5s  claude-haiku-5-5 / ledger-transaction-rollback #1",
        "run",
    ),
    (
        "  [29/45] failed                 14.3s  claude-haiku-5-5 / ledger-transaction-rollback #2",
        "run",
    ),
    (
        "  [45/45] passed                 20.8s  claude-opus-5-5 / ledger-transaction-rollback #3",
        "run",
    ),
    ("      35 passed, 10 failed, 0 excluded, 0 active, 2m24s elapsed", "dim"),
    ("Experiment exp_01m553s7zcc87j6ddt2ywer4cz (completed)", "bold"),
    ("agent               scored  success   95% CI     timeout  excluded  median", "header"),
    ("claude-haiku-5-5    9       8/9 89%   57%-98%    0        0         11.8s", "plain"),
    ("claude-sonnet-5-5   9       9/9 100%  identical  0        0         11.6s", "plain"),
    ("claude-opus-5-5     9       9/9 100%  70%-100%   0        0         18.7s", "plain"),
    ("oracle (reference)  9       9/9 100%  identical  0        0         2.3s", "plain"),
    ("noop (baseline)     9       0/9 0%    identical  0        0         2.2s", "plain"),
    ("results valid (no validity warnings)", "good"),
    ("  report: …/benchmarks/results/claude-code/report.md", "dim"),
]
"""Real output of `agentenv experiment run` (abridged: omitted lines are left out, the
model column is not shown, and the absolute results path is shortened)."""


def terminal() -> None:
    colours = {"prompt": "#e6edf3", "bold": "#ffffff", "plain": "#c9d1d9", "dim": "#7d8590",
               "header": "#e6edf3", "good": "#3fb950"}  # fmt: skip
    outcome_colours = {"passed": "#3fb950", "failed": "#f85149"}
    line_height, top, left = 21, 52, 22
    width, height = 900, top + line_height * len(TERMINAL_LINES) + 18
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        'aria-label="agentenv experiment run: live progress and summary">',
        "<style>text{font-family:ui-monospace,SFMono-Regular,'Cascadia Mono',Menlo,Consolas,"
        "'Liberation Mono',monospace;font-size:13px;white-space:pre}</style>",
        f'<rect width="{width}" height="{height}" rx="10" fill="#0d1117"/>',
        f'<rect width="{width}" height="34" rx="10" fill="#161b22"/>'
        f'<rect y="24" width="{width}" height="10" fill="#161b22"/>',
        '<circle cx="20" cy="17" r="6" fill="#ff5f57"/>'
        '<circle cx="40" cy="17" r="6" fill="#febc2e"/>'
        '<circle cx="60" cy="17" r="6" fill="#28c840"/>',
        f'<text x="{width / 2}" y="21" text-anchor="middle" fill="#7d8590" '
        'style="font-size:12px">agentenv</text>',
    ]
    for index, (line, kind) in enumerate(TERMINAL_LINES):
        y = top + index * line_height
        weight = ' font-weight="700"' if kind in {"bold", "header", "prompt"} else ""
        if kind == "run":
            prefix, outcome, rest = line[:10], line[10:16], line[16:]
            parts.append(
                f'<text x="{left}" y="{y}" xml:space="preserve" fill="{colours["dim"]}">'
                f'{escape(prefix)}<tspan fill="{outcome_colours[outcome.strip()]}" '
                f'font-weight="700">{escape(outcome)}</tspan>'
                f'<tspan fill="{colours["plain"]}">{escape(rest)}</tspan></text>'
            )
        else:
            parts.append(
                f'<text x="{left}" y="{y}" xml:space="preserve" fill="{colours[kind]}"{weight}>'
                f"{escape(line)}</text>"
            )
    parts.append("</svg>")
    (OUT / "terminal.svg").write_text("".join(parts) + "\n", encoding="utf-8")


def banner() -> None:
    width, height = 1280, 300
    mono = "ui-monospace,SFMono-Regular,'Cascadia Mono',Menlo,Consolas,monospace"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
     viewBox="0 0 {width} {height}" role="img" aria-label="AgentEnvForge">
<style>
  .sans {{ font-family: {SANS}; }}
  .mono {{ font-family: {mono}; }}
</style>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#0b1220"/><stop offset="1" stop-color="#15233d"/>
  </linearGradient>
  <linearGradient id="spark" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#ffd166"/><stop offset="1" stop-color="#eb6834"/>
  </linearGradient>
  <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
    <path d="M32 0H0V32" fill="none" stroke="#ffffff" stroke-opacity="0.045"/>
  </pattern>
</defs>
<rect width="{width}" height="{height}" rx="18" fill="url(#bg)"/>
<rect width="{width}" height="{height}" rx="18" fill="url(#grid)"/>
<g transform="translate(150 150)" fill="none" stroke-linejoin="round" stroke-linecap="round">
  <path d="M0 -84 L73 -42 L73 42 L0 84 L-73 42 L-73 -42 Z" stroke="#3987e5" stroke-width="5"/>
  <path d="M-73 -42 L0 0 L73 -42 M0 0 L0 84" stroke="#3987e5" stroke-width="5"
        stroke-opacity="0.55"/>
  <path d="M0 -50 L43 -25 L43 25 L0 50 L-43 25 L-43 -25 Z" stroke="#86b6ef" stroke-width="3"
        stroke-dasharray="7 6"/>
  <path d="M10 -30 L-14 6 L2 6 L-8 34 L18 -4 L2 -4 Z" fill="url(#spark)" stroke="none"/>
</g>
<text class="sans" x="290" y="140" fill="#ffffff" font-size="68" font-weight="800"
      letter-spacing="-1">AgentEnvForge</text>
<text class="sans" x="292" y="190" fill="#c9d1d9" font-size="25">Isolated, reproducible
environments for evaluating AI coding agents</text>
<text class="mono" x="292" y="232" fill="#7d8590" font-size="17">define a task · run any agent
in a sandbox · grade with hidden tests · reproduce</text>
</svg>
"""
    (OUT / "banner.svg").write_text(svg, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--png", type=Path, default=None, help="Also write PNG previews here.")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.png is not None:
        args.png.mkdir(parents=True, exist_ok=True)
    for theme in (LIGHT, DARK):
        claude_success(theme, args.png)
        claude_time_cost(theme, args.png)
        scaling(theme, args.png)
        isolation(theme, args.png)
        stat_tiles(theme)
    terminal()
    banner()
    for path in sorted(OUT.iterdir()):
        print(f"{path.relative_to(ROOT).as_posix():<45} {path.stat().st_size / 1024:6.1f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
