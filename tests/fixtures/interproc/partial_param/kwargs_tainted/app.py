import os


def view(user):
    run(name="label", cmd=user)


def run(name, cmd):
    os.system(cmd)
