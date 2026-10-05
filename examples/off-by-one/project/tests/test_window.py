from stats import moving_average


def test_documented_example() -> None:
    assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
