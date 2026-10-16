"""Removing machine-specific paths from exported results.

Result files are meant to be shared and committed, so they must not reveal user names or
directory layouts. Sanitisation is *structural*: it is applied to every string inside the
records before they are serialised (never to serialised text, where escaping would hide
paths), and it knows each path in its native and POSIX spelling, case-insensitively on
Windows. Paths are rewritten, most specific first, to:

* the experiment's directory  -> a path relative to it (``.``),
* ``AGENTENV_HOME``           -> ``$AGENTENV_HOME``,
* the user's home directory   -> ``~``.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path


class PathSanitizer:
    """Rewrites known path prefixes inside arbitrary nested data."""

    def __init__(self, *, experiment_root: Path | None, agentenv_home: Path | None) -> None:
        replacements: list[tuple[Path, str]] = []
        if experiment_root is not None:
            replacements.append((experiment_root, "."))
        if agentenv_home is not None:
            replacements.append((agentenv_home, "$AGENTENV_HOME"))
        replacements.append((Path.home(), "~"))
        flags = re.IGNORECASE if sys.platform == "win32" else 0
        # Only absolute spellings: a bare relative name such as "benchmarks" would also match
        # unrelated words. Longest first, so nested directories map to the most specific one.
        spellings = {
            (spelling, replacement)
            for path, replacement in replacements
            for spelling in (str(path.resolve()), path.resolve().as_posix())
        }
        self._patterns: list[tuple[re.Pattern[str], str]] = [
            (re.compile(re.escape(spelling), flags), replacement)
            for spelling, replacement in sorted(spellings, key=lambda s: len(s[0]), reverse=True)
        ]

    def text(self, value: str) -> str:
        """Sanitise one string."""
        for pattern, replacement in self._patterns:
            value = pattern.sub(replacement.replace("\\", "\\\\"), value)
        return value

    def data(self, value: object) -> object:
        """Sanitise every string in nested dicts, lists and tuples."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(str(key)): self.data(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.data(item) for item in value]
        return value
