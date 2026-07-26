import sqlite3


class Base:
    def lookup(self, q):
        sqlite3.connect(":memory:").cursor().execute("SELECT " + q)


class Child(Base):
    def lookup(self, q):
        sqlite3.connect(":memory:").cursor().execute("SELECT 1")


def view(uid):
    Child().lookup(uid)
