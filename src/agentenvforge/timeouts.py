"""Running blocking calls with a deadline.

Several boundaries (image builds, in-process agents) are blocking calls into code we do
not control. Python cannot forcibly stop a thread, so :func:`call_with_timeout` runs the
call in a daemon thread and *abandons* it on timeout or cancellation. Callers must therefore
pair it with a hard stop of the underlying resource; e.g. after an agent times out, the
orchestrator destroys its sandbox, which makes any in-flight sandbox call fail promptly.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable

from agentenvforge.cancellation import CancellationToken

_POLL_INTERVAL_SECONDS = 0.2


class CallTimeoutError(TimeoutError):
    """The wrapped call did not finish within its deadline (it may still be running)."""


def call_with_timeout[T](
    function: Callable[[], T],
    timeout_seconds: float,
    *,
    name: str,
    cancellation: CancellationToken | None = None,
    on_abandoned: Callable[[T], object] | None = None,
) -> T:
    """Return ``function()``'s result, or raise :class:`CallTimeoutError` after the deadline.

    If ``cancellation`` is requested while waiting, raises
    :class:`~agentenvforge.domain.failures.RunCancelledError` within one poll interval.
    Exceptions raised by ``function`` propagate unchanged (with their traceback). The wait
    polls in short intervals so Ctrl+C in the main thread is handled promptly on every
    platform.

    ``on_abandoned`` receives the result if ``function`` returns *after* the caller gave up
    (timeout or cancellation), e.g. to destroy a sandbox whose creation finished too late.
    """
    outcome: list[T] = []
    failure: list[BaseException] = []
    # `abandoned` and `outcome` are read and written under one lock, so that a late result
    # is handed to on_abandoned exactly once, by whichever side observes both conditions.
    handoff = threading.Lock()
    abandoned = False

    def _target() -> None:
        try:
            result = function()
        except BaseException as error:  # noqa: BLE001 - re-raised in the calling thread
            failure.append(error)
            return
        with handoff:
            outcome.append(result)
            late = abandoned
        if late and on_abandoned is not None:
            on_abandoned(result)

    # Run inside a copy of the caller's context so log context (run_id, task_id) set via
    # contextvars is visible to log records emitted from the worker thread.
    context = contextvars.copy_context()
    worker = threading.Thread(target=context.run, args=(_target,), name=name, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout_seconds
    try:
        while worker.is_alive():
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CallTimeoutError(f"{name} did not finish within {timeout_seconds:g}s")
            worker.join(min(_POLL_INTERVAL_SECONDS, remaining))
    except BaseException:
        with handoff:
            abandoned = True
            finished = list(outcome)  # the worker finished just before we gave up
        if finished and on_abandoned is not None:
            on_abandoned(finished[0])
        raise
    if failure:
        raise failure[0]
    return outcome[0]
