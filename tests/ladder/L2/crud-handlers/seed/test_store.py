"""Grader for a plain CRUD store — the "can the cheap tier do boilerplate" probe."""
import pytest

from store import TaskStore


@pytest.fixture
def store():
    return TaskStore()


def test_create_assigns_incrementing_ids(store):
    a = store.create("first")
    b = store.create("second")
    assert a["id"] == 1 and b["id"] == 2
    assert a["title"] == "first" and a["done"] is False


def test_create_respects_done_flag(store):
    assert store.create("x", done=True)["done"] is True


def test_create_rejects_blank_title(store):
    with pytest.raises(ValueError):
        store.create("")
    with pytest.raises(ValueError):
        store.create("   ")


def test_get(store):
    created = store.create("hello")
    assert store.get(created["id"])["title"] == "hello"
    assert store.get(999) is None


def test_list_preserves_insertion_order(store):
    for title in ("a", "b", "c"):
        store.create(title)
    assert [r["title"] for r in store.list()] == ["a", "b", "c"]


def test_list_filters_by_done(store):
    store.create("open one")
    store.create("closed one", done=True)
    assert [r["title"] for r in store.list(done=True)] == ["closed one"]
    assert [r["title"] for r in store.list(done=False)] == ["open one"]
    assert len(store.list()) == 2


def test_update_changes_only_given_fields(store):
    created = store.create("old")
    updated = store.update(created["id"], done=True)
    assert updated["done"] is True
    assert updated["title"] == "old"
    assert store.update(created["id"], title="new")["title"] == "new"


def test_update_missing_returns_none(store):
    assert store.update(999, title="x") is None


def test_update_unknown_field_raises(store):
    created = store.create("t")
    with pytest.raises(KeyError):
        store.update(created["id"], nope=1)


def test_delete(store):
    created = store.create("bye")
    assert store.delete(created["id"]) is True
    assert store.delete(created["id"]) is False
    assert store.get(created["id"]) is None


def test_returned_records_do_not_alias_internal_state(store):
    created = store.create("careful")
    created["title"] = "mutated"
    assert store.get(1)["title"] == "careful"
    listed = store.list()
    listed[0]["done"] = True
    assert store.get(1)["done"] is False
