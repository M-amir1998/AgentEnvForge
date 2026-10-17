"""Expanding an experiment into its run matrix (pure, deterministic).

The matrix is ``repetitions x tasks x agents``, ordered in *interleaved rounds*: every
(task, agent) combination runs once before any runs a second time::

    repetition 1: (t1, a1) (t1, a2) (t2, a1) (t2, a2)
    repetition 2: (t1, a1) (t1, a2) (t2, a1) (t2, a2)

Machine conditions drift during long experiments (thermal throttling, background load, a
model server warming its cache). Interleaving spreads that drift evenly over agents and
tasks; grouping all runs of one agent together would bias the comparison against whichever
agent happened to run under the worst conditions.

Within each (repetition, task) block the agent order is additionally shuffled with a seeded
generator, so no agent is systematically first (e.g. always paying for a model load) or
always paired with the same concurrent neighbour. The order is deterministic for a given
``order_seed`` and is recorded in the experiment manifest.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunCombination:
    """One planned run of an experiment."""

    position: int
    """0-based position in matrix order; unique within the experiment."""
    repetition: int
    """1-based repetition number."""
    task_id: str
    agent_id: str
    seed: int
    """Seed offered to the agent (agents without seeding support ignore it)."""


def derive_seed(base_seed: int, repetition: int) -> int:
    """Seed of the ``repetition``-th (1-based) attempt: ``base_seed + repetition - 1``.

    Every (task, agent) pair receives the same seed in the same repetition. Seeds therefore
    differ between repetitions (so stochastic agents produce independent samples) while the
    comparison between agents within a repetition stays paired.
    """
    return base_seed + repetition - 1


def expand_matrix(
    task_ids: Sequence[str],
    agent_ids: Sequence[str],
    repetitions: int,
    *,
    base_seed: int = 0,
    order_seed: int | None = None,
) -> Iterator[RunCombination]:
    """Yield every planned run in interleaved-round order (see module docstring).

    ``order_seed`` (default: ``base_seed``) seeds the agent-order shuffle within blocks.
    """
    if repetitions < 1:
        raise ValueError(f"repetitions must be at least 1, got {repetitions}")
    shuffle_seed = base_seed if order_seed is None else order_seed
    position = 0
    for repetition in range(1, repetitions + 1):
        seed = derive_seed(base_seed, repetition)
        for task_id in task_ids:
            block = list(agent_ids)
            # Not for security: a reproducible permutation of the agent order.
            random.Random(f"{shuffle_seed}/{repetition}/{task_id}").shuffle(block)  # noqa: S311
            for agent_id in block:
                yield RunCombination(position, repetition, task_id, agent_id, seed)
                position += 1


def matrix_size(task_count: int, agent_count: int, repetitions: int) -> int:
    """Number of runs :func:`expand_matrix` yields."""
    return task_count * agent_count * repetitions
