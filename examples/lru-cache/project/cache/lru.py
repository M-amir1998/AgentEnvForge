"""A least-recently-used (LRU) cache."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from typing import Any


class LRUCache:
    """A mapping with a fixed capacity that evicts the least recently used entry.

    Reading an entry with :meth:`get` and writing it with :meth:`put` both count as
    using it. Membership tests (``key in cache``), :func:`len` and :meth:`keys` do not.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._entries: OrderedDict[Hashable, Any] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def get(self, key: Hashable, default: Any = None) -> Any:
        """Return the value stored for ``key``, or ``default`` if it is not cached."""
        if key not in self._entries:
            return default
        return self._entries[key]

    def put(self, key: Hashable, value: Any) -> None:
        """Insert or update ``key``. When the cache is full, the least recently used entry
        is evicted to make room for a new key."""
        if len(self._entries) >= self.capacity:
            self._entries.popitem(last=False)
        self._entries[key] = value

    def keys(self) -> list[Hashable]:
        """Cached keys, from least to most recently used."""
        return list(self._entries)
