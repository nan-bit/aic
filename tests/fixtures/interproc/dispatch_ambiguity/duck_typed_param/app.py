import os


def danger(cmd):
    os.system(cmd)


def apply(fn, value):
    fn(value)


def view(user):
    apply(danger, user)
