import sqlite3


def view(uid):
    lookup("admin")


def lookup(q):
    sqlite3.connect(":memory:").cursor().execute("SELECT * FROM u WHERE id=" + q)
