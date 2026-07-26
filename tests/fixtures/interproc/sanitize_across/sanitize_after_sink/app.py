import os
from shlex import quote


def view(cmd):
    run(cmd)


def run(c):
    os.system("ls " + c)
    audit(quote(c))


def audit(c):
    return c
