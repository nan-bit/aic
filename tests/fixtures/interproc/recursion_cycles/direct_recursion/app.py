import os


def view(cmd):
    walk(cmd, 3)


def walk(c, depth):
    if depth == 0:
        os.system("ls " + c)
        return
    walk(c, depth - 1)
