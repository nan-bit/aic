import os
from shlex import quote


def view(name, cmd):
    safe_name = quote(name)
    run(safe_name, cmd)


def run(a, b):
    os.system("ls " + b)
