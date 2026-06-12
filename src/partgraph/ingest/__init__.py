"""Ingestion fetch helpers for PartGraph.

This package contains the network-facing download step
(:func:`partgraph.ingest.fetch.fetch_cdfer`). It deliberately exposes only the
fetch primitive; orchestration of fetch -> normalize -> load lives in the CLI
(``partgraph ingest jlcparts``) so the stages stay independently testable.
"""

from __future__ import annotations

__all__: list[str] = []
