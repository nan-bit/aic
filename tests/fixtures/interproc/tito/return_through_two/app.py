import sqlite3


def view(uid):
    sqlite3.connect(":memory:").cursor().execute(outer(uid))


def outer(a):
    return inner(a)


def inner(b):
    return "SELECT " + b
