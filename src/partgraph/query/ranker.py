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

Every render field present on the raw DQL dict is propagated onto the
:class:`RankedRow` (architecture BLOCK-1) so downstream gate/CLI code can read
``row.manufacturer`` / ``row.package_name`` / ``row.datasheet_urls`` / the
promoted floats directly instead of digging into nested dicts.
"""

from __future__ import annotations

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

    #: Summed parameter distance to the target (nearest pass only); None on the
    #: hard path.
    _distance: float | None = None


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


def rank_results(blocks: dict[str, list[dict]], parsed: ParsedQuery) -> RankedResults:
    """Rank, deduplicate and order multi-block DQL results.

    Args:
        blocks: DQL response keyed by block name (``exact``/``trig``/``fts`` and
            optionally ``nearest``). Missing keys are treated as empty.
        parsed: The parsed query (drives nearest-pass distance sorting).

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
            existing = by_uid.get(row.uid)
            # Keep the strongest tier seen for each uid (first occurrence wins on
            # ties because tiers are visited in descending priority order).
            if existing is None or _TIER_SCORE[tier] > _TIER_SCORE[existing.tier]:
                by_uid[row.uid] = row

    rows = list(by_uid.values())

    if nearest_match:
        # Relaxed pass: order purely by ascending parameter distance, with a
        # deterministic tie-break on mpn_norm then uid.
        for row in rows:
            row._distance = _distance_to_target(row, parsed)
        rows.sort(key=lambda r: (r._distance, r.mpn_norm, r.uid))
    else:
        # Hard path: tier desc, then stock>0, then is_basic, then mpn_norm, uid.
        rows.sort(
            key=lambda r: (
                -_TIER_SCORE[r.tier],
                0 if _stock_value(r) > 0 else 1,
                0 if r.is_basic else 1,
                r.mpn_norm,
                r.uid,
            )
        )

    return RankedResults(rows=rows, nearest_match=nearest_match)
