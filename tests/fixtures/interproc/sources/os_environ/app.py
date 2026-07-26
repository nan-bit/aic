import os


def run():
    target = os.environ["TARGET"]
    os.system("ping " + target)
