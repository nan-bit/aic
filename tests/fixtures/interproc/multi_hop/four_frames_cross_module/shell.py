import os


def stage_one(a):
    stage_two(a)


def stage_two(b):
    run(b)


def run(cmd):
    os.system("ping " + cmd)
