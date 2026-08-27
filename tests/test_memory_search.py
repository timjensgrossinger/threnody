"""Tests for FTS-backed memory search."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.db import Database
from shared.memory import MemoryNotFoundError, memory_delete, memory_search, memory_set


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "memory-search.db")


def test_memory_search_set_find_delete(db) -> None:
    memory_set(
        "project",
        "cost_pattern:jwt_auth",
        "Used low-tier execute_subtask for JWT middleware",
        project_id="/tmp/demo",
        db=db,
    )
    hits = memory_search("jwt low tier", project_id="/tmp/demo", db=db)
    assert len(hits) == 1
    assert hits[0]["key"] == "cost_pattern:jwt_auth"
    assert "jwt" in hits[0]["snippet"].lower()

    memory_delete("project", "cost_pattern:jwt_auth", project_id="/tmp/demo", db=db)
    with pytest.raises(MemoryNotFoundError):
        memory_delete("project", "cost_pattern:jwt_auth", project_id="/tmp/demo", db=db)
    assert memory_search("jwt", project_id="/tmp/demo", db=db) == []


def test_memory_search_overwrite_updates_index(db) -> None:
    memory_set("global", "note", "alpha beta", db=db)
    memory_set("global", "note", "alpha gamma", db=db)
    hits = memory_search("gamma", scope="global", db=db)
    assert len(hits) == 1
    assert "gamma" in hits[0]["snippet"]


def test_memory_search_scope_isolation(db) -> None:
    memory_set("project", "secret-recipe", "cheap refactor playbook", project_id="/a", db=db)
    memory_set("project", "secret-recipe", "cheap refactor playbook", project_id="/b", db=db)
    hits_a = memory_search("refactor", project_id="/a", db=db)
    hits_b = memory_search("refactor", project_id="/b", db=db)
    assert len(hits_a) == 1
    assert len(hits_b) == 1
    assert hits_a[0]["project_id"] == "/a"
    assert hits_b[0]["project_id"] == "/b"


def test_rebuild_memory_fts(db) -> None:
    memory_set("global", "rebuild-me", "token discipline pattern", db=db)
    rebuilt = db.rebuild_memory_fts()
    assert rebuilt >= 1
    hits = memory_search("discipline", scope="global", db=db)
    assert len(hits) == 1


def test_memory_list_pagination(db) -> None:
    from shared.memory import memory_list

    for i in range(10):
        memory_set("global", f"item_{i:02d}", f"val_{i}", db=db)

    all_items = memory_list("global", db=db)
    assert len(all_items) == 10

    page1 = memory_list("global", limit=3, offset=0, db=db)
    assert len(page1) == 3
    assert [x["key"] for x in page1] == ["item_00", "item_01", "item_02"]

    page2 = memory_list("global", limit=3, offset=3, db=db)
    assert len(page2) == 3
    assert [x["key"] for x in page2] == ["item_03", "item_04", "item_05"]
