"""Probe contract.

A probe reads the facts of a single file and emits markers. Everything after
that -- reachability, dirty propagation, fanout -- is probe-agnostic, which is
the whole point: the incremental machinery is not a security feature.
"""


class Marker:
    """A point of interest attached to a function (or to the module, via '')."""

    __slots__ = ("qualname", "kind", "detail", "line")

    def __init__(self, qualname, kind, detail, line):
        self.qualname = qualname
        self.kind = kind        # short category, e.g. "sql", "public-api"
        self.detail = detail    # human-readable specifics
        self.line = line

    def as_row(self):
        return (self.qualname, self.kind, self.detail, self.line)


class Probe:
    name = ""
    description = ""

    def inspect(self, path, tree, facts):
        """Yield Markers for one file. Must be pure and side-effect free."""
        raise NotImplementedError
