def execute(q):
    return q.upper()


def view(uid):
    execute("SELECT " + uid)
