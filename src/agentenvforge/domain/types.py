"""Small shared type aliases."""

from __future__ import annotations

type JSONScalar = str | int | float | bool | None
"""Values allowed in free-form metadata maps (events, grader details).

Restricting metadata to scalars keeps events cheap to persist, index and render, and
prevents accidental embedding of large nested payloads; large data belongs in artifacts.
"""

type Metadata = dict[str, JSONScalar]
