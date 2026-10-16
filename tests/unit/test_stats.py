"""Statistics used in experiment reports, checked against reference values and brute force."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentenvforge.analysis.stats import describe, pass_at_k, percentile, wilson_interval

# --------------------------------------------------------------------- Wilson interval


@pytest.mark.parametrize(
    ("successes", "trials", "low", "high"),
    [
        # Reference values for the 95% Wilson score interval (e.g. Newcombe 1998, Table I).
        (0, 10, 0.0, 0.2775),
        (5, 10, 0.2366, 0.7634),
        (10, 10, 0.7225, 1.0),
        (1, 1, 0.2065, 1.0),
        (81, 263, 0.2553, 0.3662),
    ],
)
def test_wilson_matches_reference_values(
    successes: int, trials: int, low: float, high: float
) -> None:
    interval = wilson_interval(successes, trials)
    assert interval is not None
    assert interval.low == pytest.approx(low, abs=1e-4)
    assert interval.high == pytest.approx(high, abs=1e-4)


def test_wilson_is_undefined_without_trials() -> None:
    assert wilson_interval(0, 0) is None


def test_wilson_widens_with_confidence() -> None:
    narrow = wilson_interval(3, 10, confidence=0.80)
    wide = wilson_interval(3, 10, confidence=0.99)
    assert narrow is not None and wide is not None
    assert wide.low < narrow.low and wide.high > narrow.high


@pytest.mark.parametrize(("successes", "trials"), [(-1, 3), (4, 3), (0, -1)])
def test_wilson_rejects_impossible_counts(successes: int, trials: int) -> None:
    with pytest.raises(ValueError, match="successes"):
        wilson_interval(successes, trials)


@given(
    st.integers(min_value=1, max_value=500).flatmap(
        lambda n: st.tuples(st.integers(0, n), st.just(n))
    )
)
def test_wilson_contains_estimate_and_is_symmetric(pair: tuple[int, int]) -> None:
    successes, trials = pair
    interval = wilson_interval(successes, trials)
    mirrored = wilson_interval(trials - successes, trials)
    assert interval is not None and mirrored is not None
    assert 0.0 <= interval.low <= successes / trials <= interval.high <= 1.0
    assert interval.low == pytest.approx(1 - mirrored.high, abs=1e-12)
    assert interval.high == pytest.approx(1 - mirrored.low, abs=1e-12)
    assert interval.high - interval.low > 0  # non-zero width even at 0/n and n/n


# --------------------------------------------------------------------------- pass@k


@pytest.mark.parametrize(
    ("samples", "correct", "k", "expected"),
    [
        (5, 2, 1, 0.4),
        (5, 2, 2, 0.7),  # 1 - C(3,2)/C(5,2) = 1 - 3/10
        (5, 2, 4, 1.0),  # only 3 failures exist, so any 4 samples contain a success
        (10, 3, 4, 1 - 35 / 210),
        (5, 0, 5, 0.0),
        (3, 3, 1, 1.0),
    ],
)
def test_pass_at_k_known_values(samples: int, correct: int, k: int, expected: float) -> None:
    assert pass_at_k(samples, correct, k) == pytest.approx(expected)


@given(
    st.integers(1, 8).flatmap(lambda n: st.tuples(st.just(n), st.integers(0, n), st.integers(1, n)))
)
def test_pass_at_k_equals_brute_force_over_all_subsets(case: tuple[int, int, int]) -> None:
    """The estimator is exactly the fraction of k-subsets of the samples containing a pass."""
    samples, correct, k = case
    outcomes = [True] * correct + [False] * (samples - correct)
    subsets = list(itertools.combinations(outcomes, k))
    brute_force = sum(any(subset) for subset in subsets) / len(subsets)
    assert pass_at_k(samples, correct, k) == pytest.approx(brute_force)


@given(st.integers(1, 30).flatmap(lambda n: st.tuples(st.just(n), st.integers(0, n))))
def test_pass_at_1_is_the_success_rate_and_pass_at_k_is_monotone(case: tuple[int, int]) -> None:
    samples, correct = case
    assert pass_at_k(samples, correct, 1) == pytest.approx(correct / samples)
    values = [pass_at_k(samples, correct, k) for k in range(1, samples + 1)]
    assert values == sorted(values)


@pytest.mark.parametrize(("samples", "correct", "k"), [(3, 4, 1), (3, 1, 0), (3, 1, 4)])
def test_pass_at_k_rejects_invalid_arguments(samples: int, correct: int, k: int) -> None:
    with pytest.raises(ValueError):
        pass_at_k(samples, correct, k)


# ----------------------------------------------------------------------- percentiles


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        # Values agree with numpy.percentile (default "linear" method, Hyndman-Fan type 7).
        ([1, 2, 3, 4], 50, 2.5),
        ([1, 2, 3, 4], 95, 3.85),
        ([4, 1, 3, 2], 0, 1),
        ([4, 1, 3, 2], 100, 4),
        ([15, 20, 35, 40, 50], 40, 29.0),
        (list(range(1, 11)), 95, 9.55),
        ([7.5], 95, 7.5),
    ],
)
def test_percentile_linear_interpolation(values: list[float], q: float, expected: float) -> None:
    assert percentile(values, q) == pytest.approx(expected)


def test_percentile_rejects_empty_and_out_of_range() -> None:
    with pytest.raises(ValueError, match="empty"):
        percentile([], 50)
    with pytest.raises(ValueError, match="q must"):
        percentile([1.0], 101)


def test_describe_summarises_a_sample() -> None:
    summary = describe([3.0, 1.0, 2.0, 10.0])
    assert summary is not None
    assert (summary.count, summary.minimum, summary.maximum) == (4, 1.0, 10.0)
    assert summary.mean == pytest.approx(4.0)
    assert summary.median == pytest.approx(2.5)
    assert summary.p95 == pytest.approx(8.95)
    assert summary.stdev == pytest.approx(4.0825, abs=1e-4)
    single = describe([5.0])
    assert single is not None and single.stdev is None
    assert describe([]) is None
