import os


def view(cmd):
    ping(cmd, 2)


def ping(c, n):
    if n == 0:
        os.system("ls " + c)
        return
    pong(c, n - 1)


def pong(c, n):
    ping(c, n - 1)
