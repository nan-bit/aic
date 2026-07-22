"""Probe registry.

To add a probe: implement `Probe.inspect` and add it below. There is
deliberately no plugin discovery, no entry points, and no config DSL --
generalize on the fourth probe, not the second.
"""

from .base import Marker, Probe
from .api import ApiProbe
from .security import SecurityProbe
from .tests import TestProbe

REGISTRY = {p.name: p for p in (SecurityProbe(), ApiProbe(), TestProbe())}
DEFAULT = "security"

__all__ = ["Marker", "Probe", "REGISTRY", "DEFAULT", "get"]


def get(name):
    try:
        return REGISTRY[name]
    except KeyError:
        raise SystemExit(
            f"unknown probe {name!r}; available: {', '.join(sorted(REGISTRY))}"
        )
