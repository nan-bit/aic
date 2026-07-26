import sqlite3


def lookup(q):
    sqlite3.connect(":memory:").cursor().execute("SELECT * FROM u WHERE id=" + q)
