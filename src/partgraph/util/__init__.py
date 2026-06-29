"""PartGraph utility sub-package.

Re-exports the adaptive resource controller and the container-engine helpers so
callers can ``from partgraph.util import ResourceController`` /
``from partgraph.util import compose_command`` without reaching into the
submodules. Both ``partgraph.util.resources`` and ``partgraph.util.container``
are leaf modules (stdlib + optional psutil only), so importing this package
never pulls in the embed/query/load/cli layers.
"""

from __future__ import annotations

from partgraph.util.container import (
    ENGINE_ENV_VAR,
    ContainerEngineError,
    compose_command,
    detect_engine,
    engine_command,
)
from partgraph.util.resources import (
    RegulationDirective,
    ResourceController,
    SystemSnapshot,
    get_system_reader,
)

__all__ = [
    "ENGINE_ENV_VAR",
    "ContainerEngineError",
    "RegulationDirective",
    "ResourceController",
    "SystemSnapshot",
    "compose_command",
    "detect_engine",
    "engine_command",
    "get_system_reader",
]
