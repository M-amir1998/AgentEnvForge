"""Sliding-window statistics."""

from __future__ import annotations


def moving_average(values: list[float], window: int) -> list[float]:
    """Return the mean of every contiguous run of ``window`` values.

    ``moving_average([1, 2, 3, 4], 2)`` is ``[1.5, 2.5, 3.5]``.
    Raises ``ValueError`` if ``window`` is smaller than 1.
    """
    if window < 1:
        raise ValueError("window must be at least 1")
    averages = []
    for start in range(len(values) - window + 1):
        chunk = values[start : start + window]
        averages.append(sum(chunk) / window)
    return averages
