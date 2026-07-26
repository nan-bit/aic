import os


class Runner:
    @classmethod
    def run(cls, cmd):
        os.system(cmd)


def view(user):
    Runner.run(user)
