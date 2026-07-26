import sqlite3
from shlex import quote


def view(uid):
    sqlite3.connect(":memory:").cursor().execute(clean(uid))


def clean(u):
    return quote(u)
