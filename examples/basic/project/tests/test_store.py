import pytest
from ledger import InsufficientFundsError, Store, transfer


def test_committed_transaction_keeps_changes() -> None:
    store = Store()
    with store.transaction():
        store.set("a", 1)
    assert store.get("a") == 1


def test_failed_transfer_raises_and_keeps_balances() -> None:
    store = Store()
    store.set("alice", 10)
    store.set("bob", 0)
    with pytest.raises(InsufficientFundsError):
        transfer(store, "alice", "bob", 25)
    assert store.get("alice") == 10
    assert store.get("bob") == 0
