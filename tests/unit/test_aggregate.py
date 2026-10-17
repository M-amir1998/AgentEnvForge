"""Experiment aggregation with synthetic runs whose outcomes are known exactly."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentenvforge.analysis.aggregate import (
    MIN_RUNS_FOR_P95,
    AgentInfo,
    ExperimentFacts,
    RunObservation,
    summarize,
)
from agentenvforge.analysis.models import ExperimentSummary
from agentenvforge.domain.failures import FailureInfo, FailureReason
from agentenvforge.domain.results import GradeResult, RunResult, StageTiming
from agentenvforge.domain.status import RunStatus

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def result(
    *,
    passed: bool | None = None,
    reason: FailureReason | None = None,
    seconds: float = 10.0,
    stage: RunStatus = RunStatus.RUNNING_AGENT,
) -> RunResult:
    grade = (
        GradeResult(grader="g", grader_type="pytest", passed=passed, score=float(passed))
        if passed is not None
        else None
    )
    failure = (
        FailureInfo(reason=reason, stage=stage, message="x", error_type="X") if reason else None
    )
    return RunResult(
        run_id="run_x",
        task_id="t",
        status=RunStatus.COMPLETED if grade else RunStatus.INTERNAL_ERROR,
        passed=passed,
        grade=grade,
        failure=failure,
        stage_timings=(
            StageTiming(
                status=RunStatus.RUNNING_AGENT, started_at=NOW, duration_ms=int(seconds * 600)
            ),
        ),
        created_at=NOW,
        finished_at=NOW,
        duration_ms=int(seconds * 1000),
    )


def observation(
    agent: str, task: str, repetition: int, run: RunResult | None, patch: str | None = None
) -> RunObservation:
    return RunObservation(
        position=0,
        task_id=task,
        agent_id=agent,
        repetition=repetition,
        seed=repetition - 1,
        run_id=f"run_{agent}_{task}_{repetition}" if run is not None else None,
        result=run,
        patch_sha256=patch if patch is not None else f"{agent}{task}{repetition}",
    )


def facts(
    agents: list[AgentInfo], tasks: list[str], repetitions: int, status: str = "completed"
) -> ExperimentFacts:
    return ExperimentFacts(
        experiment_id="exp_x",
        spec_id="spec",
        name=None,
        status=status,
        workers=2,
        repetitions=repetitions,
        task_ids=tuple(tasks),
        agents=tuple(agents),
        running_seconds=3600.0,
    )


SUBJECT = AgentInfo(agent_id="model", role="subject", adapter="ollama:m")
ORACLE = AgentInfo(agent_id="oracle", role="reference", adapter="command:oracle")
NOOP = AgentInfo(agent_id="noop", role="baseline", adapter="noop")


def test_success_rate_cells_and_validity_checks() -> None:
    observations = [
        observation("model", "easy", 1, result(passed=True)),
        observation("model", "easy", 2, result(passed=True)),
        observation("model", "easy", 3, result(passed=False)),
        observation("model", "hard", 1, result(passed=False)),
        observation("model", "hard", 2, result(reason=FailureReason.AGENT_TIMEOUT)),
        observation(
            "model",
            "hard",
            3,
            result(reason=FailureReason.GRADER_ERROR, stage=RunStatus.RUNNING_GRADER),
        ),
        *[
            observation("oracle", t, r, result(passed=True), patch="same")
            for t in ("easy", "hard")
            for r in (1, 2, 3)
        ],
        *[
            observation("noop", t, r, result(passed=False), patch="empty")
            for t in ("easy", "hard")
            for r in (1, 2, 3)
        ],
    ]
    summary = summarize(
        facts([SUBJECT, ORACLE, NOOP], ["easy", "hard"], 3), observations, generated_at=NOW
    )

    model = next(a for a in summary.agents if a.agent_id == "model")
    assert (model.success.successes, model.success.trials) == (2, 6)  # every run is scored
    assert model.counts.post_agent_error == 1 and model.counts.agent_timeout == 1
    low, high = model.success.ci_low, model.success.ci_high
    assert low is not None and high is not None and low < 2 / 6 < high
    assert model.task_mean_success == pytest.approx((2 / 3 + 0) / 2)
    assert model.pass_at_k is not None and model.pass_at_k.value == pytest.approx(
        0.5
    )  # 1 of 2 tasks

    oracle = next(a for a in summary.agents if a.agent_id == "oracle")
    assert oracle.deterministic and oracle.success.ci_low is None  # identical repetitions
    assert oracle.pass_at_k is not None and oracle.pass_at_k.value is None

    easy = next(t for t in summary.tasks if t.task_id == "easy")
    assert (easy.success.successes, easy.success.trials) == (2, 3)  # subjects only
    assert easy.reference_passed_all is True and easy.baseline_failed_all is True
    assert summary.results_valid and summary.warnings == () and not summary.partial


def test_excluded_runs_leave_the_denominator_and_raise_a_warning() -> None:
    observations = [
        observation("model", "easy", 1, result(passed=True)),
        observation(
            "model",
            "easy",
            2,
            result(reason=FailureReason.SANDBOX_BUILD, stage=RunStatus.BUILDING_SANDBOX),
        ),
        observation("model", "easy", 3, result(reason=FailureReason.AGENT_UNAVAILABLE)),
    ]
    summary = summarize(facts([SUBJECT], ["easy"], 3), observations, generated_at=NOW)
    assert (summary.agents[0].success.successes, summary.agents[0].success.trials) == (1, 1)
    assert summary.counts.infrastructure_error == 1 and summary.counts.agent_unavailable == 1
    assert not summary.results_valid
    assert any("outside the agents' control" in w for w in summary.warnings)


def test_cancelled_experiment_is_partial_and_counts_reconcile() -> None:
    observations = [
        observation("model", "easy", 1, result(passed=True)),
        observation("model", "easy", 2, result(reason=FailureReason.CANCELLED)),
        observation("model", "easy", 3, None),  # never started
    ]
    summary = summarize(
        facts([SUBJECT], ["easy"], 3, status="cancelled"), observations, generated_at=NOW
    )
    counts = summary.counts
    assert (counts.passed, counts.cancelled, counts.not_started) == (1, 1, 1)
    assert counts.planned == counts.started + counts.not_started == 3
    assert summary.partial and not summary.results_valid
    assert summary.invariant_violations == ()


def test_reference_failure_and_baseline_success_are_flagged() -> None:
    observations = [
        observation("oracle", "easy", 1, result(passed=False)),
        observation("noop", "easy", 1, result(passed=True)),
    ]
    summary = summarize(facts([ORACLE, NOOP], ["easy"], 1), observations, generated_at=NOW)
    assert any("reference agent did not pass" in w for w in summary.warnings)
    assert any("baseline agent passed" in w for w in summary.warnings)


def test_no_scored_runs_gives_nulls_not_numbers() -> None:
    observations = [observation("model", "easy", 1, result(reason=FailureReason.CANCELLED))]
    summary = summarize(
        facts([SUBJECT], ["easy"], 1, status="cancelled"), observations, generated_at=NOW
    )
    agent = summary.agents[0]
    assert agent.success.estimate is None and agent.success.note == "no scored runs"
    assert agent.task_mean_success is None and agent.run_seconds is None
    ExperimentSummary.model_validate_json(summary.model_dump_json())  # JSON round-trip, no NaN


def test_p95_needs_enough_runs_and_timeouts_are_censored() -> None:
    few = [observation("model", "easy", r, result(passed=True, seconds=r)) for r in range(1, 6)]
    summary = summarize(facts([SUBJECT], ["easy"], 5), few, generated_at=NOW)
    seconds = summary.agents[0].run_seconds
    assert seconds is not None and seconds.p95 is None and seconds.count == 5
    assert seconds.median == pytest.approx(3.0)

    many = [
        observation("model", "easy", r, result(passed=True, seconds=r))
        for r in range(1, MIN_RUNS_FOR_P95 + 1)
    ]
    many.append(
        observation("model", "easy", 99, result(reason=FailureReason.AGENT_TIMEOUT, seconds=300))
    )
    summary = summarize(facts([SUBJECT], ["easy"], MIN_RUNS_FOR_P95 + 1), many, generated_at=NOW)
    seconds = summary.agents[0].run_seconds
    assert seconds is not None and seconds.p95 is not None and seconds.censored == 1


def test_failures_are_broken_down_by_reason_and_stage() -> None:
    observations = [
        observation("model", "easy", 1, result(passed=False)),
        observation("model", "easy", 2, result(reason=FailureReason.AGENT_TIMEOUT)),
        observation("model", "easy", 3, result(reason=FailureReason.AGENT_TIMEOUT)),
    ]
    summary = summarize(facts([SUBJECT], ["easy"], 3), observations, generated_at=NOW)
    rows = {(f.outcome, f.reason, f.stage): f.count for f in summary.failures}
    assert rows == {
        ("failed", "graded_failure", None): 1,
        ("agent_timeout", "agent_timeout", "running_agent"): 2,
    }
