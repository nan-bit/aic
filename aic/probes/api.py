"""API probe: the public surface.

Answers "I changed this file -- whose contract might I have broken?"

Shaped very differently from the security probe on purpose. Security markers are
sparse and cluster in a few files; public-API markers are dense and spread across
almost every module. If the incremental engine only worked for sparse markers it
would be a security tool wearing a platform costume.
"""

from .base import Marker, Probe

# Decorators that make a function part of an externally observed contract even
# if the name suggests otherwise.
CONTRACT_DECORATORS = ("property", "abstractmethod", "overload", "staticmethod", "classmethod")


class ApiProbe(Probe):
    name = "api"
    description = "public functions and methods -- the surface a caller can break against"

    def inspect(self, path, tree, facts):
        for fn in facts.functions:
            if fn.context == "nested":
                continue
            decorated = any(
                any(d.endswith(cd) or d.startswith(cd) for cd in CONTRACT_DECORATORS)
                for d in fn.decorators
            )
            if not fn.public and not decorated:
                continue

            sig = f"{fn.qualname}({', '.join(fn.args)})"
            if fn.returns:
                sig += f" -> {fn.returns}"
            kind = "public-api" if fn.context == "module" else "public-method"
            yield Marker(fn.qualname, kind, sig, fn.line)
