"""Hidden grader tests for the LRU cache task. Only ever copied into the grading sandbox.

Every test checks behaviour documented in the class docstring; nothing undocumented is graded.
"""

import random

import pytest
from cache import LRUCache


def test_updating_an_existing_key_at_capacity_does_not_evict() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    assert len(cache) == 2
    assert cache.get("a") == 10
    assert cache.get("b") == 2


def test_writing_an_entry_counts_as_using_it() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 3)  # "a" is now the most recently used entry
    cache.put("c", 4)
    assert cache.keys() == ["a", "c"]


def test_reading_an_entry_counts_as_using_it() -> None:
    cache = LRUCache(3)
    for key in "abc":
        cache.put(key, key.upper())
    cache.get("a")
    assert cache.keys() == ["b", "c", "a"]


def test_membership_and_len_do_not_count_as_use() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert "a" in cache
    assert len(cache) == 2
    cache.put("c", 3)
    assert cache.keys() == ["b", "c"]


def test_missing_key_returns_default_without_side_effects() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    assert cache.get("zzz", "fallback") == "fallback"
    assert cache.keys() == ["a"]


def test_capacity_one_keeps_only_the_latest_key() -> None:
    cache = LRUCache(1)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.keys() == ["b"]
    cache.put("b", 3)
    assert cache.get("b") == 3


@pytest.mark.parametrize("capacity", [0, -1])
def test_invalid_capacity_is_rejected(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity"):
        LRUCache(capacity)


def test_long_operation_sequence_matches_a_reference_model() -> None:
    """500 pseudo-random operations (fixed seed) compared with a list-based reference."""
    rng = random.Random(1234)  # noqa: S311 - a reproducible test sequence, not security
    cache = LRUCache(4)
    reference: list[tuple[str, int]] = []  # least recently used first
    for step in range(500):
        key = rng.choice("abcdefg")
        if rng.random() < 0.5:
            cache.put(key, step)
            reference = [(k, v) for k, v in reference if k != key] + [(key, step)]
            if len(reference) > 4:
                reference.pop(0)
        else:
            expected = next((v for k, v in reference if k == key), None)
            assert cache.get(key) == expected, f"step {step}: get({key!r})"
            if expected is not None:
                reference = [(k, v) for k, v in reference if k != key] + [(key, expected)]
        assert cache.keys() == [k for k, _ in reference], f"step {step}"
