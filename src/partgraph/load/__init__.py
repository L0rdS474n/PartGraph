"""Dgraph load stage for PartGraph ingestion.

Exposes :class:`partgraph.load.loader.Loader`, which upserts batches of
:class:`~partgraph.normalize.model.StagedPart` records into Dgraph using JSON
mutations only (``set_obj``) — never hand-built N-Quad strings — so untrusted
values are always serialized safely by ``json``/pydgraph.
"""

from __future__ import annotations

__all__: list[str] = []
