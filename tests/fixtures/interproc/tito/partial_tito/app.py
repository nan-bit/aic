import os


def view(cmd):
    os.system(pick(cmd, "uptime"))


def pick(tainted, safe):
    return safe
