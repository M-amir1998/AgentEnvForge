"""Hidden grader tests for the off-by-one task."""

import pytest
from stats import moving_average


@pytest.mark.parametrize(
    ("values", "window", "expected"),
    [
        ([1, 2, 3, 4], 2, [1.5, 2.5, 3.5]),
        ([5, 5, 5], 3, [5.0]),
        ([1, 2, 3], 1, [1.0, 2.0, 3.0]),
        ([2, 4], 3, []),
        ([], 1, []),
        ([1, 3, 5, 7, 9], 4, [4.0, 6.0]),
    ],
)
def test_moving_average(values: list[float], window: int, expected: list[float]) -> None:
    assert moving_average(values, window) == expected


def test_invalid_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="window"):
        moving_average([1, 2], 0)
