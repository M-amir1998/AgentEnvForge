"""Cooperative cancellation shared between threads.

A run executing in a worker thread cannot be interrupted with ``KeyboardInterrupt``: Python
delivers signals to the main thread only. Instead, the main thread calls
:meth:`CancellationToken.cancel`, and the code doing the work checks the token at safe
points: every lifecycle stage boundary, while waiting on a blocking call, and before each
sandbox command. Cancellation is therefore prompt but never abrupt, so the normal cleanup
path always runs.
"""

from __future__ import annotations

import threading

from agentenvforge.domain.failures import RunCancelledError


class CancellationToken:
    """A one-way, thread-safe flag with the reason it was set."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""
        self._lock = threading.Lock()

    def cancel(self, reason: str) -> None:
        """Request cancellation; the first reason is kept, later calls are no-ops."""
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._event.set()

    @property
    def cancelled(self) -> bool:
        """True once cancellation was requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str:
        """Why cancellation was requested (empty while not cancelled)."""
        return self._reason

    def raise_if_cancelled(self) -> None:
        """Raise :class:`RunCancelledError` if cancellation was requested."""
        if self._event.is_set():
            raise RunCancelledError(f"cancelled: {self._reason}", details={"reason": self._reason})

    def wait(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds; return True if cancellation was requested."""
        return self._event.wait(timeout)
