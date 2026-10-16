"""Bounded local execution of many independent work items (ADR-0008).

``workers`` threads each pull the next item from a shared, lazily consumed iterator, so no
more than ``workers`` items are ever in flight (backpressure: 10, 100 or 10,000 planned
runs create at most ``workers`` active runs, containers and model calls). A failure inside
one item is captured and reported for that item only (failure isolation).

All bookkeeping happens on the calling (main) thread: workers only hand completed items back
through a queue. The main thread therefore stays responsive to signals, which Python only
delivers to it, and is the only writer of progress state.

Cancellation is cooperative. Once the token is set, workers take no new items; items in
flight are expected to observe the same token and finish promptly (runs end CANCELLED after
their normal cleanup), and :meth:`BoundedScheduler.run` returns when they have.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from agentenvforge.cancellation import CancellationToken

logger = logging.getLogger(__name__)

_POLL_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class Completed[T, R]:
    """One finished work item: its result, or the exception it raised."""

    item: T
    result: R | None
    error: BaseException | None


class BoundedScheduler:
    """Runs work items with at most ``workers`` executing at the same time."""

    def __init__(self, workers: int) -> None:
        if workers < 1:
            raise ValueError(f"workers must be at least 1, got {workers}")
        self.workers = workers

    def run[T, R](
        self,
        items: Iterable[T],
        work: Callable[[T], R],
        *,
        cancellation: CancellationToken,
        on_start: Callable[[T], None] | None = None,
        on_complete: Callable[[Completed[T, R]], None],
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        """Execute ``work(item)`` for items until they run out or cancellation is requested.

        ``on_start`` runs in the worker thread just before ``work`` (keep it tiny and
        thread-safe); ``on_complete`` and ``on_tick`` run on the calling thread. If either
        raises, cancellation is requested, in-flight items are drained, and the error is
        re-raised: a failing main thread must not leave runs executing unattended.
        """
        source: Iterator[T] = iter(items)
        source_lock = threading.Lock()
        completions: queue.Queue[Completed[T, R]] = queue.Queue()

        def take_next() -> list[T]:
            """Zero or one item; empty once the items are exhausted or cancelled."""
            with source_lock:
                return [] if cancellation.cancelled else list(itertools.islice(source, 1))

        def worker_loop() -> None:
            while batch := take_next():
                item = batch[0]
                try:
                    if on_start is not None:
                        on_start(item)
                    outcome: Completed[T, R] = Completed(item, work(item), None)
                # Failure isolation: whatever one item raises is reported for that item
                # only; the worker continues with the next one.
                except BaseException as error:  # noqa: BLE001 - delivered via the queue
                    outcome = Completed(item, None, error)
                completions.put(outcome)

        threads = [
            threading.Thread(target=worker_loop, name=f"experiment-worker-{index}", daemon=True)
            for index in range(self.workers)
        ]
        for thread in threads:
            thread.start()

        failure: BaseException | None = None
        while any(thread.is_alive() for thread in threads) or not completions.empty():
            try:
                completed = completions.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                completed = None
            try:
                if completed is not None:
                    on_complete(completed)
                if on_tick is not None:
                    on_tick()
            except BaseException as error:  # re-raised after draining
                if failure is None:
                    failure = error
                    cancellation.cancel(f"internal error: {type(error).__name__}: {error}")
                    logger.exception("experiment bookkeeping failed; cancelling active runs")
        for thread in threads:
            thread.join()
        if failure is not None:
            raise failure
