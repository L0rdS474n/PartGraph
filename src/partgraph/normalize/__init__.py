"""Normalization stage for PartGraph ingestion.

Turns raw adapter rows into deterministic, source-stamped :class:`StagedPart`
records (see :mod:`partgraph.normalize.model`) and writes them to a JSONL
staging file (see :mod:`partgraph.normalize.run`). Unit parsing
(:mod:`partgraph.normalize.units`) and tag extraction
(:mod:`partgraph.normalize.tags`) are pure, locale-independent helpers so that
the staged output is byte-reproducible across runs and machines.
"""

from __future__ import annotations

__all__: list[str] = []
