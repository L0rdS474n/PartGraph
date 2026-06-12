"""
Tests: SEARCH-RANK-1..6 — partgraph.query.ranker

Specifies the behavior of rank_results() which converts multi-block DQL
result dicts into a deterministically ordered, deduplicated RankedResults.

Module under test: partgraph.query.ranker
  - rank_results(blocks: dict[str, list[dict]], parsed: ParsedQuery) -> RankedResults
  - RankedResults.rows: list[RankedRow]
  - RankedResults.nearest_match: bool
  - RankedRow fields (at minimum): uid, mpn_norm, tier, score

Design decisions pinned by dispatcher (ADR-RANK, ADR-NEAREST):
  - Tier order: exact > trigram > fulltext.
  - In-tier boost: stock>0, then is_basic.
  - Dedup by uid.
  - Deterministic tie-break: mpn_norm then uid.
  - nearest_match=False when hard hits ≥1; nearest_match=True on zero hard + relaxed rows.
  - Nearest rows sorted ascending by sum|candidate.pred - target| for parametric queries.

NOTE: Collection will ERROR on import of partgraph.query.ranker because that
module does not exist yet. That is the correct red state before PR3 implementation.
"""

from __future__ import annotations

import pytest

from partgraph.query.parser import ParsedQuery, Quantity
from partgraph.query.ranker import RankedResults, rank_results  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_parsed(
    *,
    quantities: list[Quantity] | None = None,
    package: str | None = None,
    text_tokens: list[str] | None = None,
    raw_query: str = "",
) -> ParsedQuery:
    return ParsedQuery(
        quantities=quantities or [],
        package=package,
        text_tokens=text_tokens or [],
        raw_query=raw_query,
    )


def _q(predicate: str, value: float, raw: str = "") -> Quantity:
    return Quantity(predicate=predicate, value=value, raw=raw)


def _part(
    uid: str,
    mpn_norm: str,
    *,
    stock: int = 0,
    is_basic: bool = False,
    voltage_max: float | None = None,
) -> dict:
    """Build a minimal part dict as returned from DQL."""
    row: dict = {
        "uid": uid,
        "mpn": mpn_norm,
        "mpn_norm": mpn_norm,
        "stock": stock,
        "is_basic": is_basic,
    }
    if voltage_max is not None:
        row["voltage_max"] = voltage_max
    return row


# ---------------------------------------------------------------------------
# SEARCH-RANK-1: Tier order exact > trigram > fulltext
# ---------------------------------------------------------------------------

def test_rank_1_exact_tier_before_trigram_before_fulltext() -> None:
    """Given blocks with one part each in 'exact', 'trig', and 'fts' blocks.
    When rank_results is called with any ParsedQuery.
    Then the 'exact' block part appears before 'trig' which appears before 'fts'
    in the rows list (tier ordering: exact > trigram > fulltext).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [_part("0x01", "MAX232", stock=0, is_basic=False)],
        "trig":  [_part("0x02", "MAX232A", stock=0, is_basic=False)],
        "fts":   [_part("0x03", "SN65C1232", stock=0, is_basic=False)],
    }

    result = rank_results(blocks, parsed)

    mpn_norms = [row.mpn_norm for row in result.rows]
    idx_exact = mpn_norms.index("MAX232")
    idx_trig  = mpn_norms.index("MAX232A")
    idx_fts   = mpn_norms.index("SN65C1232")

    assert idx_exact < idx_trig, (
        f"Exact-tier part must appear before trigram-tier. "
        f"Row order: {mpn_norms}"
    )
    assert idx_trig < idx_fts, (
        f"Trigram-tier part must appear before fts-tier. "
        f"Row order: {mpn_norms}"
    )


# ---------------------------------------------------------------------------
# SEARCH-RANK-2: In-tier boost: stock>0 before stock=0; is_basic before not is_basic
# ---------------------------------------------------------------------------

def test_rank_2_in_tier_stock_boost() -> None:
    """Given two parts in the same tier, one with stock>0 and one with stock=0.
    When rank_results is called.
    Then the part with stock>0 appears before the part with stock=0 (in-tier boost).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part("0x10", "MAX232-NOSTOCK", stock=0, is_basic=False),
            _part("0x11", "MAX232-INSTOCK", stock=100, is_basic=False),
        ],
    }

    result = rank_results(blocks, parsed)
    mpn_norms = [row.mpn_norm for row in result.rows]

    assert mpn_norms.index("MAX232-INSTOCK") < mpn_norms.index("MAX232-NOSTOCK"), (
        f"Stock>0 part must rank above no-stock part in same tier. "
        f"Row order: {mpn_norms}"
    )


def test_rank_2_in_tier_is_basic_boost_when_stock_equal() -> None:
    """Given two parts in the same tier, same stock=0, but one is_basic=True.
    When rank_results is called.
    Then the is_basic=True part appears before the non-basic part (is_basic boost).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part("0x20", "MAX232-NOTBASIC", stock=0, is_basic=False),
            _part("0x21", "MAX232-BASIC", stock=0, is_basic=True),
        ],
    }

    result = rank_results(blocks, parsed)
    mpn_norms = [row.mpn_norm for row in result.rows]

    assert mpn_norms.index("MAX232-BASIC") < mpn_norms.index("MAX232-NOTBASIC"), (
        f"is_basic=True part must rank above non-basic when stock is equal. "
        f"Row order: {mpn_norms}"
    )


# ---------------------------------------------------------------------------
# SEARCH-RANK-3: Dedup by uid
# ---------------------------------------------------------------------------

def test_rank_3_dedup_by_uid_across_blocks() -> None:
    """Given the same uid appearing in both 'exact' and 'trig' blocks.
    When rank_results is called.
    Then the uid appears exactly once in the output rows (dedup; exact tier wins).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [_part("0x30", "MAX232", stock=0)],
        "trig":  [_part("0x30", "MAX232", stock=0)],  # same uid
        "fts":   [_part("0x31", "MAX232-OTHER", stock=0)],
    }

    result = rank_results(blocks, parsed)
    uids = [row.uid for row in result.rows]

    assert uids.count("0x30") == 1, (
        f"uid '0x30' appears {uids.count('0x30')} times; must be exactly 1 (dedup). "
        f"All uids: {uids}"
    )


def test_rank_3_dedup_keeps_higher_tier_entry() -> None:
    """Given the same uid in exact (higher) and trig (lower) blocks.
    When rank_results is called.
    Then the surviving entry is in the exact tier (higher-tier entry wins on dedup).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [_part("0x40", "MAX232", stock=0)],
        "trig":  [_part("0x40", "MAX232", stock=0)],
    }

    result = rank_results(blocks, parsed)
    surviving = [row for row in result.rows if row.uid == "0x40"]
    assert len(surviving) == 1, (
        f"uid '0x40' must appear exactly once. Found: {[r.uid for r in result.rows]}"
    )
    # The surviving entry must carry the exact tier.
    row = surviving[0]
    assert hasattr(row, "tier"), "RankedRow must have a 'tier' attribute."
    assert "exact" in str(row.tier).lower(), (
        f"Deduped row from exact block must retain exact tier. Got tier: {row.tier!r}"
    )


# ---------------------------------------------------------------------------
# SEARCH-RANK-4: Deterministic tie-break: mpn_norm then uid
# ---------------------------------------------------------------------------

def test_rank_4_tie_break_by_mpn_norm_then_uid() -> None:
    """Given multiple parts in the same tier with identical boost scores.
    When rank_results is called multiple times.
    Then the row order is identical each time (deterministic) and follows
    lexicographic mpn_norm ordering as the primary tie-break.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part("0x53", "ZZZ232", stock=0),
            _part("0x51", "AAA232", stock=0),
            _part("0x52", "MMM232", stock=0),
        ],
    }

    result1 = rank_results(blocks, parsed)
    result2 = rank_results(blocks, parsed)

    mpn1 = [row.mpn_norm for row in result1.rows]
    mpn2 = [row.mpn_norm for row in result2.rows]

    assert mpn1 == mpn2, (
        f"rank_results must be deterministic. Got different orders: {mpn1} vs {mpn2}"
    )
    assert mpn1 == sorted(mpn1), (
        f"Same-tier, same-boost parts must be sorted by mpn_norm. Got: {mpn1}"
    )


def test_rank_4_uid_tiebreak_when_mpn_norm_equal() -> None:
    """Given two parts with identical mpn_norm and identical boost scores.
    When rank_results is called.
    Then the ordering is by uid (secondary tie-break) to guarantee determinism.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part("0xBB", "SAME232", stock=0),
            _part("0xAA", "SAME232", stock=0),
        ],
    }

    result = rank_results(blocks, parsed)
    uids = [row.uid for row in result.rows]

    # uid tie-break must be deterministic; lower uid string sorts first.
    assert uids == sorted(uids), (
        f"When mpn_norm is equal, uid must be the tie-break (lexicographic). "
        f"Got: {uids}"
    )


# ---------------------------------------------------------------------------
# SEARCH-RANK-5: nearest_match flag
# ---------------------------------------------------------------------------

def test_rank_5_nearest_match_false_when_hard_hits_present() -> None:
    """Given blocks with at least one hard hit (any block non-empty).
    When rank_results is called.
    Then nearest_match=False in the result.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [_part("0x60", "MAX232", stock=0)],
    }

    result = rank_results(blocks, parsed)

    assert result.nearest_match is False, (
        f"nearest_match must be False when hard hits exist. Got: {result.nearest_match}"
    )


def test_rank_5_nearest_match_true_when_zero_hard_rows_but_relaxed_present() -> None:
    """Given blocks where all hard-match blocks are empty but a 'relaxed' block
    (or equivalent signal) contains rows.
    When rank_results is called with nearest_match rows pre-populated.
    Then nearest_match=True in the result.

    NOTE: The ranker receives blocks already shaped by the two-pass query engine
    (ADR-NEAREST). We simulate the relaxed state by passing only a 'nearest'
    (or 'relaxed') keyed block with no 'exact'/'trig'/'fts' hits.
    """
    parsed = _make_parsed(quantities=[_q("voltage_max", 1.2, "1.2V")])
    # All hard blocks empty; relaxed rows provided under 'nearest' key.
    blocks = {
        "exact":   [],
        "trig":    [],
        "fts":     [],
        "nearest": [
            _part("0x70", "MAX232", stock=10, voltage_max=3.3),
            _part("0x71", "MAX3232", stock=5, voltage_max=5.5),
        ],
    }

    result = rank_results(blocks, parsed)

    assert result.nearest_match is True, (
        f"nearest_match must be True when hard blocks empty and nearest rows present. "
        f"Got: {result.nearest_match}"
    )
    assert len(result.rows) > 0, (
        "rows must be non-empty even when nearest_match=True."
    )


# ---------------------------------------------------------------------------
# SEARCH-RANK-6: Nearest rows sorted ascending by |voltage_max - 1.2|
# ---------------------------------------------------------------------------

def test_rank_6_nearest_rows_sorted_by_ascending_parametric_distance() -> None:
    """Given a nearest-match result with a voltage_max target of 1.2V and multiple
    candidate parts with varying voltage_max values.
    When rank_results produces nearest_match=True rows.
    Then rows are sorted ascending by |voltage_max - 1.2| (closest first).

    ADR-NEAREST: nearest rows sorted ascending by sum|candidate.pred - target|.
    """
    parsed = _make_parsed(quantities=[_q("voltage_max", 1.2, "1.2V")])
    # Distances: 5.5 is |5.5-1.2|=4.3; 1.8 is |1.8-1.2|=0.6; 3.3 is |3.3-1.2|=2.1
    blocks = {
        "exact":   [],
        "trig":    [],
        "fts":     [],
        "nearest": [
            _part("0x80", "FAR-PART",    stock=0,  voltage_max=5.5),
            _part("0x81", "CLOSE-PART",  stock=0,  voltage_max=1.8),
            _part("0x82", "MID-PART",    stock=0,  voltage_max=3.3),
        ],
    }

    result = rank_results(blocks, parsed)

    assert result.nearest_match is True, "Expected nearest_match=True for this fixture."
    assert len(result.rows) == 3, f"Expected 3 rows. Got: {len(result.rows)}"

    mpn_norms = [row.mpn_norm for row in result.rows]
    assert mpn_norms[0] == "CLOSE-PART", (
        f"Row closest to 1.2V (voltage_max=1.8, distance=0.6) must be first. "
        f"Got order: {mpn_norms}"
    )
    assert mpn_norms[1] == "MID-PART", (
        f"Middle distance (voltage_max=3.3, distance=2.1) must be second. "
        f"Got order: {mpn_norms}"
    )
    assert mpn_norms[2] == "FAR-PART", (
        f"Farthest (voltage_max=5.5, distance=4.3) must be last. "
        f"Got order: {mpn_norms}"
    )


def test_rank_6_nearest_empty_blocks_no_nearest_block_gives_empty_rows() -> None:
    """Given all blocks empty (no hard hits, no nearest rows).
    When rank_results is called.
    Then rows is empty and nearest_match is False
    (there are no results of any kind to show).
    """
    parsed = _make_parsed(text_tokens=["NONEXISTENT9999"])
    blocks: dict[str, list[dict]] = {
        "exact": [],
        "trig":  [],
        "fts":   [],
    }

    result = rank_results(blocks, parsed)

    assert result.rows == [], (
        f"Expected empty rows when all blocks are empty. Got: {result.rows}"
    )
    assert result.nearest_match is False, (
        f"nearest_match must be False when there are no results at all. "
        f"Got: {result.nearest_match}"
    )


# ---------------------------------------------------------------------------
# Structural / return type contracts
# ---------------------------------------------------------------------------

def test_ranked_results_has_required_attributes() -> None:
    """Given a call to rank_results.
    When it returns.
    Then the result is a RankedResults with 'rows' (list) and 'nearest_match' (bool).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {"exact": [_part("0x90", "MAX232")]}

    result = rank_results(blocks, parsed)

    assert isinstance(result, RankedResults), (
        f"rank_results must return a RankedResults. Got: {type(result)}"
    )
    assert hasattr(result, "rows"), "RankedResults must have 'rows' attribute."
    assert hasattr(result, "nearest_match"), "RankedResults must have 'nearest_match' attribute."
    assert isinstance(result.rows, list), "RankedResults.rows must be a list."
    assert isinstance(result.nearest_match, bool), "RankedResults.nearest_match must be bool."


def test_ranked_row_has_required_fields() -> None:
    """Given a non-empty result from rank_results.
    When we inspect the first row.
    Then it has at minimum: uid, mpn_norm, tier.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {"exact": [_part("0xA0", "MAX232")]}

    result = rank_results(blocks, parsed)

    assert result.rows, "Expected at least one row."
    row = result.rows[0]
    assert hasattr(row, "uid"), "RankedRow must have 'uid'."
    assert hasattr(row, "mpn_norm"), "RankedRow must have 'mpn_norm'."
    assert hasattr(row, "tier"), "RankedRow must have 'tier'."


# ---------------------------------------------------------------------------
# C — RankedRow field propagation (ARCHITECTURE BLOCK-1)
# PIN: RankedRow must expose manufacturer, datasheet_urls, package_name,
#      and the numeric predicates: resistance, voltage_max, tolerance_pct.
#
# These tests close the gap where unit tests pass but GATE-PR3-1/2 fail because
# the gate code does `getattr(row, "manufacturer", None)` etc. and gets None
# even though the raw block dicts carry the data.
# ---------------------------------------------------------------------------

def _part_rich(uid: str, mpn_norm: str, *, stock: int = 0, is_basic: bool = False, **extra) -> dict:  # noqa: PLR0913
    """Build a fully-populated part dict as returned from DQL (all predicates).

    Pass optional DQL predicates as keyword arguments, e.g.:
        _part_rich("0xC1", "MAX232CPE", made_by=[{"name": "TI"}], voltage_max=5.5)
    Only keys with non-None values are added to the dict (mirrors DQL omission
    of predicates that are not set on the node).
    """
    row: dict = {
        "uid": uid,
        "mpn": mpn_norm,
        "mpn_norm": mpn_norm,
        "stock": stock,
        "is_basic": is_basic,
    }
    for key, val in extra.items():
        if val is not None:
            row[key] = val
    return row


def test_rank_row_propagates_manufacturer_from_made_by() -> None:
    """Given a raw block dict with made_by:[{name:"Texas Instruments"}].
    When rank_results is called.
    Then the resulting RankedRow exposes:
      - row.manufacturer == "Texas Instruments"  (str, not None).

    Closes GATE-PR3-1 gap: gate does getattr(row, "manufacturer", None) and
    builds the manufacturer set — must get the name, not None.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part_rich(
                "0xC1",
                "MAX232CPE",
                made_by=[{"name": "Texas Instruments"}],
            )
        ]
    }

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "manufacturer"), (
        "RankedRow must expose 'manufacturer' attribute "
        "(propagated from made_by[0].name in the raw block dict)."
    )
    assert row.manufacturer == "Texas Instruments", (
        f"row.manufacturer must be 'Texas Instruments' (from made_by[0].name). "
        f"Got: {row.manufacturer!r}"
    )


def test_rank_row_manufacturer_none_when_made_by_absent() -> None:
    """Given a raw block dict with no made_by field.
    When rank_results is called.
    Then row.manufacturer is None (field is present but nullable).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {"exact": [_part("0xC2", "NOMAKER232")]}

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "manufacturer"), (
        "RankedRow must always expose 'manufacturer' attribute (None when absent)."
    )
    assert row.manufacturer is None, (
        f"row.manufacturer must be None when made_by is absent. Got: {row.manufacturer!r}"
    )


def test_rank_row_propagates_datasheet_urls_from_datasheet() -> None:
    """Given a raw block dict with datasheet:[{url:"https://example.com/ds.pdf"}].
    When rank_results is called.
    Then the resulting RankedRow exposes:
      - row.datasheet_urls == ["https://example.com/ds.pdf"]  (list[str]).

    Closes GATE-PR3-1 gap: gate does `urls = getattr(row, "datasheet_urls", None) or []`
    and checks `any(u.startswith("http") for u in urls)`.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part_rich(
                "0xC3",
                "MAX232CPE",
                datasheet=[{"url": "https://www.ti.com/lit/ds/symlink/max232.pdf"}],
            )
        ]
    }

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "datasheet_urls"), (
        "RankedRow must expose 'datasheet_urls' attribute "
        "(propagated from datasheet[*].url in the raw block dict)."
    )
    assert isinstance(row.datasheet_urls, list), (
        f"row.datasheet_urls must be a list. Got: {type(row.datasheet_urls)!r}"
    )
    assert row.datasheet_urls == ["https://www.ti.com/lit/ds/symlink/max232.pdf"], (
        f"row.datasheet_urls must extract url strings from datasheet list. "
        f"Got: {row.datasheet_urls!r}"
    )


def test_rank_row_datasheet_urls_empty_list_when_absent() -> None:
    """Given a raw block dict with no datasheet field.
    When rank_results is called.
    Then row.datasheet_urls is an empty list (not None, not missing).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {"exact": [_part("0xC4", "NODS232")]}

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "datasheet_urls"), (
        "RankedRow must always expose 'datasheet_urls' ([] when absent)."
    )
    assert row.datasheet_urls == [], (
        f"row.datasheet_urls must be [] when datasheet is absent. "
        f"Got: {row.datasheet_urls!r}"
    )


def test_rank_row_propagates_package_name_from_in_package() -> None:
    """Given a raw block dict with in_package:[{name:"0402"}].
    When rank_results is called.
    Then the resulting RankedRow exposes:
      - row.package_name == "0402"  (str, not None).

    Closes GATE-PR3-2 gap: gate does `package = getattr(row, "package_name", None) or ""`
    and asserts "0402" in package.
    """
    parsed = _make_parsed(package="0402")
    blocks = {
        "exact": [
            _part_rich(
                "0xC5",
                "RC0402FR-0710KL",
                in_package=[{"name": "0402"}],
            )
        ]
    }

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "package_name"), (
        "RankedRow must expose 'package_name' attribute "
        "(propagated from in_package[0].name in the raw block dict)."
    )
    assert row.package_name == "0402", (
        f"row.package_name must be '0402' (from in_package[0].name). "
        f"Got: {row.package_name!r}"
    )


def test_rank_row_package_name_none_when_in_package_absent() -> None:
    """Given a raw block dict with no in_package field.
    When rank_results is called.
    Then row.package_name is None.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {"exact": [_part("0xC6", "NOPKG232")]}

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "package_name"), (
        "RankedRow must always expose 'package_name' (None when absent)."
    )
    assert row.package_name is None, (
        f"row.package_name must be None when in_package is absent. "
        f"Got: {row.package_name!r}"
    )


def test_rank_row_propagates_resistance_float() -> None:
    """Given a raw block dict with resistance=10000.0.
    When rank_results is called.
    Then row.resistance == 10000.0  (float attribute directly on RankedRow).

    Closes GATE-PR3-2 gap: gate does `resistance = getattr(row, "resistance", None)`
    and asserts 9900 <= resistance <= 10100.
    """
    parsed = _make_parsed(
        quantities=[_q("resistance", 10000.0, "10k")],
        package="0402",
    )
    blocks = {
        "exact": [
            _part_rich(
                "0xC7",
                "RC0402FR-0710KL",
                resistance=10000.0,
            )
        ]
    }

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "resistance"), (
        "RankedRow must expose 'resistance' attribute "
        "(propagated from the 'resistance' numeric predicate in the raw dict)."
    )
    assert row.resistance == pytest.approx(10000.0), (
        f"row.resistance must be 10000.0. Got: {row.resistance!r}"
    )


def test_rank_row_propagates_voltage_max_float() -> None:
    """Given a raw block dict with voltage_max=5.5.
    When rank_results is called.
    Then row.voltage_max == 5.5  (float attribute directly on RankedRow).
    """
    parsed = _make_parsed(quantities=[_q("voltage_max", 1.2, "1.2V")])
    blocks = {
        "nearest": [
            _part_rich("0xC8", "MAX232CPE", voltage_max=5.5),
        ],
        "exact": [],
        "trig": [],
        "fts": [],
    }

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "voltage_max"), (
        "RankedRow must expose 'voltage_max' attribute "
        "(propagated from 'voltage_max' numeric predicate in the raw dict)."
    )
    assert row.voltage_max == pytest.approx(5.5), (
        f"row.voltage_max must be 5.5. Got: {row.voltage_max!r}"
    )


def test_rank_row_propagates_tolerance_pct_float() -> None:
    """Given a raw block dict with tolerance_pct=1.0.
    When rank_results is called.
    Then row.tolerance_pct == 1.0  (float attribute directly on RankedRow).
    """
    parsed = _make_parsed(quantities=[_q("tolerance_pct", 1.0, "1%")])
    blocks = {
        "exact": [
            _part_rich("0xC9", "RC0402FR-0710KL", tolerance_pct=1.0),
        ]
    }

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "tolerance_pct"), (
        "RankedRow must expose 'tolerance_pct' attribute "
        "(propagated from 'tolerance_pct' numeric predicate in the raw dict)."
    )
    assert row.tolerance_pct == pytest.approx(1.0), (
        f"row.tolerance_pct must be 1.0. Got: {row.tolerance_pct!r}"
    )


def test_rank_row_uid_mpn_norm_tier_still_present_on_rich_part() -> None:
    """Given a fully-populated part dict with all predicates.
    When rank_results is called.
    Then existing fields uid, mpn_norm, tier are still present alongside the new ones.
    (Regression guard: adding new fields must not remove existing ones.)
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part_rich(
                "0xCA",
                "MAX232CPE",
                made_by=[{"name": "Texas Instruments"}],
                datasheet=[{"url": "https://example.com/ds.pdf"}],
                in_package=[{"name": "PDIP-16"}],
                resistance=None,
                voltage_max=5.5,
                tolerance_pct=None,
            )
        ]
    }

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "uid") and row.uid == "0xCA", (
        f"RankedRow.uid must be '0xCA'. Got: {getattr(row, 'uid', 'MISSING')!r}"
    )
    assert hasattr(row, "mpn_norm") and row.mpn_norm == "MAX232CPE", (
        f"RankedRow.mpn_norm must be 'MAX232CPE'. Got: {getattr(row, 'mpn_norm', 'MISSING')!r}"
    )
    assert hasattr(row, "tier"), (
        "RankedRow.tier must still be present on a rich part."
    )
