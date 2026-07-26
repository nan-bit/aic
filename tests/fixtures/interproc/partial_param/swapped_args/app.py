import sqlite3


def view(uid):
    lookup("SELECT 1", uid)


def lookup(query, unused):
    sqlite3.connect(":memory:").cursor().execute(query)
