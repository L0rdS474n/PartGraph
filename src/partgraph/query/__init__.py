"""Read-only search and detail querying for PartGraph.

This package turns a free-text component search string into safe, parameterised
DQL and ranks the resulting rows for display. It is strictly read-only: nothing
in this package mutates, commits to, or alters the Dgraph database.

Modules:
- :mod:`partgraph.query.parser` — ``parse_query`` (pure/total) producing a
  ``ParsedQuery`` of quantities, an optional package, and text tokens.
- :mod:`partgraph.query.dql_builder` — ``build_search_dql`` / ``build_show_dql``
  producing ``(query_text, variables)`` with injection-safe ``$``-variables.
- :mod:`partgraph.query.ranker` — ``rank_results`` converting multi-block DQL
  responses into a deterministically ordered, deduplicated ``RankedResults``.
- :mod:`partgraph.query.renderer` — Rich rendering of search and detail output.
"""

from __future__ import annotations
