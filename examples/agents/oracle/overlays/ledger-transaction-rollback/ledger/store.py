"""In-memory key-value store with nested transactions."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class InsufficientFundsError(Exception):
    """Raised when an account balance would become negative."""


class Store:
    """A key-value store whose changes can be grouped into atomic transactions."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._snapshots: list[dict[str, Any]] = []

    @property
    def depth(self) -> int:
        """Number of currently open transactions."""
        return len(self._snapshots)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        del self._data[key]

    def items(self) -> dict[str, Any]:
        return dict(self._data)

    @contextmanager
    def transaction(self) -> Iterator[Store]:
        """Run a block atomically.

        If the block raises, every change made inside it is undone and the exception
        propagates. Transactions may be nested; rolling back an inner transaction only
        undoes the changes made inside that inner block.
        """
        # Deep copy: values may be mutable (e.g. the transfer history list).
        self._snapshots.append(copy.deepcopy(self._data))
        try:
            yield self
        except Exception:
            # Restore *this* transaction's snapshot, then let the error propagate.
            self._data = self._snapshots[-1]
            raise
        finally:
            self._snapshots.pop()


def transfer(store: Store, source: str, target: str, amount: int) -> None:
    """Move ``amount`` from ``source`` to ``target`` and record it in the history."""
    with store.transaction():
        store.set(source, store.get(source, 0) - amount)
        store.set(target, store.get(target, 0) + amount)
        history = store.get("history")
        if history is None:
            history = []
            store.set("history", history)
        history.append((source, target, amount))
        if store.get(source) < 0:
            raise InsufficientFundsError(f"{source} cannot cover {amount}")
