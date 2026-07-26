import os


def view(user):
    run("uptime", user)


def run(cmd, name):
    os.system(cmd)
