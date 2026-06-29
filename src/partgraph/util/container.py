"""Container-engine detection for engine-agnostic Docker/Podman support.

This is a **leaf** module: it depends only on the Python standard library
(:mod:`os`, :mod:`shutil`). It must never import ``partgraph.cli`` or any of the
embed/query/load layers, so both the CLI and the integration tests can use it
freely without import cycles.

Why this exists
---------------
Every PartGraph container/compose invocation used to hard-code ``docker``. On a
host that ships only Podman (no ``docker`` shim) that fails with
``FileNotFoundError``. This module resolves the engine once, so ``cli.py`` and
the integration tests build their argv from whatever is actually installed.

Selection policy
----------------
1. **Explicit override** — if ``PARTGRAPH_CONTAINER_ENGINE`` is set to a
   non-empty value, that executable is used (after a PATH check). A value that
   is not on PATH raises :class:`ContainerEngineError` rather than silently
   falling back, so a typo never picks the wrong engine.
2. **Auto-detection (podman-first)** — otherwise the first of
   ``("podman", "docker")`` found on PATH wins. Podman is preferred because:
   - it is equally correct when only one engine is installed;
   - on a host where ``docker`` is a Podman shim it uses the real engine
     directly, skipping the emulation layer and its stderr noise;
   - it selects the rootless engine in the rare both-installed case.
   The override above is the escape hatch for anyone who wants ``docker``.
3. **Neither present** — raise :class:`ContainerEngineError` naming both
   candidates so the user knows what to install.

The compose prefix is the v2 plugin form ``<engine> compose`` (Docker Compose
v2 / Podman 4.1+), never the legacy ``docker-compose`` standalone binary.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping

__all__ = [
    "ENGINE_ENV_VAR",
    "ContainerEngineError",
    "compose_command",
    "detect_engine",
    "engine_command",
]

#: Environment variable that forces a specific container engine, overriding
#: auto-detection (e.g. ``PARTGRAPH_CONTAINER_ENGINE=docker``).
ENGINE_ENV_VAR = "PARTGRAPH_CONTAINER_ENGINE"

#: Auto-detection order. Podman first — see the module docstring for why.
_PREFERRED_ENGINES: tuple[str, ...] = ("podman", "docker")


class ContainerEngineError(RuntimeError):
    """Raised when no usable container engine can be resolved.

    Either ``PARTGRAPH_CONTAINER_ENGINE`` points at a binary that is not on
    PATH, or neither podman nor docker is installed.
    """


def detect_engine(
    *,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the container engine executable name (``"podman"``/``"docker"``).

    Args:
        which: PATH-lookup callable (injected so tests stay hermetic); defaults
            to :func:`shutil.which`.
        environ: Environment mapping (injected for tests); defaults to
            :data:`os.environ`.

    Returns:
        The engine command name to invoke (the override value verbatim when set,
        otherwise the first preferred engine found on PATH).

    Raises:
        ContainerEngineError: If the override is set but missing from PATH, or
            if no preferred engine is installed.
    """
    if environ is None:
        environ = os.environ

    override = (environ.get(ENGINE_ENV_VAR) or "").strip()
    if override:
        if which(override):
            return override
        raise ContainerEngineError(
            f"{ENGINE_ENV_VAR}={override!r} but {override!r} was not found on "
            f"PATH. Install it, or set {ENGINE_ENV_VAR} to one of: "
            f"{', '.join(_PREFERRED_ENGINES)}."
        )

    for candidate in _PREFERRED_ENGINES:
        if which(candidate):
            return candidate

    raise ContainerEngineError(
        "No container engine found on PATH. PartGraph needs "
        f"{' or '.join(_PREFERRED_ENGINES)} (podman is preferred). Install one, "
        f"or set {ENGINE_ENV_VAR} to its executable name."
    )


def compose_command(
    *,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the compose command prefix, e.g. ``["podman", "compose"]``.

    Callers append their own arguments (``-f <file> up -d`` …). Uses the v2
    plugin form ``<engine> compose``.
    """
    return [detect_engine(which=which, environ=environ), "compose"]


def engine_command(
    *,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the bare engine command prefix, e.g. ``["docker"]``.

    Used for direct engine sub-commands such as ``port``/``ps``/``inspect``
    (mainly in the integration tests).
    """
    return [detect_engine(which=which, environ=environ)]
