import os
from shlex import quote


def view(cmd):
    run(scrub(cmd))


def scrub(c):
    return quote(c)


def run(c):
    os.system("ls " + c)
