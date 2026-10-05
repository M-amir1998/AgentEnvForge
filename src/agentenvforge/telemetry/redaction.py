"""Secret redaction.

Secrets explicitly injected into a run (``secrets:`` in an agent spec) are registered with
a :class:`Redactor`. Every artifact and every recorded command output passes through it
before being persisted, so an agent echoing its API key does not leak it into stored
results. Redaction is a safety net for *known* secret values; it cannot detect secrets
it was never told about, which is why secrets are never baked into images or tasks.
"""

from __future__ import annotations

from collections.abc import Iterable

REDACTED = "***REDACTED***"


class Redactor:
    """Replaces registered secret values in text and bytes."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        # Longest first, so a secret containing another secret is fully masked.
        self._secrets = sorted({value for value in secrets if value}, key=len, reverse=True)

    @property
    def active(self) -> bool:
        """True when at least one secret is registered."""
        return bool(self._secrets)

    def redact_text(self, text: str) -> str:
        """Return ``text`` with every secret replaced by :data:`REDACTED`."""
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def redact_bytes(self, data: bytes) -> bytes:
        """Return ``data`` with every UTF-8-encoded secret replaced."""
        for secret in self._secrets:
            data = data.replace(secret.encode("utf-8"), REDACTED.encode("utf-8"))
        return data
