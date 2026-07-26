import os
from shlex import quote


def view(cmd):
    run(quote(cmd))


def run(c):
    os.system("ls " + c)
