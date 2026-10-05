"""Time helpers. All timestamps inside AgentEnvForge are timezone-aware UTC."""

from __future__ import annotations

import time
from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def monotonic_ms() -> float:
    """Return a monotonic timestamp in milliseconds, for measuring durations."""
    return time.monotonic() * 1000.0


def elapsed_ms(start_monotonic_ms: float) -> int:
    """Return whole milliseconds elapsed since ``start_monotonic_ms``."""
    return max(0, round(monotonic_ms() - start_monotonic_ms))
