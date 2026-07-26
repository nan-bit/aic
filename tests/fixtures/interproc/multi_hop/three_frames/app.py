import sqlite3


def view(uid):
    middle(uid)


def middle(v):
    inner(v)


def inner(q):
    sqlite3.connect(":memory:").cursor().execute("SELECT " + q)
