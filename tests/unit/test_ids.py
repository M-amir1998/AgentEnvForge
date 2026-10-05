"""Identifier generation."""

from __future__ import annotations

import re
import time

import pytest

from agentenvforge.domain.ids import new_id, new_run_id

_RUN_ID = re.compile(r"^run_[0-9a-hjkmnp-tv-z]{26}$")


def test_run_id_format_is_docker_and_filename_safe() -> None:
    assert _RUN_ID.match(new_run_id())


def test_ids_are_unique() -> None:
    ids = {new_run_id() for _ in range(10_000)}
    assert len(ids) == 10_000


def test_ids_sort_by_creation_time() -> None:
    first = new_run_id()
    time.sleep(0.002)
    second = new_run_id()
    assert first < second


@pytest.mark.parametrize("prefix", ["", "Run", "run-1", "x" * 17])
def test_invalid_prefix_is_rejected(prefix: str) -> None:
    with pytest.raises(ValueError, match="invalid id prefix"):
        new_id(prefix)
