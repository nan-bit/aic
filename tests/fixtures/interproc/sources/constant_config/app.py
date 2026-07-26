import os

CONFIG = {"target": "localhost"}


def run():
    os.system("ping " + CONFIG["target"])
