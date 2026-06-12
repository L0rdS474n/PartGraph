"""Source adapters for PartGraph ingestion.

Currently provides the JLCPCB/LCSC SQLite adapter
(:mod:`partgraph.sources.jlcparts`), which introspects the on-disk schema and
yields normalized-ready rows. Adapters read their source strictly read-only and
never trust schema-derived identifiers without validation.
"""

from __future__ import annotations

__all__: list[str] = []
