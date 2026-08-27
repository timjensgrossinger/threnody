"""Tiny user repository over sqlite3."""
import sqlite3


def connect():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
    conn.execute("INSERT INTO users (name, email) VALUES ('alice', 'a@example.com')")
    conn.execute("INSERT INTO users (name, email) VALUES ('bob', 'b@example.com')")
    conn.commit()
    return conn


def find_by_name(conn, name):
    cur = conn.execute("SELECT id, name, email FROM users WHERE name = '%s'" % name)
    return cur.fetchall()


def find_by_name_and_email(conn, name, email):
    cur = conn.execute(
        "SELECT id, name, email FROM users WHERE name = '{}' AND email = '{}'".format(
            name, email
        )
    )
    return cur.fetchall()


def add_user(conn, name, email):
    conn.execute(
        "INSERT INTO users (name, email) VALUES ('%s', '%s')" % (name, email)
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def delete_by_name(conn, name):
    conn.execute("DELETE FROM users WHERE name = '%s'" % name)
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
