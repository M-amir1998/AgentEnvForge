"""Experiment plots (PNG, headless).

Four charts, each answering one question; no decoration:

* ``success_by_agent.png``   - how often did each agent solve the tasks? (Wilson whiskers)
* ``success_matrix.png``     - on which tasks? (agent x task heatmap, k/n in every cell)
* ``run_time_by_agent.png``  - how long did runs take? (one dot per run, median marked)
* ``outcomes_by_agent.png``  - why did runs fail? (outcome categories, stacked counts)

matplotlib is optional (``pip install agentenvforge[plots]``); without it, plots are skipped
and the Markdown report and CSV/JSON files are still written. The object-oriented API is used
(no pyplot global state), so plotting works headless, in CI and from any thread.

Colours: categorical slots in fixed order (validated for colour-vision deficiency on the
light surface), one sequential blue ramp for the heatmap, neutral grey for runs that do not
count. Identity never relies on colour alone: every chart is direct-labelled or has a legend,
and report.md contains the same numbers as tables.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentenvforge.analysis.aggregate import RunObservation
from agentenvforge.analysis.models import ExperimentSummary
from agentenvforge.analysis.outcomes import Outcome

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
SUBJECT = "#2a78d6"
NON_SUBJECT = "#9a9993"
EXCLUDED = "#c9c8c2"
HEAT_RAMP = ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")

# Fixed outcome -> colour mapping (categorical slots 1-6 in order; never by rank).
OUTCOME_COLOURS: tuple[tuple[str, tuple[Outcome, ...], str], ...] = (
    ("passed", (Outcome.PASSED,), "#2a78d6"),
    ("failed (graded)", (Outcome.FAILED,), "#eb6834"),
    ("agent timeout", (Outcome.AGENT_TIMEOUT,), "#1baf7a"),
    ("solution timeout", (Outcome.SOLUTION_TIMEOUT,), "#eda100"),
    ("agent error", (Outcome.AGENT_ERROR,), "#e87ba4"),
    ("post-agent error", (Outcome.POST_AGENT_ERROR,), "#008300"),
    (
        "excluded (not scored)",
        (
            Outcome.INFRASTRUCTURE_ERROR,
            Outcome.AGENT_UNAVAILABLE,
            Outcome.CANCELLED,
            Outcome.INCOMPLETE,
        ),
        EXCLUDED,
    ),
)


def plotting_available() -> bool:
    """True when matplotlib is installed."""
    return importlib.util.find_spec("matplotlib") is not None


def render_plots(
    summary: ExperimentSummary, observations: Sequence[RunObservation], directory: Path
) -> list[Path]:
    """Write every plot into ``directory``; return the files written (empty without matplotlib)."""
    if not plotting_available():
        return []
    directory.mkdir(parents=True, exist_ok=True)
    return [
        _success_by_agent(summary, directory / "success_by_agent.png"),
        _success_matrix(summary, directory / "success_matrix.png"),
        _run_time_by_agent(summary, observations, directory / "run_time_by_agent.png"),
        _outcomes_by_agent(summary, directory / "outcomes_by_agent.png"),
    ]


# ------------------------------------------------------------------------------ helpers


def _figure(width: float, height: float) -> tuple[Any, Any]:
    from matplotlib.figure import Figure  # noqa: PLC0415 - optional dependency

    figure = Figure(figsize=(width, height), dpi=150, facecolor=SURFACE)
    axes = figure.add_subplot()
    axes.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(GRID)
    axes.tick_params(colors=TEXT_SECONDARY, labelsize=8, length=0)
    axes.grid(axis="x", color=GRID, linewidth=0.8)
    axes.set_axisbelow(True)
    return figure, axes


def _finish(figure: Any, axes: Any, title: str, path: Path) -> Path:
    axes.set_title(title, loc="left", fontsize=10, color=TEXT, pad=10)
    figure.tight_layout()
    figure.savefig(path, facecolor=SURFACE)
    return path


def _labels(summary: ExperimentSummary) -> list[str]:
    return [
        agent.agent_id if agent.role == "subject" else f"{agent.agent_id} ({agent.role})"
        for agent in summary.agents
    ]


# ------------------------------------------------------------------------------- charts


def _success_by_agent(summary: ExperimentSummary, path: Path) -> Path:
    agents = summary.agents
    figure, axes = _figure(7.0, 0.55 * len(agents) + 1.4)
    positions = list(range(len(agents)))[::-1]
    for position, agent in zip(positions, agents, strict=True):
        rate = agent.success
        colour = SUBJECT if agent.role == "subject" else NON_SUBJECT
        if rate.estimate is None:
            axes.text(
                1.03,
                position,
                "no scored runs",
                va="center",
                fontsize=8,
                color=TEXT_SECONDARY,
                transform=axes.get_yaxis_transform(),
            )
            continue
        axes.barh(position, rate.estimate, height=0.5, color=colour)
        if rate.ci_low is not None and rate.ci_high is not None:
            axes.errorbar(
                rate.estimate,
                position,
                xerr=[[rate.estimate - rate.ci_low], [rate.ci_high - rate.estimate]],
                fmt="none",
                ecolor=TEXT,
                elinewidth=1.2,
                capsize=3,
            )
        label = f"{rate.successes}/{rate.trials}  ({rate.estimate:.0%})"
        if agent.deterministic:
            label += "  identical repetitions"
        # A label column right of the axes: never on top of a bar or a whisker.
        axes.text(
            1.03,
            position,
            label,
            va="center",
            fontsize=8,
            color=TEXT_SECONDARY,
            transform=axes.get_yaxis_transform(),
        )
    axes.set_yticks(positions, _labels(summary), fontsize=8, color=TEXT)
    axes.set_xlim(0, 1)
    axes.xaxis.set_major_formatter(_percent())
    axes.set_xlabel(
        "success rate (scored runs); whiskers: 95% Wilson interval",
        fontsize=8,
        color=TEXT_SECONDARY,
    )
    return _finish(figure, axes, "Success rate by agent", path)


def _success_matrix(summary: ExperimentSummary, path: Path) -> Path:
    agents = summary.agents
    tasks = [task.task_id for task in summary.tasks]
    figure, axes = _figure(1.6 * len(tasks) + 2.6, 0.5 * len(agents) + 1.4)
    axes.grid(False)
    axes.spines["bottom"].set_visible(False)
    cells = {(cell.agent_id, cell.task_id): cell for cell in summary.cells}
    for row, agent in enumerate(agents):
        for column, task_id in enumerate(tasks):
            rate = cells[(agent.agent_id, task_id)].success
            if rate.estimate is None:
                colour, text, ink = SURFACE, "-", TEXT_SECONDARY
            else:
                step = min(len(HEAT_RAMP) - 1, int(rate.estimate * (len(HEAT_RAMP) - 1) + 0.5))
                colour, text = HEAT_RAMP[step], f"{rate.successes}/{rate.trials}"
                ink = "#ffffff" if step >= 4 else TEXT
            axes.add_patch(_rectangle((column, len(agents) - 1 - row), colour))
            axes.text(
                column + 0.5,
                len(agents) - 0.5 - row,
                text,
                ha="center",
                va="center",
                fontsize=8,
                color=ink,
            )
    axes.set_xlim(0, len(tasks))
    axes.set_ylim(0, len(agents))
    axes.set_xticks([c + 0.5 for c in range(len(tasks))], tasks, fontsize=8, color=TEXT)
    axes.set_yticks(
        [len(agents) - 0.5 - r for r in range(len(agents))],
        _labels(summary),
        fontsize=8,
        color=TEXT,
    )
    axes.xaxis.tick_top()
    return _finish(figure, axes, "Passed / scored runs, by agent and task", path)


def _run_time_by_agent(
    summary: ExperimentSummary, observations: Sequence[RunObservation], path: Path
) -> Path:
    agents = summary.agents
    figure, axes = _figure(7.0, 0.55 * len(agents) + 1.4)
    positions = list(range(len(agents)))[::-1]
    for position, agent in zip(positions, agents, strict=True):
        durations = [
            o.result.duration_ms / 1000
            for o in observations
            if o.agent_id == agent.agent_id and o.result is not None and o.outcome.is_scored
        ]
        if not durations:
            continue
        # Deterministic vertical spread so overlapping dots stay visible.
        offsets = [((index % 5) - 2) * 0.06 for index in range(len(durations))]
        colour = SUBJECT if agent.role == "subject" else NON_SUBJECT
        axes.scatter(
            durations,
            [position + o for o in offsets],
            s=14,
            color=colour,
            alpha=0.75,
            edgecolors=SURFACE,
            linewidths=0.6,
            zorder=3,
        )
        if agent.run_seconds is not None:
            median = agent.run_seconds.median
            axes.plot(
                [median, median],
                [position - 0.28, position + 0.28],
                color=TEXT,
                linewidth=2,
                zorder=4,
            )
    axes.set_yticks(positions, _labels(summary), fontsize=8, color=TEXT)
    axes.set_xlim(left=0)
    axes.set_xlabel(
        "run duration in seconds (one dot per scored run; bar: median)",
        fontsize=8,
        color=TEXT_SECONDARY,
    )
    return _finish(figure, axes, "Run time by agent", path)


def _outcomes_by_agent(summary: ExperimentSummary, path: Path) -> Path:
    agents = summary.agents
    figure, axes = _figure(8.5, 0.55 * len(agents) + 1.4)
    positions = list(range(len(agents)))[::-1]
    shown: set[str] = set()
    for position, agent in zip(positions, agents, strict=True):
        left = 0
        for label, outcomes, colour in OUTCOME_COLOURS:
            count = sum(agent.counts.count(outcome) for outcome in outcomes)
            if not count:
                continue
            axes.barh(
                position,
                count,
                left=left,
                height=0.5,
                color=colour,
                edgecolor=SURFACE,
                linewidth=2,
            )
            shown.add(label)
            if count >= 2 or agent.counts.planned <= 6:
                axes.text(
                    left + count / 2,
                    position,
                    str(count),
                    ha="center",
                    va="center",
                    fontsize=7,
                    color=TEXT,
                )
            left += count
    axes.set_yticks(positions, _labels(summary), fontsize=8, color=TEXT)
    axes.set_xlabel("planned runs", fontsize=8, color=TEXT_SECONDARY)
    # Legend right of the chart, in the fixed category order, listing only what occurs.
    axes.legend(
        handles=[_patch(colour, label) for label, _, colour in OUTCOME_COLOURS if label in shown],
        loc="upper left",
        bbox_to_anchor=(1.01, 1.0),
        frameon=False,
        fontsize=7,
        labelcolor=TEXT,
    )
    return _finish(figure, axes, "Outcome of every planned run, by agent", path)


def _rectangle(origin: tuple[float, float], colour: str) -> Any:
    from matplotlib.patches import Rectangle  # noqa: PLC0415 - optional dependency

    return Rectangle(origin, 1, 1, facecolor=colour, edgecolor=SURFACE, linewidth=2)


def _patch(colour: str, label: str) -> Any:
    from matplotlib.patches import Patch  # noqa: PLC0415 - optional dependency

    return Patch(facecolor=colour, label=label)


def _percent() -> Any:
    from matplotlib.ticker import PercentFormatter  # noqa: PLC0415 - optional dependency

    return PercentFormatter(xmax=1.0)
