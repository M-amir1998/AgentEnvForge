"""Dependency-free statistics for experiment results.

The methods are deliberately few and standard, and each is documented in
docs/architecture/experiments.md and ADR-0009:

* :func:`wilson_interval`: the Wilson score interval for a binomial proportion. It keeps good
  coverage for the small sample sizes typical of agent experiments, never leaves [0, 1], and
  has non-zero width at 0/n and n/n, unlike the normal ("Wald") approximation.
* :func:`pass_at_k`: the unbiased pass@k estimator of Chen et al. (2021), "Evaluating Large
  Language Models Trained on Code", computed with exact integer binomial coefficients.
* :func:`percentile`: linear interpolation between closest ranks (Hyndman & Fan type 7, the
  default of numpy and R), so results match common analysis tools.

All three assume repetitions are independent samples. Deterministic agents (scripts, or
models sampled greedily) repeat the same outcome, so their intervals describe nothing.
The reports say so.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist, fmean, median, stdev

DEFAULT_CONFIDENCE = 0.95


@dataclass(frozen=True, slots=True)
class Interval:
    """A closed interval ``[low, high]``."""

    low: float
    high: float


def wilson_interval(
    successes: int, trials: int, *, confidence: float = DEFAULT_CONFIDENCE
) -> Interval | None:
    """Return the Wilson score interval for ``successes`` out of ``trials``.

    ``None`` when ``trials`` is 0: with no observations there is no estimate to bound.
    """
    if trials < 0 or not 0 <= successes <= trials:
        raise ValueError(f"need 0 <= successes <= trials, got {successes}/{trials}")
    if not 0 < confidence < 1:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    if trials == 0:
        return None
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    proportion = successes / trials
    z_squared_over_n = z * z / trials
    denominator = 1 + z_squared_over_n
    centre = (proportion + z_squared_over_n / 2) / denominator
    half_width = (z / denominator) * math.sqrt(
        proportion * (1 - proportion) / trials + z_squared_over_n / (4 * trials)
    )
    # At 0/n the exact lower bound is 0 and at n/n the exact upper bound is 1; floating-point
    # evaluation can miss them by one ulp, which would exclude the point estimate itself.
    low = 0.0 if successes == 0 else max(0.0, centre - half_width)
    high = 1.0 if successes == trials else min(1.0, centre + half_width)
    return Interval(low, high)


def pass_at_k(samples: int, correct: int, k: int) -> float:
    """Unbiased estimate of the probability that at least one of ``k`` samples passes.

    Given ``samples`` attempts of which ``correct`` passed: ``1 - C(n - c, k) / C(n, k)``.
    """
    if not 0 <= correct <= samples:
        raise ValueError(f"need 0 <= correct <= samples, got {correct}/{samples}")
    if not 1 <= k <= samples:
        raise ValueError(f"need 1 <= k <= samples, got k={k}, samples={samples}")
    if samples - correct < k:
        return 1.0
    return 1.0 - math.comb(samples - correct, k) / math.comb(samples, k)


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q``-th percentile (0-100) of ``values`` with linear interpolation (type 7)."""
    if not values:
        raise ValueError("percentile of an empty sequence is undefined")
    if not 0 <= q <= 100:
        raise ValueError(f"q must be within [0, 100], got {q}")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass(frozen=True, slots=True)
class Distribution:
    """Summary of a sample of durations (or any non-negative quantity)."""

    count: int
    mean: float
    median: float
    p95: float
    minimum: float
    maximum: float
    stdev: float | None
    """Sample standard deviation; ``None`` for fewer than two values."""


def describe(values: Sequence[float]) -> Distribution | None:
    """Return summary statistics of ``values``, or ``None`` for an empty sample."""
    if not values:
        return None
    return Distribution(
        count=len(values),
        mean=fmean(values),
        median=median(values),
        p95=percentile(values, 95),
        minimum=min(values),
        maximum=max(values),
        stdev=stdev(values) if len(values) >= 2 else None,
    )
