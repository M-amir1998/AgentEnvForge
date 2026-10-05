"""Identifier generation.

Run, event and artifact IDs must be unique across concurrently executing processes and
machines without a coordinating service, and should sort by creation time so that
``runs list`` and log files are naturally ordered. A ULID (48-bit millisecond timestamp
plus 80 random bits, Crockford base32) satisfies both properties using only the stdlib.
"""

from __future__ import annotations

import re
import secrets
import time

_CROCKFORD_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_ULID_LENGTH = 26
_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9]{0,15}$")


def new_id(prefix: str) -> str:
    """Return a new identifier of the form ``<prefix>_<ulid>``, e.g. ``run_01j9...``.

    The lowercase alphabet keeps IDs valid as Docker container names and file names.
    """
    if not _PREFIX_PATTERN.match(prefix):
        raise ValueError(f"invalid id prefix {prefix!r}: expected lowercase letters/digits")
    timestamp_ms = time.time_ns() // 1_000_000
    value = (timestamp_ms << 80) | secrets.randbits(80)
    characters = []
    for _ in range(_ULID_LENGTH):
        characters.append(_CROCKFORD_ALPHABET[value & 0b11111])
        value >>= 5
    return f"{prefix}_{''.join(reversed(characters))}"


def new_run_id() -> str:
    """Return a new run identifier."""
    return new_id("run")


def new_event_id() -> str:
    """Return a new event identifier."""
    return new_id("evt")


def new_artifact_id() -> str:
    """Return a new artifact identifier."""
    return new_id("art")
