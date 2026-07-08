"""Deterministic ranking and deduplication of multi-block search results.

:func:`rank_results` takes the multi-block DQL response (keyed ``exact`` /
``trig`` / ``fts`` for the hard pass and ``nearest`` for the relaxed pass) and a
:class:`~partgraph.query.parser.ParsedQuery`, and returns a
:class:`RankedResults`: a deduplicated, deterministically ordered list of
:class:`RankedRow`.

Ordering (ADR-RANK):
1. Tier — exact > trigram > fulltext (there is no family tier).
2. In-tier boost — ``stock > 0`` first, then ``is_basic`` first.
3. Tie-break — ``mpn_norm`` ascending, then ``uid`` ascending (fully
   deterministic).

Nearest match (ADR-NEAREST): when every hard block is empty but a ``nearest``
block has rows, ``nearest_match`` is ``True`` and the rows are sorted ascending
by the summed absolute parameter distance to the parsed target quantities
(closest first); each row's ``_distance`` records that sum.

Semantic re-rank (ADR-0020): when :func:`rank_results` is given a
``query_vector`` (the semantic path only), each semantic-tier row's scalar
``similarity`` is set to ``cosine(query_vector, raw["embedding"])`` — computed
with stdlib :mod:`math` ONLY (ranker sits on the lexical path and must stay
importable with the optional ``[embed]`` extra — numpy/torch — ABSENT). The raw
384-float vector is read from the DQL dict, used, and DISCARDED; it is never
stored on :class:`RankedRow`. Semantic rows are ordered by cosine DESCENDING
(tie-break ``mpn_norm`` then ``uid``); the ordered list is truncated to the
``result_limit`` (clamped to :data:`_MAX_RESULT_ROWS`) BY COSINE first, so the
survivors are always the top-cosine rows; a ``sort="stock"/"price"`` value then
only re-orders the DISPLAY of those survivors. ``query_vector=None`` (the lexical
default) leaves behaviour byte-identical to the pre-semantic contract and every
``similarity`` stays ``None``.

Every render field present on the raw DQL dict is propagated onto the
:class:`RankedRow` (architecture BLOCK-1) so downstream gate/CLI code can read
``row.manufacturer`` / ``row.package_name`` / ``row.datasheet_urls`` / the
promoted floats directly instead of digging into nested dicts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from partgraph.query.parser import ParsedQuery

__all__ = ["RankedResults", "RankedRow", "rank_results"]

#: Tier names in descending priority. Lower index = stronger match. ``semantic``
#: (PR4) is a HARD tier: it sits below the lexical tiers (exact/trig/fts) but is
#: still a confident, datasheet-backed hit, so a semantic-only result must NOT
#: trigger the nearest-match banner (ADR-0008 / AC-SR).
_HARD_TIERS: tuple[str, ...] = ("exact", "trig", "fts", "semantic")

#: The relaxed-pass block name.
_NEAREST_TIER = "nearest"

#: Numeric scores per tier (higher = ranked first). ``semantic`` ranks strictly
#: below ``fts`` and strictly above ``nearest``; ``nearest`` is the relaxed pass
#: and only appears on its own (all hard blocks empty).
_TIER_SCORE: dict[str, int] = {
    "exact": 4,
    "trig": 3,
    "fts": 2,
    "semantic": 1,
    "nearest": 0,
}

#: Result-row ceiling for the semantic cosine re-rank truncation. Deliberately a
#: DUPLICATE of :data:`partgraph.query.dql_builder.MAX_RESULT_LIMIT` (200), NOT
#: an import of it: ranker sits on the lexical search path and importing its
#: sibling ``dql_builder`` here would create a module-import cycle. The two are
#: kept in lockstep by an explicit drift-guard test (ADR-0007 result bound;
#: ADR-0020 ranker result-ceiling duplicate-not-import rationale).
_MAX_RESULT_ROWS = 200

#: Promoted numeric predicates copied verbatim onto each RankedRow.
_PROMOTED_PREDICATES: tuple[str, ...] = (
    "voltage_min",
    "voltage_max",
    "current_max",
    "resistance",
    "capacitance",
    "inductance",
    "frequency_max",
    "power",
    "tolerance_pct",
)


@dataclass
class RankedRow:
    """A single ranked search result row.

    Identity / ranking fields are always present. Render fields are propagated
    from the raw DQL dict and are ``None``/empty when the underlying predicate
    was absent on the node (BLOCK-1).
    """

    uid: str
    mpn_norm: str
    tier: str
    mpn: str | None = None
    manufacturer: str | None = None
    package_name: str | None = None
    datasheet_urls: list[str] = field(default_factory=list)
    stock: int | None = None
    is_basic: bool | None = None
    price_usd: float | None = None
    category: str | None = None

    # Promoted numeric predicates (None when absent on the node).
    voltage_min: float | None = None
    voltage_max: float | None = None
    current_max: float | None = None
    resistance: float | None = None
    capacitance: float | None = None
    inductance: float | None = None
    frequency_max: float | None = None
    power: float | None = None
    tolerance_pct: float | None = None

    #: Scalar cosine similarity to the query vector (semantic re-rank only;
    #: ADR-0020). ``None`` on every non-semantic row and whenever no
    #: ``query_vector`` was supplied. The raw 384-float embedding is NEVER stored
    #: here (or on any other field): it is read from the DQL dict, reduced to this
    #: scalar, and discarded.
    similarity: float | None = None

    #: Summed parameter distance to the target (nearest pass only); None on the
    #: hard path.
    _distance: float | None = None

    def params_dict(self) -> dict[str, float]:
        """Return the sparse map of promoted numeric predicates present on the row.

        Only predicates carrying a non-``None`` value are included (``{}`` when
        none). This is the PUBLIC surface the JSON serializer sources its
        ``params`` map from, so :mod:`partgraph.query.renderer` never needs to
        import the module-private :data:`_PROMOTED_PREDICATES` tuple.
        """
        return {
            pred: value
            for pred in _PROMOTED_PREDICATES
            if (value := getattr(self, pred)) is not None
        }


@dataclass
class RankedResults:
    """The ordered, deduplicated outcome of :func:`rank_results`."""

    rows: list[RankedRow] = field(default_factory=list)
    nearest_match: bool = False


def _first_name(raw: dict, edge: str) -> str | None:
    """Return ``raw[edge][0]["name"]`` if present and well-formed, else ``None``."""
    items = raw.get(edge)
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            name = first.get("name")
            if isinstance(name, str):
                return name
    return None


def _datasheet_urls(raw: dict) -> list[str]:
    """Return the list of datasheet URL strings from ``raw['datasheet']``."""
    urls: list[str] = []
    items = raw.get("datasheet")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url:
                    urls.append(url)
    return urls


def _coerce_float(value: object) -> float | None:
    """Return *value* as a float when numeric, else ``None`` (never raises)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _make_row(raw: dict, tier: str) -> RankedRow:
    """Build a :class:`RankedRow` from a raw DQL part dict, propagating fields."""
    uid = str(raw.get("uid", ""))
    mpn_norm = raw.get("mpn_norm") or ""
    mpn = raw.get("mpn")

    stock_val = raw.get("stock")
    stock = stock_val if isinstance(stock_val, int) and not isinstance(stock_val, bool) else None
    is_basic_val = raw.get("is_basic")
    is_basic = is_basic_val if isinstance(is_basic_val, bool) else None

    row = RankedRow(
        uid=uid,
        mpn_norm=str(mpn_norm),
        tier=tier,
        mpn=str(mpn) if mpn is not None else None,
        manufacturer=_first_name(raw, "made_by"),
        package_name=_first_name(raw, "in_package"),
        datasheet_urls=_datasheet_urls(raw),
        stock=stock,
        is_basic=is_basic,
        # price_usd==0.0 is a genuine (free) price, not absence: _coerce_float
        # returns 0.0 for a present 0.0 and None only when the predicate is
        # absent/non-numeric on the node.
        price_usd=_coerce_float(raw.get("price_usd")),
        category=_first_name(raw, "in_category"),
    )
    for pred in _PROMOTED_PREDICATES:
        if pred in raw:
            setattr(row, pred, _coerce_float(raw.get(pred)))
    return row


def _stock_value(row: RankedRow) -> int:
    """Return a non-negative stock count for boost comparison (None -> 0)."""
    return row.stock if isinstance(row.stock, int) and row.stock > 0 else 0


def _distance_to_target(row: RankedRow, parsed: ParsedQuery) -> float:
    """Return the summed absolute distance from *row* to the parsed quantities.

    Only predicates present on the row contribute. With no parsed quantities or
    no overlapping predicates the distance is ``0.0`` (a neutral, stable key).
    """
    total = 0.0
    for quantity in parsed.quantities:
        candidate = getattr(row, quantity.predicate, None)
        if candidate is not None:
            total += abs(candidate - quantity.value)
    return total


def _relevance_key(row: RankedRow) -> tuple:
    """Today's default order: tier desc, stock>0, is_basic, mpn_norm, uid.

    This is the byte-identical historical ordering; ``sort="relevance"`` (and the
    no-``sort``-argument default) both use it unchanged.
    """
    return (
        -_TIER_SCORE[row.tier],
        0 if _stock_value(row) > 0 else 1,
        0 if row.is_basic else 1,
        row.mpn_norm,
        row.uid,
    )


def _stock_key(row: RankedRow) -> tuple:
    """``sort="stock"``: stock DESC (``None`` -> 0, sorts last), then mpn_norm, uid.

    Tier is deliberately NOT part of the key — a high-stock fulltext hit outranks
    a low-stock exact hit under this sort.
    """
    return (-_stock_value(row), row.mpn_norm, row.uid)


def _price_key(row: RankedRow) -> tuple:
    """``sort="price"``: price_usd ASC, rows lacking a price LAST, then mpn_norm, uid.

    ``price_usd == 0.0`` is a real (lowest) price, never conflated with a missing
    price: only a ``None`` price_usd sorts into the trailing block.
    """
    missing = row.price_usd is None
    return (1 if missing else 0, 0.0 if missing else row.price_usd, row.mpn_norm, row.uid)


#: Ordering-key function per ``sort`` value (hard path only). Unknown values fall
#: back to ``relevance`` — the CLI validates ``--sort`` before this is reached.
_SORT_KEYS = {
    "relevance": _relevance_key,
    "stock": _stock_key,
    "price": _price_key,
}


def _cosine_similarity(
    query_vector: list[float], query_norm: float, embedding: object
) -> float | None:
    """Return the cosine similarity of *query_vector* and a raw *embedding*.

    Stdlib :mod:`math` ONLY (no numpy/torch, at module scope or lazily) so the
    ranker stays importable with the optional ``[embed]`` extra absent (ADR-0020).

    *query_norm* is the pre-computed magnitude of *query_vector* (computed once by
    the caller so the per-row cost is a single dot product plus the row's own
    norm). Returns:

    - ``None`` when *embedding* is missing / not a non-empty list / carries a
      non-numeric element — the row could not be scored at all, so it sorts LAST
      and is never conflated with a genuine zero cosine.
    - ``0.0`` when either vector has zero magnitude (a degenerate all-zero
      vector) — never raises :class:`ZeroDivisionError`.
    - the cosine in ``[-1.0, 1.0]`` otherwise.
    """
    if not isinstance(embedding, list) or not embedding:
        return None
    try:
        # strict=True: a wrong-length embedding is unscoreable (raises
        # ValueError -> None below), never a silently-partial cosine.
        dot = math.fsum(q * e for q, e in zip(query_vector, embedding, strict=True))
        embedding_norm = math.sqrt(math.fsum(e * e for e in embedding))
    except (TypeError, ValueError):
        # A malformed (non-numeric or wrong-length) embedding: unscoreable ->
        # similarity None -> sorts last (never raises into the caller).
        return None
    if query_norm == 0.0 or embedding_norm == 0.0:
        return 0.0
    return dot / (query_norm * embedding_norm)


def _cosine_sort_key(row: RankedRow) -> tuple:
    """Semantic re-rank order: cosine DESC, unscoreable (None) last, tie-break.

    An unscoreable row (``similarity is None`` — a missing/malformed raw
    embedding) sorts strictly after every scored row; scored rows sort by cosine
    DESCENDING, then ``mpn_norm`` ascending, then ``uid`` ascending (fully
    deterministic).
    """
    missing = row.similarity is None
    return (
        1 if missing else 0,
        0.0 if missing else -row.similarity,
        row.mpn_norm,
        row.uid,
    )


def _resolve_result_limit(result_limit: int | None) -> int:
    """Clamp *result_limit* into ``[0, _MAX_RESULT_ROWS]`` for the final slice.

    ``None`` (no caller limit) -> the :data:`_MAX_RESULT_ROWS` result ceiling. A
    negative or zero value clamps to ``0`` — a clean empty result, never a
    negative-index slice artifact (security F3). An oversized value clamps to
    :data:`_MAX_RESULT_ROWS` so the RESULT bound is never bypassed.
    """
    if result_limit is None:
        return _MAX_RESULT_ROWS
    return max(0, min(result_limit, _MAX_RESULT_ROWS))


def rank_results(
    blocks: dict[str, list[dict]],
    parsed: ParsedQuery,
    *,
    sort: str = "relevance",
    query_vector: list[float] | None = None,
    result_limit: int | None = None,
) -> RankedResults:
    """Rank, deduplicate and order multi-block DQL results.

    Args:
        blocks: DQL response keyed by block name (``exact``/``trig``/``fts`` and
            optionally ``nearest``). Missing keys are treated as empty.
        parsed: The parsed query (drives nearest-pass distance sorting).
        sort: The hard-path ordering — ``"relevance"`` (default; today's
            tier/stock/is_basic/mpn_norm/uid order), ``"stock"`` (stock DESC) or
            ``"price"`` (price_usd ASC, missing last). Ignored entirely in
            nearest-match mode, where the parameter-distance order always wins.
        query_vector: The semantic query embedding (semantic path only;
            ADR-0020). When supplied, each semantic-tier row's ``similarity`` is
            set to ``cosine(query_vector, raw["embedding"])`` (stdlib), semantic
            rows are ordered by cosine DESCENDING, and the ordered list is
            truncated to ``result_limit`` BY COSINE before any ``stock``/``price``
            re-order of the survivors. ``None`` (the lexical default) leaves
            behaviour byte-identical and every ``similarity`` ``None``.
        result_limit: The maximum number of rows to return AFTER the cosine
            re-rank (semantic path only). Clamped to ``[0, _MAX_RESULT_ROWS]``:
            ``None`` -> the result ceiling; a negative/zero value -> a clean empty
            result (never a negative-index slice); an oversized value -> the
            ceiling. Ignored when ``query_vector`` is ``None``.

    Returns:
        A :class:`RankedResults` with deduplicated, deterministically ordered
        rows and the ``nearest_match`` flag.
    """
    # Determine whether any hard tier produced rows.
    hard_has_rows = any(
        isinstance(blocks.get(tier), list) and blocks.get(tier)
        for tier in _HARD_TIERS
    )
    nearest_rows_present = (
        isinstance(blocks.get(_NEAREST_TIER), list) and bool(blocks.get(_NEAREST_TIER))
    )
    nearest_match = (not hard_has_rows) and nearest_rows_present

    # Pre-compute the query vector's magnitude ONCE (stdlib; ADR-0020) so each
    # per-row cosine costs a single dot product plus the row's own norm.
    query_norm = (
        math.sqrt(math.fsum(component * component for component in query_vector))
        if query_vector is not None
        else None
    )

    # Build rows, deduplicating by uid. The strongest tier seen for a uid wins.
    # Every block (including ``nearest``) is iterated: when a hard tier already
    # produced rows (so ``nearest_match`` is False), any ``nearest`` rows still
    # surface but are ranked last by their tier score (0). When only the relaxed
    # pass produced rows (``nearest_match`` True) the distance sort below applies.
    by_uid: dict[str, RankedRow] = {}
    tier_order = (*_HARD_TIERS, _NEAREST_TIER)
    for tier in tier_order:
        block = blocks.get(tier)
        if not isinstance(block, list):
            continue
        for raw in block:
            if not isinstance(raw, dict):
                continue
            row = _make_row(raw, tier)
            if query_vector is not None and tier == "semantic":
                # Read the raw 384-float embedding, reduce it to a scalar cosine
                # and DISCARD the vector — it is never stored on RankedRow
                # (ADR-0020). A missing/malformed embedding yields None (the row
                # sorts last), never a raised error.
                row.similarity = _cosine_similarity(
                    query_vector, query_norm, raw.get("embedding")
                )
            existing = by_uid.get(row.uid)
            # Keep the strongest tier seen for each uid (first occurrence wins on
            # ties because tiers are visited in descending priority order).
            if existing is None or _TIER_SCORE[tier] > _TIER_SCORE[existing.tier]:
                by_uid[row.uid] = row

    rows = list(by_uid.values())

    if nearest_match:
        # Relaxed pass: order purely by ascending parameter distance, with a
        # deterministic tie-break on mpn_norm then uid. ``sort`` is a no-op here —
        # the parameter-distance order always wins (AC-SF-23).
        for row in rows:
            row._distance = _distance_to_target(row, parsed)
        rows.sort(key=lambda r: (r._distance, r.mpn_norm, r.uid))
    elif query_vector is not None:
        # Semantic cosine re-rank (ADR-0020): order by cosine DESC, then truncate
        # the ORDERED list to the (clamped) result limit so the survivors are
        # always the top-cosine rows. Only AFTER that do stock/price re-order the
        # survivors for display — this never changes WHICH rows survived.
        rows.sort(key=_cosine_sort_key)
        rows = rows[: _resolve_result_limit(result_limit)]
        if sort in ("stock", "price"):
            rows.sort(key=_SORT_KEYS[sort])
    else:
        # Hard (lexical) path: order by the selected ``sort`` key. ``relevance``
        # is the byte-identical historical order; ``stock``/``price`` are
        # in-memory re-orders only (no DQL change). ``result_limit`` is ignored
        # here so the lexical path stays byte-identical to the pre-semantic
        # contract.
        rows.sort(key=_SORT_KEYS.get(sort, _relevance_key))

    return RankedResults(rows=rows, nearest_match=nearest_match)
