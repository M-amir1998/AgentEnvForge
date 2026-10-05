"""Running blocking calls with a deadline.

Several boundaries (image builds, in-process agents) are blocking calls into code we do
not control. Python cannot forcibly stop a thread, so :func:`call_with_timeout` runs the
call in a daemon thread and *abandons* it on timeout. Callers must therefore pair it with
a hard stop of the underlying resource; e.g. after an agent times out, the orchestrator
destroys its sandbox, which makes any in-flight sandbox call fail promptly.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable

_POLL_INTERVAL_SECONDS = 0.2


class CallTimeoutError(TimeoutError):
    """The wrapped call did not finish within its deadline (it may still be running)."""


def call_with_timeout[T](function: Callable[[], T], timeout_seconds: float, *, name: str) -> T:
    """Return ``function()``'s result, or raise :class:`CallTimeoutError` after the deadline.

    Exceptions raised by ``function`` propagate unchanged (with their traceback). The wait
    polls in short intervals so Ctrl+C in the main thread is handled promptly on every
    platform.
    """
    outcome: list[T] = []
    failure: list[BaseException] = []

    def _target() -> None:
        try:
            outcome.append(function())
        except BaseException as error:  # noqa: BLE001 - re-raised in the calling thread
            failure.append(error)

    # Run inside a copy of the caller's context so log context (run_id, task_id) set via
    # contextvars is visible to log records emitted from the worker thread.
    context = contextvars.copy_context()
    worker = threading.Thread(target=context.run, args=(_target,), name=name, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout_seconds
    while worker.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CallTimeoutError(f"{name} did not finish within {timeout_seconds:g}s")
        worker.join(min(_POLL_INTERVAL_SECONDS, remaining))
    if failure:
        raise failure[0]
    return outcome[0]
