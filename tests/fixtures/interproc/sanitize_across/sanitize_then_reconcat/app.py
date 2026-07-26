import os
from shlex import quote


def view(cmd):
    run(quote(cmd), cmd)


def run(safe, raw):
    os.system(safe + " " + raw)
