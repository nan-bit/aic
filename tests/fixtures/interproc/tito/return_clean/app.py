import sqlite3


def view(uid):
    sqlite3.connect(":memory:").cursor().execute(build(uid))


def build(u):
    return "SELECT * FROM u WHERE id=1"
