import os


def view(user):
    run("label", user)


def run(name, cmd):
    os.system(cmd)
