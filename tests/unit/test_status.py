"""The run lifecycle state machine."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentenvforge.domain.status import (
    ACTIVE_SEQUENCE,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidTransitionError,
    RunStateMachine,
    RunStatus,
    can_transition,
)


def test_happy_path_visits_every_active_state_in_order() -> None:
    machine = RunStateMachine()
    for status in ACTIVE_SEQUENCE[1:]:
        machine.transition(status)
    machine.transition(RunStatus.COMPLETED)
    assert [change.status for change in machine.history] == [*ACTIVE_SEQUENCE, RunStatus.COMPLETED]


def test_backwards_transition_is_rejected() -> None:
    machine = RunStateMachine()
    machine.transition(RunStatus.VALIDATING)
    with pytest.raises(InvalidTransitionError, match="validating -> created"):
        machine.transition(RunStatus.CREATED)


def test_terminal_state_requires_cleanup_first() -> None:
    machine = RunStateMachine()
    machine.transition(RunStatus.VALIDATING)
    with pytest.raises(InvalidTransitionError):
        machine.transition(RunStatus.FAILED_VALIDATION)


@pytest.mark.parametrize("status", sorted(set(ACTIVE_SEQUENCE) - {RunStatus.CLEANING_UP}))
def test_every_active_state_can_abort_into_cleanup(status: RunStatus) -> None:
    assert can_transition(status, RunStatus.CLEANING_UP)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
def test_terminal_states_are_absorbing(terminal: RunStatus) -> None:
    assert ALLOWED_TRANSITIONS[terminal] == frozenset()
    assert terminal.is_terminal
    assert terminal.is_failure == (terminal is not RunStatus.COMPLETED)


@given(st.lists(st.sampled_from(list(RunStatus)), max_size=40))
def test_random_transition_attempts_preserve_invariants(attempts: list[RunStatus]) -> None:
    """Whatever sequence of transitions is attempted, only legal ones take effect,
    and a terminal state can only ever be reached through CLEANING_UP."""
    machine = RunStateMachine()
    for requested in attempts:
        legal = can_transition(machine.current, requested)
        try:
            machine.transition(requested)
        except InvalidTransitionError:
            assert not legal
        else:
            assert legal
    statuses = [change.status for change in machine.history]
    for previous, current in itertools.pairwise(statuses):
        assert current in ALLOWED_TRANSITIONS[previous]
        if current.is_terminal:
            assert previous is RunStatus.CLEANING_UP
    assert sum(status.is_terminal for status in statuses) <= 1
