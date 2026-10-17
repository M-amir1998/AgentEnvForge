"""Matrix expansion, the experiment state machine and outcome classification (pure)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentenvforge.analysis.outcomes import SCORED_OUTCOMES, Outcome, classify
from agentenvforge.domain.failures import FailureInfo, FailureReason
from agentenvforge.domain.results import GradeResult, RunResult
from agentenvforge.domain.status import RunStatus
from agentenvforge.experiments.matrix import derive_seed, expand_matrix, matrix_size
from agentenvforge.experiments.status import (
    ALLOWED_TRANSITIONS,
    ExperimentStateMachine,
    ExperimentStatus,
    InvalidExperimentTransitionError,
)

S = ExperimentStatus

# --------------------------------------------------------------------------- matrix


def test_matrix_is_interleaved_by_repetition_with_shuffled_blocks() -> None:
    runs = list(expand_matrix(["t1", "t2"], ["a1", "a2", "a3"], 2))
    blocks = [(r.repetition, r.task_id) for r in runs]
    assert blocks == [(1, "t1")] * 3 + [(1, "t2")] * 3 + [(2, "t1")] * 3 + [(2, "t2")] * 3
    for _, group in itertools.groupby(runs, key=lambda r: (r.repetition, r.task_id)):
        assert sorted(r.agent_id for r in group) == ["a1", "a2", "a3"]
    assert [r.position for r in runs] == list(range(12))


def test_block_order_is_deterministic_and_seed_dependent() -> None:
    def order(seed: int) -> list[str]:
        return [r.agent_id for r in expand_matrix(["t"], list("abcdefgh"), 3, order_seed=seed)]

    assert order(1) == order(1)
    assert order(1) != order(2)


def test_seeds_vary_by_repetition_and_are_shared_within_a_repetition() -> None:
    runs = list(expand_matrix(["t1", "t2"], ["a1", "a2"], 3, base_seed=100))
    for repetition, group in itertools.groupby(runs, key=lambda r: r.repetition):
        assert {r.seed for r in group} == {derive_seed(100, repetition)}
    assert sorted({r.seed for r in runs}) == [100, 101, 102]


@given(st.integers(1, 5), st.integers(1, 5), st.integers(1, 6), st.integers(0, 1000))
def test_matrix_covers_every_combination_exactly_once(
    task_count: int, agent_count: int, repetitions: int, base_seed: int
) -> None:
    tasks = [f"t{i}" for i in range(task_count)]
    agents = [f"a{i}" for i in range(agent_count)]
    runs = list(expand_matrix(tasks, agents, repetitions, base_seed=base_seed))
    assert len(runs) == matrix_size(task_count, agent_count, repetitions)
    keys = [(r.task_id, r.agent_id, r.repetition) for r in runs]
    assert len(set(keys)) == len(keys)
    assert set(keys) == set(itertools.product(tasks, agents, range(1, repetitions + 1)))


def test_matrix_is_generated_lazily() -> None:
    """Planned runs are produced on demand, so a scheduler never materialises them all."""
    matrix = expand_matrix([f"t{i}" for i in range(1000)], ["a"], 1000)
    assert next(matrix).position == 0  # a million-run matrix costs nothing until consumed


def test_matrix_rejects_zero_repetitions() -> None:
    with pytest.raises(ValueError, match="repetitions"):
        list(expand_matrix(["t"], ["a"], 0))


# ---------------------------------------------------------------------- state machine

COMPLETED_PATH = [S.PREPARING, S.RUNNING, S.FINALIZING, S.COMPLETED]
CANCELLED_PATH = [S.PREPARING, S.RUNNING, S.CANCELLING, S.FINALIZING, S.CANCELLED]
FAILED_PATH = [S.PREPARING, S.FINALIZING, S.FAILED]


@pytest.mark.parametrize("path", [COMPLETED_PATH, CANCELLED_PATH, FAILED_PATH])
def test_legal_paths(path: list[ExperimentStatus]) -> None:
    machine = ExperimentStateMachine()
    for status in path:
        machine.transition(status)
    assert machine.current is path[-1]
    assert machine.entered(S.CREATED) is not None


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ([], S.RUNNING),  # cannot skip preparation
        ([S.PREPARING], S.COMPLETED),  # terminal states only via FINALIZING
        ([S.PREPARING, S.RUNNING], S.PREPARING),  # no going back
        (FAILED_PATH, S.RUNNING),  # terminal states are absorbing
    ],
)
def test_illegal_transitions_are_rejected(
    path: list[ExperimentStatus], illegal: ExperimentStatus
) -> None:
    machine = ExperimentStateMachine()
    for status in path:
        machine.transition(status)
    with pytest.raises(InvalidExperimentTransitionError):
        machine.transition(illegal)


@given(st.lists(st.sampled_from(list(ExperimentStatus)), max_size=30))
def test_random_transitions_preserve_invariants(attempts: list[ExperimentStatus]) -> None:
    machine = ExperimentStateMachine()
    for requested in attempts:
        legal = requested in ALLOWED_TRANSITIONS[machine.current]
        try:
            machine.transition(requested)
        except InvalidExperimentTransitionError:
            assert not legal
        else:
            assert legal
    statuses = [change.status for change in machine.history]
    for previous, current in itertools.pairwise(statuses):
        if current.is_terminal:
            assert previous is S.FINALIZING
    assert sum(status.is_terminal for status in statuses) <= 1


# ---------------------------------------------------------------------------- outcomes


def _result(
    status: RunStatus,
    *,
    passed: bool | None = None,
    reason: FailureReason | None = None,
    stage: RunStatus = RunStatus.RUNNING_AGENT,
) -> RunResult:
    now = datetime.now(UTC)
    grade = (
        GradeResult(grader="g", grader_type="pytest", passed=passed, score=1.0 if passed else 0.0)
        if passed is not None
        else None
    )
    failure = (
        FailureInfo(reason=reason, stage=stage, message="x", error_type="X")
        if reason is not None
        else None
    )
    return RunResult(
        run_id="run_x",
        task_id="t",
        status=status,
        passed=passed,
        grade=grade,
        failure=failure,
        created_at=now,
        finished_at=now,
        duration_ms=1,
    )


R = FailureReason
CASES = [
    # A grade stands, even if something failed afterwards.
    (_result(RunStatus.COMPLETED, passed=True), Outcome.PASSED),
    (_result(RunStatus.COMPLETED, passed=False), Outcome.FAILED),
    (
        _result(RunStatus.INTERNAL_ERROR, passed=True, reason=R.ARTIFACT_COLLECTION,
                stage=RunStatus.COLLECTING_ARTIFACTS),
        Outcome.PASSED,
    ),
    # Agent-attributable failures are scored.
    (_result(RunStatus.TIMED_OUT, reason=R.AGENT_TIMEOUT), Outcome.AGENT_TIMEOUT),
    (_result(RunStatus.TIMED_OUT, reason=R.GRADER_TIMEOUT, stage=RunStatus.RUNNING_GRADER),
     Outcome.SOLUTION_TIMEOUT),
    (_result(RunStatus.FAILED_AGENT, reason=R.AGENT_ERROR), Outcome.AGENT_ERROR),
    (_result(RunStatus.FAILED_GRADER, reason=R.GRADER_ERROR, stage=RunStatus.RUNNING_GRADER),
     Outcome.POST_AGENT_ERROR),
    (_result(RunStatus.FAILED_SANDBOX, reason=R.SANDBOX_INFRASTRUCTURE,
             stage=RunStatus.CAPTURING_SOLUTION), Outcome.POST_AGENT_ERROR),
    # Nothing the agent could influence is scored.
    (_result(RunStatus.FAILED_BUILD, reason=R.SANDBOX_BUILD, stage=RunStatus.BUILDING_SANDBOX),
     Outcome.INFRASTRUCTURE_ERROR),
    (_result(RunStatus.FAILED_SANDBOX, reason=R.SETUP_COMMAND, stage=RunStatus.STARTING_SANDBOX),
     Outcome.INFRASTRUCTURE_ERROR),
    (_result(RunStatus.FAILED_SANDBOX, reason=R.SANDBOX_START, stage=RunStatus.RUNNING_GRADER),
     Outcome.INFRASTRUCTURE_ERROR),
    (_result(RunStatus.FAILED_AGENT, reason=R.AGENT_UNAVAILABLE), Outcome.AGENT_UNAVAILABLE),
    (_result(RunStatus.CANCELLED, reason=R.CANCELLED), Outcome.CANCELLED),
]  # fmt: skip


@pytest.mark.parametrize(("result", "expected"), CASES)
def test_classification(result: RunResult, expected: Outcome) -> None:
    assert classify(result, started=True) is expected


def test_runs_without_result_are_incomplete_or_not_started() -> None:
    assert classify(None, started=True) is Outcome.INCOMPLETE
    assert classify(None, started=False) is Outcome.NOT_STARTED


@pytest.mark.parametrize("reason", list(FailureReason))
@pytest.mark.parametrize("stage", list(RunStatus))
def test_every_reason_and_stage_has_a_consistent_outcome(
    reason: FailureReason, stage: RunStatus
) -> None:
    outcome = classify(_result(RunStatus.INTERNAL_ERROR, reason=reason, stage=stage), started=True)
    never_scored = {R.CANCELLED, R.AGENT_UNAVAILABLE, R.SANDBOX_START}
    if reason in never_scored or stage in {
        RunStatus.CREATED, RunStatus.VALIDATING, RunStatus.PREPARING_SOURCE,
        RunStatus.BUILDING_SANDBOX, RunStatus.STARTING_SANDBOX,
    }:  # fmt: skip
        assert outcome not in SCORED_OUTCOMES
    else:
        assert outcome in SCORED_OUTCOMES
