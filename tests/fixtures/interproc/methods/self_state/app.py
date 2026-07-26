import sqlite3


class Repo:
    def remember(self, q):
        self.query = q

    def run(self):
        sqlite3.connect(":memory:").cursor().execute(self.query)


def view(uid):
    r = Repo()
    r.remember(uid)
    r.run()
