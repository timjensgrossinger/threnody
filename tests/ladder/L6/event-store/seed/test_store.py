import pytest

from models import Event
from store import ConcurrencyError, EventStore


def ev(aid, kind, version, **payload):
    return Event(aggregate_id=aid, kind=kind, payload=payload, expected_version=version)


def test_append_and_replay():
    s = EventStore()
    s.append(ev('a', 'created', 0))
    s.append(ev('a', 'renamed', 1, name='x'))
    kinds = [e.kind for e in s.replay('a')]
    assert kinds == ['created', 'renamed']


def test_version_counts_events():
    s = EventStore()
    assert s.version('a') == 0
    s.append(ev('a', 'created', 0))
    assert s.version('a') == 1


def test_aggregates_are_isolated():
    s = EventStore()
    s.append(ev('a', 'created', 0))
    s.append(ev('b', 'created', 0))
    assert len(s.replay('a')) == 1
    assert len(s.replay('b')) == 1


def test_unknown_aggregate_replays_empty():
    assert EventStore().replay('nope') == []


def test_concurrency_conflict():
    s = EventStore()
    s.append(ev('a', 'created', 0))
    with pytest.raises(ConcurrencyError):
        s.append(ev('a', 'renamed', 0))


def test_concurrency_allows_correct_version():
    s = EventStore()
    s.append(ev('a', 'created', 0))
    s.append(ev('a', 'renamed', 1))
    assert s.version('a') == 2


def test_replay_returns_a_copy():
    s = EventStore()
    s.append(ev('a', 'created', 0))
    out = s.replay('a')
    out.clear()
    assert len(s.replay('a')) == 1
