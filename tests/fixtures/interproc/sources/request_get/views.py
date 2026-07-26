import sqlite3


def view(request):
    uid = request.GET["id"]
    sqlite3.connect(":memory:").cursor().execute("SELECT * FROM u WHERE id=" + uid)
