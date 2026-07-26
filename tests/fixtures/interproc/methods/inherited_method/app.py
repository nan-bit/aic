import sqlite3


class Base:
    def lookup(self, q):
        sqlite3.connect(":memory:").cursor().execute("SELECT " + q)


class Child(Base):
    pass


def view(uid):
    Child().lookup(uid)
