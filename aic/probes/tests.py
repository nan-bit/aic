"""Test probe: which tests does a change put at risk?

This is the oldest and most legible use of impact analysis -- test selection.
Both Google and Meta run it at scale because running the whole suite on every
change stops being affordable long before a repo stops growing.

It is here to make a point: the engine underneath is not a security engine.
Swap the definition of *interesting* and the same dirty propagation answers a
completely different question.
"""

import os

from .base import Marker, Probe

TEST_FILE_HINTS = ("test_", "_test", "tests", "conftest")


class TestProbe(Probe):
    name = "tests"
    description = "test functions -- what a change forces you to re-run"

    def inspect(self, path, tree, facts):
        if not self._is_test_file(path):
            return
        for fn in facts.functions:
            if fn.context == "nested":
                continue
            leaf = fn.qualname.split(".")[-1]
            if leaf.startswith("test") or leaf == "setUp" or leaf == "setUpClass":
                yield Marker(fn.qualname, "test", f"{path}::{fn.qualname}", fn.line)

    @staticmethod
    def _is_test_file(path):
        parts = path.replace(os.sep, "/").split("/")
        stem = parts[-1][:-3] if parts[-1].endswith(".py") else parts[-1]
        if any(p in ("test", "tests") for p in parts[:-1]):
            return True
        return any(h in stem for h in TEST_FILE_HINTS)
