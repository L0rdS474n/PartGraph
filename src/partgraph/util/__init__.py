"""PartGraph utility sub-package.

Re-exports the adaptive resource controller so callers can ``from partgraph.util
import ResourceController`` without reaching into the ``resources`` submodule.
``partgraph.util.resources`` is a leaf module (stdlib + optional psutil only),
so importing this package never pulls in the embed/query/load/cli layers.
"""

from __future__ import annotations

from partgraph.util.resources import (
    RegulationDirective,
    ResourceController,
    SystemSnapshot,
    get_system_reader,
)

__all__ = [
    "RegulationDirective",
    "ResourceController",
    "SystemSnapshot",
    "get_system_reader",
]
