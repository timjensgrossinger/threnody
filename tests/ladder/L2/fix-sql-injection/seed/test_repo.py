"""Exploit-style grader: crafted values must be treated as data, not SQL.

Also asserts normal behaviour and signatures, so deleting or crippling the
functions fails as hard as leaving the hole open.
"""
import sqlite3

import pytest

from repo import (
    add_user,
    connect,
    delete_by_name,
    find_by_name,
    find_by_name_and_email,
)


@pytest.fixture
def conn():
    c = connect()
    yield c
    c.close()


# --- functionality must survive -------------------------------------------------

def test_find_existing(conn):
    rows = find_by_name(conn, "alice")
    assert len(rows) == 1
    assert rows[0][1] == "alice"
    assert rows[0][2] == "a@example.com"


def test_find_missing_returns_empty(conn):
    assert find_by_name(conn, "nobody") == []


def test_find_by_name_and_email(conn):
    assert len(find_by_name_and_email(conn, "bob", "b@example.com")) == 1
    assert find_by_name_and_email(conn, "bob", "wrong@example.com") == []


def test_add_and_delete(conn):
    assert add_user(conn, "carol", "c@example.com") == 3
    assert len(find_by_name(conn, "carol")) == 1
    assert delete_by_name(conn, "carol") == 2


# --- the injection must be closed ----------------------------------------------

def test_tautology_does_not_dump_the_table(conn):
    # Classic ' OR '1'='1 -- if interpolated this returns every row.
    rows = find_by_name(conn, "' OR '1'='1")
    assert rows == [], "tautology returned rows: value was interpolated as SQL"


def test_tautology_on_second_predicate(conn):
    rows = find_by_name_and_email(conn, "bob", "' OR '1'='1")
    assert rows == [], "tautology returned rows: value was interpolated as SQL"


def test_comment_terminator_is_data(conn):
    assert find_by_name(conn, "alice' --") == []


def test_quote_in_value_is_stored_literally(conn):
    # A legitimate apostrophe must round-trip, not error and not be stripped.
    add_user(conn, "O'Brien", "ob@example.com")
    rows = find_by_name(conn, "O'Brien")
    assert len(rows) == 1
    assert rows[0][1] == "O'Brien"


def test_stacked_statement_cannot_drop_the_table(conn):
    # sqlite3.execute refuses multiple statements, so an interpolated payload
    # raises Warning/ProgrammingError; a parameterised query treats it as data.
    try:
        rows = delete_by_name(conn, "x'; DROP TABLE users; --")
    except (sqlite3.Warning, sqlite3.ProgrammingError, sqlite3.OperationalError) as exc:
        pytest.fail(f"payload reached the SQL parser: {exc!r}")
    assert rows == 2, "no row should have matched that name"
    # The table must still be queryable.
    assert len(find_by_name(conn, "alice")) == 1


def test_source_uses_parameter_placeholders():
    import pathlib

    src = pathlib.Path("repo.py").read_text()
    # The four query builders must bind parameters rather than format values in.
    assert "?" in src, "no parameter placeholders found"
    for bad in ("WHERE name = '%s'", "WHERE name = '{}'", "VALUES ('%s'"):
        assert bad not in src, f"string-formatted SQL still present: {bad}"
