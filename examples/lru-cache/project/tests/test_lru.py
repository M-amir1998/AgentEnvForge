from cache import LRUCache


def test_new_cache_is_empty() -> None:
    cache = LRUCache(3)
    assert len(cache) == 0
    assert cache.get("missing") is None


def test_reading_an_entry_protects_it_from_eviction() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)  # "b" is now the least recently used entry
    assert "a" in cache
    assert "b" not in cache
    assert "c" in cache
