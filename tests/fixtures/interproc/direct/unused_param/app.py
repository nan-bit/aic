import sqlite3


def view(uid):
    lookup(uid, "admin")


def lookup(unused, name):
    sqlite3.connect(":memory:").cursor().execute("SELECT * FROM u WHERE n=" + name)
