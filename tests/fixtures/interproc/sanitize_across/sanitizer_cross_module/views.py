from sanitize import scrub
from runner import run


def view(cmd):
    run(scrub(cmd))
