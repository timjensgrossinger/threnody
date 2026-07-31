import pytest

from solution import LRUCache


def test_get_and_put():
    c = LRUCache(2)
    c.put('a', 1)
    assert c.get('a') == 1


def test_missing_returns_none():
    assert LRUCache(2).get('nope') is None


def test_evicts_least_recently_used():
    c = LRUCache(2)
    c.put('a', 1)
    c.put('b', 2)
    c.put('c', 3)
    assert c.get('a') is None
    assert c.get('b') == 2
    assert c.get('c') == 3


def test_get_refreshes_recency():
    c = LRUCache(2)
    c.put('a', 1)
    c.put('b', 2)
    assert c.get('a') == 1
    c.put('c', 3)
    assert c.get('b') is None
    assert c.get('a') == 1


def test_overwrite_updates_value():
    c = LRUCache(2)
    c.put('a', 1)
    c.put('a', 9)
    assert c.get('a') == 9


def test_zero_capacity_stores_nothing():
    c = LRUCache(0)
    c.put('a', 1)
    assert c.get('a') is None
