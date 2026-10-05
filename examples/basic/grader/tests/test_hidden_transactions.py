"""Hidden grader tests. Only ever copied into the grading sandbox, never shown to agents."""

import pytest
from ledger import InsufficientFundsError, Store, transfer


class Boom(Exception):
    pass


def test_exception_propagates_out_of_transaction() -> None:
    store = Store()
    with pytest.raises(Boom), store.transaction():
        raise Boom


def test_rollback_restores_overwritten_and_deleted_keys() -> None:
    store = Store()
    store.set("kept", 1)
    store.set("removed", 2)
    with pytest.raises(Boom), store.transaction():
        store.set("kept", 100)
        store.delete("removed")
        store.set("added", 3)
        raise Boom
    assert store.items() == {"kept": 1, "removed": 2}


def test_inner_rollback_keeps_outer_changes() -> None:
    store = Store()
    with store.transaction():
        store.set("outer", 1)
        with pytest.raises(Boom), store.transaction():
            store.set("inner", 2)
            raise Boom
        assert store.get("outer") == 1
        assert store.get("inner") is None
    assert store.items() == {"outer": 1}


def test_rollback_restores_mutable_values() -> None:
    store = Store()
    store.set("history", [("x", "y", 1)])
    with pytest.raises(Boom), store.transaction():
        store.get("history").append(("y", "x", 1))
        raise Boom
    assert store.get("history") == [("x", "y", 1)]


def test_failed_transfer_is_not_recorded_in_history() -> None:
    store = Store()
    store.set("alice", 5)
    transfer(store, "alice", "bob", 5)
    with pytest.raises(InsufficientFundsError):
        transfer(store, "alice", "bob", 1)
    assert store.get("history") == [("alice", "bob", 5)]
    assert (store.get("alice"), store.get("bob")) == (0, 5)


def test_depth_returns_to_zero_after_failure() -> None:
    store = Store()
    with pytest.raises(Boom), store.transaction(), store.transaction():
        assert store.depth == 2
        raise Boom
    assert store.depth == 0


def test_successful_nested_transfers_commit() -> None:
    store = Store()
    store.set("alice", 10)
    with store.transaction():
        transfer(store, "alice", "bob", 3)
        transfer(store, "bob", "carol", 1)
    assert (store.get("alice"), store.get("bob"), store.get("carol")) == (7, 2, 1)
