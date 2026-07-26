import sqlite3


class Repo:
    def lookup(self, q):
        sqlite3.connect(":memory:").cursor().execute("SELECT " + q)


def view(uid):
    Repo().lookup(uid)
