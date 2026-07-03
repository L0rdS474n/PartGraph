"""Refresh stage for PartGraph time-sensitive data (issue #11).

This package holds the leaf link-rot checker
(:mod:`partgraph.refresh.links`) that underpins the ``partgraph refresh-links``
CLI command. The leaf depends only on injected seams
(``http_client`` / ``client`` / ``clock`` / ``sleep``), never on a real socket,
Dgraph connection or wall clock, so it stays hermetically unit-testable —
mirroring the embed pipeline's leaf discipline (ADR-0010). Orchestration
(paging, pacing, error rendering) lives in the CLI so the stages stay
independently testable.

This ``__init__`` is intentionally minimal (docstring + empty ``__all__``) so
``setuptools`` package discovery (``[tool.setuptools.packages.find] where =
["src"]``) picks the package up without exporting a public surface.
"""

from __future__ import annotations

__all__: list[str] = []
