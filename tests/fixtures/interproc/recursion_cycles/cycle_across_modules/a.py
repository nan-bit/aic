import b


def view(cmd):
    step(cmd, 2)


def step(c, n):
    if n == 0:
        b.finish(c)
        return
    b.bounce(c, n - 1)
