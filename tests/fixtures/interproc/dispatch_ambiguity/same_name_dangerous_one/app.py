import os


class Safe:
    def run(self, cmd):
        return len(cmd)


class Dangerous:
    def run(self, cmd):
        os.system(cmd)


def view(user):
    Dangerous().run(user)
