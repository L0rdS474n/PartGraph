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


# ===========================================================================
# AC-SR: PR4 semantic tier extension
#
# Pinned _TIER_SCORE (after PR4): exact=4, trig=3, fts=2, semantic=1, nearest=0
#
# Contract:
#  - "semantic" is a HARD tier (below fts, above nearest).
#  - uid in fts AND semantic -> kept at fts (higher tier wins).
#  - semantic does NOT trigger the "nearest" banner.
#  - semantic-only results -> rows at semantic tier, deterministic order.
# ===========================================================================


def test_ac_sr_1_tier_order_exact_trig_fts_semantic() -> None:
    """AC-SR-1: Given blocks with one part each in exact, trig, fts, and semantic.
    When rank_results is called.
    Then the row order is exact, trig, fts, semantic (semantic below fts).

    Pinned _TIER_SCORE: exact=4, trig=3, fts=2, semantic=1, nearest=0.
    """
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "exact":    [_part("0xE1", "MAX232EXACT",    stock=0)],
        "trig":     [_part("0xE2", "MAX232TRIG",     stock=0)],
        "fts":      [_part("0xE3", "MAX232FTS",      stock=0)],
        "semantic": [_part("0xE4", "SN75C1232SEM",   stock=0)],
    }

    result = rank_results(blocks, parsed)
    mpn_norms = [row.mpn_norm for row in result.rows]

    idx_exact = mpn_norms.index("MAX232EXACT")
    idx_trig = mpn_norms.index("MAX232TRIG")
    idx_fts = mpn_norms.index("MAX232FTS")
    idx_sem = mpn_norms.index("SN75C1232SEM")

    assert idx_exact < idx_trig, (
        f"AC-SR-1: exact must precede trig. Order: {mpn_norms}"
    )
    assert idx_trig < idx_fts, (
        f"AC-SR-1: trig must precede fts. Order: {mpn_norms}"
    )
    assert idx_fts < idx_sem, (
        f"AC-SR-1: fts must precede semantic. Order: {mpn_norms}"
    )


def test_ac_sr_2_uid_in_fts_and_semantic_kept_at_fts() -> None:
    """AC-SR-2: Given a uid that appears in both fts and semantic blocks.
    When rank_results is called.
    Then:
    - The uid appears exactly once in the output.
    - The surviving row is at the fts tier (higher tier wins on dedup).
    """
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "exact":    [],
        "trig":     [],
        "fts":      [_part("0xDUP", "DUPPART232", stock=0)],
        "semantic": [_part("0xDUP", "DUPPART232", stock=0)],
    }

    result = rank_results(blocks, parsed)
    uids = [row.uid for row in result.rows]

    assert uids.count("0xDUP") == 1, (
        f"AC-SR-2: uid '0xDUP' must appear exactly once (dedup). "
        f"All uids: {uids}"
    )
    survivor = next(row for row in result.rows if row.uid == "0xDUP")
    assert "fts" in str(survivor.tier).lower(), (
        f"AC-SR-2: surviving row must be at fts tier (fts > semantic). "
        f"Got tier: {survivor.tier!r}"
    )


def test_ac_sr_3_semantic_above_nearest_no_nearest_banner() -> None:
    """AC-SR-3: Given blocks with semantic rows only (no nearest).
    When rank_results is called.
    Then:
    - nearest_match is False (semantic is a HARD tier — does not trigger the banner).
    - Rows from the semantic block are present.
    - Semantic rows rank above nearest rows (semantic score > nearest score).
    """
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "exact":    [],
        "trig":     [],
        "fts":      [],
        "semantic": [_part("0xS1", "MAX232SEM", stock=10)],
    }

    result = rank_results(blocks, parsed)

    assert result.nearest_match is False, (
        f"AC-SR-3: semantic block hit must NOT set nearest_match=True. "
        f"Semantic is a hard tier. Got: nearest_match={result.nearest_match}"
    )
    assert any(row.uid == "0xS1" for row in result.rows), (
        "AC-SR-3: semantic row must appear in results."
    )


def test_ac_sr_3_semantic_scores_above_nearest_tier() -> None:
    """AC-SR-3 (ordering): Given blocks with semantic AND nearest rows.
    When rank_results is called.
    Then all semantic rows appear before all nearest rows in the output.
    """
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "exact":    [],
        "trig":     [],
        "fts":      [],
        "semantic": [_part("0xSEM", "SEMPART232", stock=0)],
        "nearest":  [_part("0xNEAR", "NEARPART232", stock=0)],
    }

    result = rank_results(blocks, parsed)
    uids = [row.uid for row in result.rows]

    assert "0xSEM" in uids, "AC-SR-3: semantic row must be present."
    assert "0xNEAR" in uids, "AC-SR-3: nearest row must be present."

    idx_sem = uids.index("0xSEM")
    idx_near = uids.index("0xNEAR")

    assert idx_sem < idx_near, (
        f"AC-SR-3: semantic row must rank above nearest row. "
        f"Order: {uids}"
    )


def test_ac_sr_4_semantic_only_deterministic_order() -> None:
    """AC-SR-4 (REWRITTEN — hybrid semantic search PR, AC-HY-6/AC-HY-7):
    Given a semantic block with 5 parts carrying KNOWN 384-d embeddings and
    a query_vector passed to rank_results, where two of the parts
    (TIE-APPLE/TIE-ZEBRA) are DELIBERATELY given embeddings pointing in the
    EXACT SAME direction as the top (HIGH) part — a genuine 3-way cosine tie
    at 1.0 — so the mpn_norm tie-break is actually exercised, not just
    assumed.
    When rank_results(blocks, parsed, query_vector=query_vector) is called
    twice.
    Then:
    - All 5 parts appear in the result.
    - The order is the same on both calls (deterministic).
    - nearest_match is False (semantic is a HARD tier; a query_vector must
      never turn a confident embedding hit into the relaxed/nearest path).
    - Rows are ordered by cosine similarity to query_vector DESCENDING —
      HAND-COMPUTED: HIGH/TIE-APPLE/TIE-ZEBRA all have cosine=1.0 (tied,
      tie-broken by mpn_norm ascending: HIGH < TIE-APPLE < TIE-ZEBRA), MID
      has cosine=1/sqrt(2)~=0.70711, LOW has cosine=1/sqrt(3)~=0.57735. This
      is NOT plain mpn_norm-alphabetic order (which would put LOW before
      MID: "HIGH,LOW,MID,TIE-APPLE,TIE-ZEBRA") — proving cosine, not
      alphabetic order, genuinely drives the ranking.

    CHANGED FROM PRE-HYBRID (documented, not silent): the pre-hybrid version
    of this test asserted plain mpn_norm-ascending order (no query_vector
    existed). With the hybrid `query_vector` kwarg, semantic-tier rows are
    now ordered by cosine similarity DESCENDING (tie-break mpn_norm, then
    uid) instead.
    """
    query_vector = [1.0] + [0.0] * 383

    # HIGH: identical direction to the query -> cosine = 1.0.
    high_embedding = [1.0] + [0.0] * 383
    # MID: 45-degree-ish direction -> cosine = 1/sqrt(2) ~= 0.70711.
    mid_embedding = [1.0, 1.0] + [0.0] * 382
    # LOW: further off-axis -> cosine = 1/sqrt(3) ~= 0.57735.
    low_embedding = [1.0, 1.0, 1.0] + [0.0] * 381
    # TIE-APPLE / TIE-ZEBRA: SAME direction as HIGH (cosine = 1.0 for both —
    # a genuine, deliberate cosine tie), differing only in magnitude (cosine
    # similarity is magnitude-invariant).
    tie_apple_embedding = [1.0] + [0.0] * 383
    tie_zebra_embedding = [5.0] + [0.0] * 383

    def _sem_part(uid: str, mpn_norm: str, embedding: list[float]) -> dict:
        return {
            "uid": uid, "mpn": mpn_norm, "mpn_norm": mpn_norm,
            "embedding": embedding,
        }

    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "exact":    [],
        "trig":     [],
        "fts":      [],
        "semantic": [
            _sem_part("0xSA1", "LOW", low_embedding),
            _sem_part("0xSA2", "MID", mid_embedding),
            _sem_part("0xSA3", "HIGH", high_embedding),
            _sem_part("0xSA4", "TIE-ZEBRA", tie_zebra_embedding),
            _sem_part("0xSA5", "TIE-APPLE", tie_apple_embedding),
        ],
    }

    result1 = rank_results(blocks, parsed, query_vector=query_vector)
    result2 = rank_results(blocks, parsed, query_vector=query_vector)

    mpn1 = [row.mpn_norm for row in result1.rows]
    mpn2 = [row.mpn_norm for row in result2.rows]

    assert len(mpn1) == 5, f"AC-SR-4: must return 5 rows. Got: {mpn1}"
    assert mpn1 == mpn2, (
        f"AC-SR-4: semantic-only result must be deterministic. "
        f"Got different orders: {mpn1} vs {mpn2}"
    )
    assert result1.nearest_match is False, (
        f"AC-SR-4: nearest_match must be False for semantic-only result, "
        f"even with query_vector supplied. Got: {result1.nearest_match}"
    )

    # The three cosine==1.0 rows must tie-break by mpn_norm ascending among
    # themselves, and rank ahead of MID/LOW.
    assert mpn1[:3] == ["HIGH", "TIE-APPLE", "TIE-ZEBRA"], (
        f"AC-SR-4: the three cosine==1.0 rows must tie-break by mpn_norm "
        f"ascending. Got: {mpn1}"
    )
    # MID (cosine ~0.70711) must rank above LOW (cosine ~0.57735).
    assert mpn1[3:] == ["MID", "LOW"], (
        f"AC-SR-4: MID (cosine~0.70711) must rank above LOW "
        f"(cosine~0.57735). Got: {mpn1}"
    )


def test_ac_sr_existing_tier_scores_preserved() -> None:
    """Regression guard: existing PR3 tier scores must still work after PR4 extension.

    Given blocks with exact, trig, fts parts.
    When rank_results is called.
    Then exact > trig > fts ordering is preserved (PR3 contract).
    This test must stay green to prove PR4 does not break PR3 tier ordering.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [_part("0xP01", "MAX232", stock=0)],
        "trig":  [_part("0xP02", "MAX232A", stock=0)],
        "fts":   [_part("0xP03", "SN65C1232", stock=0)],
    }

    result = rank_results(blocks, parsed)
    mpn_norms = [row.mpn_norm for row in result.rows]

    idx_exact = mpn_norms.index("MAX232")
    idx_trig  = mpn_norms.index("MAX232A")
    idx_fts   = mpn_norms.index("SN65C1232")

    assert idx_exact < idx_trig < idx_fts, (
        f"Regression: PR3 tier ordering must be preserved after PR4. "
        f"Order: {mpn_norms}"
    )


# ===========================================================================
# AC-SF-19..23 + AC-SF-32: issue #15 PR2 — `rank_results(..., sort=...)` and
# RankedRow.price_usd / RankedRow.category
#
# NEW contract (not yet implemented):
#   rank_results(blocks, parsed, *, sort: str = "relevance") -> RankedResults
#   sort is one of {"relevance", "stock", "price"}; "relevance" is today's
#   default (-tier, stock>0, is_basic, mpn_norm, uid) order, unchanged.
#   RankedRow gains price_usd: float | None and category: str | None,
#   populated in _make_row from the raw 'price_usd' / 'in_category:[{name}]'
#   predicates (mirrors the existing manufacturer/package_name propagation).
#
# RED reasons in this section are DELIBERATELY split:
#   - AC-SF-19..23 (sort=...) fail with `TypeError: rank_results() got an
#     unexpected keyword argument 'sort'` — sort does not exist as a
#     parameter yet. Not a collection error: rank_results itself already
#     exists and imports fine today.
#   - AC-SF-32 (price_usd/category) tests deliberately call rank_results
#     WITHOUT sort=..., so they fail on a clean, isolated
#     `AttributeError: 'RankedRow' object has no attribute 'price_usd'`/
#     `'category'` — the missing-field reason, not entangled with the
#     missing-sort-kwarg reason above.
# ===========================================================================

def _raw(uid: str, mpn_norm: str, **extra) -> dict:
    """Build a bare raw DQL part dict with ONLY uid/mpn/mpn_norm plus whatever
    predicates are explicitly passed as keyword arguments.

    Unlike _part()/_part_rich(), this never injects a default stock/is_basic,
    so a predicate can be genuinely ABSENT from the dict — simulating a node
    where that DQL predicate was never set (needed for the None-stock/
    None-price_usd "missing predicate" test cases below).
    """
    row: dict = {"uid": uid, "mpn": mpn_norm, "mpn_norm": mpn_norm}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# AC-SF-19: no sort arg / sort="relevance" == today's default order
# ---------------------------------------------------------------------------

def test_ac_sf_19_no_sort_arg_matches_pinned_tier_stock_basic_mpn_uid_order() -> None:
    """AC-SF-19 (regression pin — PASSES TODAY, no new kwarg used): Given a
    fixture spanning tier, the in-tier stock boost, the is_basic boost, and
    the mpn_norm/uid tie-breaks.
    When rank_results(blocks, parsed) is called with NO sort argument.
    Then the row order is EXACTLY the documented
    (-tier, stock>0, is_basic, mpn_norm, uid) order — i.e. today's behavior,
    captured as a golden fixture so PR2 cannot silently change the
    no-sort-arg default.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part("0xF4", "ZZZ", stock=0, is_basic=False),   # exact, no boost.
            _part("0xF3", "MMM", stock=0, is_basic=True),    # exact, is_basic boost.
            _part("0xF2", "AAA", stock=5, is_basic=False),   # exact, stock boost (wins).
        ],
        "trig": [
            _part("0xF1", "AAA", stock=100, is_basic=True),  # trig -> ranks below ALL exact.
        ],
    }

    result = rank_results(blocks, parsed)  # NO sort kwarg — today's default.

    uids_in_order = [row.uid for row in result.rows]
    assert uids_in_order == ["0xF2", "0xF3", "0xF4", "0xF1"], (
        "AC-SF-19 regression: no-sort-arg order must stay "
        f"(-tier, stock>0, is_basic, mpn_norm, uid). Got: {uids_in_order}"
    )


def test_ac_sf_19_sort_relevance_kwarg_matches_default_order() -> None:
    """AC-SF-19: Given the SAME fixture as the regression-pin test above.
    When rank_results(blocks, parsed, sort="relevance") is called.
    Then the row order is IDENTICAL to the no-sort-arg call — sort="relevance"
    is an explicit alias for today's default tier/stock/is_basic/mpn_norm/uid
    order.

    RED: `sort` does not exist as a rank_results parameter yet -> TypeError.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part("0xF4", "ZZZ", stock=0, is_basic=False),
            _part("0xF3", "MMM", stock=0, is_basic=True),
            _part("0xF2", "AAA", stock=5, is_basic=False),
        ],
        "trig": [
            _part("0xF1", "AAA", stock=100, is_basic=True),
        ],
    }

    default_result = rank_results(blocks, parsed)
    explicit_result = rank_results(blocks, parsed, sort="relevance")

    default_uids = [row.uid for row in default_result.rows]
    explicit_uids = [row.uid for row in explicit_result.rows]
    assert explicit_uids == default_uids, (
        f"AC-SF-19: sort='relevance' must match the no-sort-arg default order. "
        f"default={default_uids} explicit={explicit_uids}"
    )


# ---------------------------------------------------------------------------
# AC-SF-20: sort="stock" -> stock DESC, tier NOT a key, None -> 0 (last)
# ---------------------------------------------------------------------------

def test_ac_sf_20_sort_stock_orders_by_stock_desc_ignoring_tier() -> None:
    """AC-SF-20: Given a fts-tier part with HIGH stock and an exact-tier part
    with LOW stock (distinct uids — no dedup collision).
    When rank_results(blocks, parsed, sort="stock") is called.
    Then the fts-tier (higher stock) row ranks ABOVE the exact-tier (lower
    stock) row — proving tier is NOT a sort key under sort="stock".

    RED: `sort` kwarg does not exist yet -> TypeError.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [_part("0xG1", "LOWSTOCK", stock=5)],
        "fts":   [_part("0xG2", "HIGHSTOCK", stock=500)],
    }

    result = rank_results(blocks, parsed, sort="stock")
    uids = [row.uid for row in result.rows]

    assert uids == ["0xG2", "0xG1"], (
        f"AC-SF-20: sort='stock' must rank by stock DESC regardless of tier. "
        f"Got: {uids}"
    )


def test_ac_sf_20_sort_stock_tier_never_breaks_ties() -> None:
    """AC-SF-20: Given two parts with EQUAL stock and EQUAL mpn_norm, one in
    'exact' and one in 'fts'.
    When rank_results(blocks, parsed, sort="stock") is called.
    Then the tie is broken by uid ALONE — tier contributes nothing to the
    sort key (unlike sort="relevance", where tier is primary).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [_part("0xG42", "SAME", stock=10)],
        "fts":   [_part("0xG41", "SAME", stock=10)],
    }

    result = rank_results(blocks, parsed, sort="stock")
    uids = [row.uid for row in result.rows]

    assert uids == ["0xG41", "0xG42"], (
        f"AC-SF-20: equal stock+mpn_norm ties must break by uid alone "
        f"(tier must not be a sort key under sort='stock'). Got: {uids}"
    )


def test_ac_sf_20_sort_stock_ties_broken_by_mpn_norm_then_uid() -> None:
    """AC-SF-20: Given three parts with EQUAL stock.
    When rank_results(blocks, parsed, sort="stock") is called.
    Then ties are broken by mpn_norm ascending, then uid ascending.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part("0xG13", "ZZZ", stock=10),
            _part("0xG11", "AAA", stock=10),
            _part("0xG12", "AAA", stock=10),
        ],
    }

    result = rank_results(blocks, parsed, sort="stock")
    order = [(row.mpn_norm, row.uid) for row in result.rows]

    assert order == [("AAA", "0xG11"), ("AAA", "0xG12"), ("ZZZ", "0xG13")], (
        f"AC-SF-20: equal-stock ties must break by mpn_norm then uid. Got: {order}"
    )


def test_ac_sf_20_sort_stock_none_treated_as_zero_sorts_last() -> None:
    """AC-SF-20: Given one part with stock=20 (present) and one with the
    'stock' predicate entirely ABSENT from the raw DQL dict (row.stock is
    None).
    When rank_results(blocks, parsed, sort="stock") is called.
    Then the None-stock row is treated as 0 and sorts LAST (behind the real,
    positive stock) — no crash from comparing None to an int.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _raw("0xG21", "HASSTOCK", stock=20),
            _raw("0xG22", "NOSTOCKFIELD"),  # no "stock" key at all -> row.stock is None.
        ],
    }

    result = rank_results(blocks, parsed, sort="stock")
    uids = [row.uid for row in result.rows]

    assert uids == ["0xG21", "0xG22"], (
        f"AC-SF-20: a None stock must sort as 0 (last, behind real stock). "
        f"Got: {uids}"
    )


def test_ac_sf_20_sort_stock_all_none_orders_by_mpn_norm_then_uid_no_error() -> None:
    """AC-SF-20: Given ALL rows with the 'stock' predicate entirely absent.
    When rank_results(blocks, parsed, sort="stock") is called.
    Then no error is raised and rows are ordered by mpn_norm then uid (the
    all-None tie-break — never a TypeError from comparing None to an int).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _raw("0xG32", "ZZZ"),
            _raw("0xG31", "AAA"),
        ],
    }

    result = rank_results(blocks, parsed, sort="stock")
    order = [(row.mpn_norm, row.uid) for row in result.rows]

    assert order == [("AAA", "0xG31"), ("ZZZ", "0xG32")], (
        f"AC-SF-20: all-None stock must fall back to mpn_norm/uid order. Got: {order}"
    )


# ---------------------------------------------------------------------------
# AC-SF-21: sort="price" -> price_usd ASC, missing LAST, 0.0 is valid
# ---------------------------------------------------------------------------

def test_ac_sf_21_sort_price_ascending_missing_price_last() -> None:
    """AC-SF-21: Given price_usd=0.50, price_usd=0.10, and price_usd absent
    entirely (never set on the node).
    When rank_results(blocks, parsed, sort="price") is called.
    Then order is ascending price_usd with the missing-price row LAST:
    [0.10, 0.50, missing].

    RED: `sort` kwarg does not exist yet -> TypeError.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _raw("0xH1", "HIGH", price_usd=0.50),
            _raw("0xH2", "LOW", price_usd=0.10),
            _raw("0xH3", "NOPRICE"),  # price_usd predicate entirely absent.
        ],
    }

    result = rank_results(blocks, parsed, sort="price")
    uids = [row.uid for row in result.rows]

    assert uids == ["0xH2", "0xH1", "0xH3"], (
        f"AC-SF-21: sort='price' must be ascending with missing price LAST. "
        f"Got: {uids}"
    )


def test_ac_sf_21_sort_price_zero_is_valid_not_treated_as_missing() -> None:
    """AC-SF-21: Given price_usd=0.0 (a genuine free/zero price) and a row
    with the price_usd predicate entirely absent.
    When rank_results(blocks, parsed, sort="price") is called.
    Then the price_usd=0.0 row sorts FIRST (a real, present value) and the
    missing-price row sorts LAST — 0.0 must never be conflated with
    "missing".
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _raw("0xH11", "MISSING"),          # price_usd absent.
            _raw("0xH12", "FREE", price_usd=0.0),
            _raw("0xH13", "PAID", price_usd=1.0),
        ],
    }

    result = rank_results(blocks, parsed, sort="price")
    uids = [row.uid for row in result.rows]

    assert uids == ["0xH12", "0xH13", "0xH11"], (
        f"AC-SF-21: price_usd=0.0 must sort as a real (lowest) price, never "
        f"as missing; the truly-absent row must sort last. Got: {uids}"
    )


def test_ac_sf_21_sort_price_ties_broken_by_mpn_norm_then_uid() -> None:
    """AC-SF-21: Given three parts with EQUAL price_usd.
    When rank_results(blocks, parsed, sort="price") is called.
    Then ties are broken by mpn_norm ascending, then uid ascending.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _raw("0xH23", "ZZZ", price_usd=1.0),
            _raw("0xH21", "AAA", price_usd=1.0),
            _raw("0xH22", "AAA", price_usd=1.0),
        ],
    }

    result = rank_results(blocks, parsed, sort="price")
    order = [(row.mpn_norm, row.uid) for row in result.rows]

    assert order == [("AAA", "0xH21"), ("AAA", "0xH22"), ("ZZZ", "0xH23")], (
        f"AC-SF-21: equal-price ties must break by mpn_norm then uid. Got: {order}"
    )


def test_ac_sf_21_sort_price_all_missing_orders_by_mpn_norm_then_uid_no_error() -> None:
    """AC-SF-21: Given ALL rows with price_usd entirely absent.
    When rank_results(blocks, parsed, sort="price") is called.
    Then no error is raised and rows are ordered by mpn_norm then uid.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _raw("0xH32", "ZZZ"),
            _raw("0xH31", "AAA"),
        ],
    }

    result = rank_results(blocks, parsed, sort="price")
    order = [(row.mpn_norm, row.uid) for row in result.rows]

    assert order == [("AAA", "0xH31"), ("ZZZ", "0xH32")], (
        f"AC-SF-21: all-missing price must fall back to mpn_norm/uid order. Got: {order}"
    )


# ---------------------------------------------------------------------------
# AC-SF-23: nearest-match mode -> sort is a no-op
# ---------------------------------------------------------------------------

def test_ac_sf_23_nearest_match_order_identical_regardless_of_sort_value() -> None:
    """AC-SF-23: Given a nearest-match scenario (all hard blocks empty, a
    'nearest' block populated) with a parametric target.
    When rank_results is called once per sort in {"relevance", "stock", "price"}.
    Then the row order is IDENTICAL across all three — sort is a no-op in
    nearest-match mode; ordering always follows the parameter-distance
    (_distance, mpn_norm, uid) key.
    """
    parsed = _make_parsed(quantities=[_q("voltage_max", 1.2, "1.2V")])
    blocks = {
        "exact": [], "trig": [], "fts": [],
        "nearest": [
            _part_rich("0xJ1", "FAR", voltage_max=5.5, stock=1, price_usd=9.0),
            _part_rich("0xJ2", "CLOSE", voltage_max=1.8, stock=100, price_usd=0.1),
            _part_rich("0xJ3", "MID", voltage_max=3.3, stock=50, price_usd=1.0),
        ],
    }

    baseline = rank_results(blocks, parsed)  # today's default (implicit nearest sort).
    baseline_uids = [row.uid for row in baseline.rows]
    assert baseline_uids == ["0xJ2", "0xJ3", "0xJ1"], (
        "AC-SF-23 setup sanity: expected distance-ascending order CLOSE, MID, FAR. "
        f"Got: {baseline_uids}"
    )

    for sort_value in ("relevance", "stock", "price"):
        result = rank_results(blocks, parsed, sort=sort_value)
        uids = [row.uid for row in result.rows]
        assert uids == baseline_uids, (
            f"AC-SF-23: sort={sort_value!r} must be a no-op in nearest-match "
            f"mode (distance order preserved). Got: {uids}; expected {baseline_uids}"
        )


# ===========================================================================
# AC-SF-32: RankedRow gains price_usd / category, populated in _make_row
# ===========================================================================

def test_ac_sf_32_rank_row_propagates_price_usd_float() -> None:
    """AC-SF-32: Given a raw block dict with price_usd=1.23.
    When rank_results is called (NO sort kwarg — isolates this test from the
    AC-SF-19..23 sort-kwarg RED reason above).
    Then row.price_usd == 1.23 (float attribute directly on RankedRow).

    RED: RankedRow has no 'price_usd' field yet -> AttributeError.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {"exact": [_raw("0xK1", "PRICED232", price_usd=1.23)]}

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "price_usd"), (
        "AC-SF-32: RankedRow must expose 'price_usd' attribute (propagated "
        "from the 'price_usd' predicate in the raw dict)."
    )
    assert row.price_usd == pytest.approx(1.23), (
        f"AC-SF-32: row.price_usd must be 1.23. Got: {row.price_usd!r}"
    )


def test_ac_sf_32_rank_row_price_usd_none_when_absent() -> None:
    """AC-SF-32: Given a raw block dict with no price_usd field.
    When rank_results is called.
    Then row.price_usd is None (field present but nullable).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {"exact": [_raw("0xK2", "NOPRICE232")]}

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "price_usd"), (
        "AC-SF-32: RankedRow must always expose 'price_usd' (None when absent)."
    )
    assert row.price_usd is None, (
        f"AC-SF-32: row.price_usd must be None when absent. Got: {row.price_usd!r}"
    )


def test_ac_sf_32_rank_row_price_usd_zero_is_not_none() -> None:
    """AC-SF-32: Given price_usd=0.0 (a genuine zero price, not absence).
    When rank_results is called.
    Then row.price_usd == 0.0 exactly (never coerced to None).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {"exact": [_raw("0xK3", "FREE232", price_usd=0.0)]}

    result = rank_results(blocks, parsed)
    row = result.rows[0]

    assert row.price_usd == 0.0 and row.price_usd is not None, (
        f"AC-SF-32: price_usd=0.0 must round-trip as 0.0, not None. "
        f"Got: {row.price_usd!r}"
    )


def test_ac_sf_32_rank_row_propagates_category_from_in_category() -> None:
    """AC-SF-32: Given a raw block dict with in_category:[{name:"RS232 ICs"}].
    When rank_results is called.
    Then row.category == "RS232 ICs" (str, not None).

    RED: RankedRow has no 'category' field yet -> AttributeError.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _raw("0xK4", "CATTED232", in_category=[{"name": "RS232 ICs"}])
        ]
    }

    result = rank_results(blocks, parsed)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "category"), (
        "AC-SF-32: RankedRow must expose 'category' attribute (propagated "
        "from in_category[0].name in the raw dict)."
    )
    assert row.category == "RS232 ICs", (
        f"AC-SF-32: row.category must be 'RS232 ICs'. Got: {row.category!r}"
    )


def test_ac_sf_32_rank_row_category_none_when_in_category_absent() -> None:
    """AC-SF-32: Given a raw block dict with no in_category field.
    When rank_results is called.
    Then row.category is None.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {"exact": [_raw("0xK5", "NOCAT232")]}

    result = rank_results(blocks, parsed)
    row = result.rows[0]

    assert hasattr(row, "category"), (
        "AC-SF-32: RankedRow must always expose 'category' (None when absent)."
    )
    assert row.category is None, (
        f"AC-SF-32: row.category must be None when in_category is absent. "
        f"Got: {row.category!r}"
    )


def test_ac_sf_32_existing_ranked_row_fields_still_present_regression() -> None:
    """AC-SF-32 (regression guard — largely PASSES TODAY already, since it
    only inspects PRE-EXISTING fields via hasattr; it does not itself assert
    on price_usd/category): Given a fully-populated part dict.
    When rank_results is called.
    Then all PRE-EXISTING RankedRow fields (uid, mpn_norm, tier, mpn,
    manufacturer, package_name, datasheet_urls, stock, is_basic, and every
    promoted numeric predicate) are still present — adding price_usd/category
    must not remove or rename anything.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part_rich(
                "0xK6",
                "MAX232CPE",
                made_by=[{"name": "Texas Instruments"}],
                in_package=[{"name": "PDIP-16"}],
                datasheet=[{"url": "https://example.com/ds.pdf"}],
                voltage_max=5.5,
                resistance=None,
                tolerance_pct=None,
                price_usd=2.5,
                in_category=[{"name": "RS232 ICs"}],
            )
        ]
    }

    result = rank_results(blocks, parsed)
    row = result.rows[0]

    for field_name in (
        "uid", "mpn_norm", "tier", "mpn", "manufacturer", "package_name",
        "datasheet_urls", "stock", "is_basic", "voltage_min", "voltage_max",
        "current_max", "resistance", "capacitance", "inductance",
        "frequency_max", "power", "tolerance_pct",
    ):
        assert hasattr(row, field_name), (
            f"AC-SF-32 regression: pre-existing RankedRow field {field_name!r} "
            f"must still be present after adding price_usd/category."
        )
    assert row.manufacturer == "Texas Instruments"
    assert row.package_name == "PDIP-16"
    assert row.voltage_max == pytest.approx(5.5)


# ===========================================================================
# AC-HY: hybrid semantic search robustness (Gate-1 ratified contract)
#
# New rank_results signature:
#   rank_results(blocks, parsed, *, sort="relevance", query_vector=None,
#                result_limit=None) -> RankedResults
#
# When query_vector is given, semantic-tier rows get a computed
# `similarity` = cosine(query_vector, raw["embedding"]) (stdlib math only —
# see AC-HY-10), ordered cosine DESC with an mpn_norm/uid tie-break.
# RankedRow gains a scalar `similarity: float | None` field (cosine for
# semantic rows, None otherwise); the raw 384-float vector itself is NEVER
# copied onto RankedRow (AC-HY-7). result_limit truncates the FINAL ordered
# row list to min(result_limit, MAX_RESULT_LIMIT) — for a semantic result
# this truncation happens AFTER the cosine re-rank, so the SURVIVING rows
# are always the top-cosine ones; a `sort="stock"/"price"` value then only
# re-orders the DISPLAY of those survivors (AC-HY-12).
#
# query_vector=None (the lexical path's default) must be byte-identical to
# today's behaviour — see test_ac_hy_12_query_vector_none_is_byte_identical_
# regression below.
#
# RED reason: rank_results does not accept query_vector=/result_limit= yet
# -> TypeError("unexpected keyword argument"), a per-test runtime failure
# (rank_results itself already exists and imports fine today), never a
# collection error.
# ===========================================================================

_EMBED_DIM = 384


def _sem_part(uid: str, mpn_norm: str, embedding: list[float], **extra) -> dict:
    """Build a minimal semantic-tier raw part dict carrying a raw embedding."""
    row: dict = {"uid": uid, "mpn": mpn_norm, "mpn_norm": mpn_norm, "embedding": embedding}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# AC-HY-6: cosine DESC order across the full [-1, 1] range.
# ---------------------------------------------------------------------------

def test_ac_hy_6_semantic_cosine_desc_order_with_negative_and_orthogonal_vectors() -> None:
    """AC-HY-6: Given a semantic block with parts whose embeddings are
    PARALLEL (cosine=1.0), ORTHOGONAL (cosine=0.0) and ANTI-PARALLEL
    (cosine=-1.0) to the query_vector.
    When rank_results(blocks, parsed, query_vector=query_vector) is called.
    Then rows are ordered cosine DESCENDING: PARALLEL (1.0), ORTHOGONAL
    (0.0), ANTI-PARALLEL (-1.0) — proving the full [-1, 1] cosine range
    sorts correctly, not just the positive-value cases.
    """
    query_vector = [1.0] + [0.0] * 383
    parallel = [2.0] + [0.0] * 383           # cosine = 1.0
    orthogonal = [0.0, 3.0] + [0.0] * 382    # cosine = 0.0
    anti_parallel = [-1.0] + [0.0] * 383     # cosine = -1.0

    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "semantic": [
            _sem_part("0xC1", "ANTIPART", anti_parallel),
            _sem_part("0xC2", "ORTHOPART", orthogonal),
            _sem_part("0xC3", "PARAPART", parallel),
        ],
    }

    result = rank_results(blocks, parsed, query_vector=query_vector)
    mpn_norms = [row.mpn_norm for row in result.rows]

    assert mpn_norms == ["PARAPART", "ORTHOPART", "ANTIPART"], (
        f"AC-HY-6: rows must be ordered cosine DESC across the full [-1, 1] "
        f"range (parallel=1.0, orthogonal=0.0, anti-parallel=-1.0). "
        f"Got: {mpn_norms}"
    )
    assert result.rows[0].similarity == pytest.approx(1.0)
    assert result.rows[1].similarity == pytest.approx(0.0)
    assert result.rows[2].similarity == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# AC-HY-7: RankedRow gets a scalar similarity, never the raw vector.
# ---------------------------------------------------------------------------

def test_ac_hy_7_ranked_row_gets_scalar_similarity_never_the_raw_vector() -> None:
    """AC-HY-7: Given a semantic-tier raw part dict carrying a 384-element
    'embedding' list and a query_vector.
    When rank_results(blocks, parsed, query_vector=query_vector) is called.
    Then the resulting RankedRow:
    - exposes a 'similarity' attribute that is a plain float (a SCALAR
      cosine value, not a list/vector).
    - has NO attribute named 'embedding' (the 384-float vector is never
      copied onto RankedRow).
    - has NO attribute anywhere on the instance holding a list of length
      384 (defence-in-depth: the raw vector must not leak onto the row
      under any field name).
    """
    query_vector = [1.0] + [0.0] * 383
    embedding = [1.0] + [0.0] * 383
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {"semantic": [_sem_part("0xV1", "MAX232CPE", embedding)]}

    result = rank_results(blocks, parsed, query_vector=query_vector)
    assert result.rows, "Expected at least one row."
    row = result.rows[0]

    assert hasattr(row, "similarity"), (
        "AC-HY-7: RankedRow must expose a 'similarity' attribute."
    )
    assert isinstance(row.similarity, float), (
        f"AC-HY-7: row.similarity must be a plain float (scalar cosine), "
        f"not a list/vector. Got: {type(row.similarity)!r} = {row.similarity!r}"
    )
    assert row.similarity == pytest.approx(1.0)

    assert not hasattr(row, "embedding"), (
        "AC-HY-7: RankedRow must NEVER carry an 'embedding' attribute "
        "(the 384-float vector must not be copied onto the row)."
    )
    for field_name, value in vars(row).items():
        assert not (isinstance(value, list) and len(value) == _EMBED_DIM), (
            f"AC-HY-7: RankedRow field {field_name!r} unexpectedly holds a "
            f"{_EMBED_DIM}-length list — the raw embedding vector must "
            f"never leak onto any RankedRow field."
        )


# ---------------------------------------------------------------------------
# AC-HY-10: cosine computed with stdlib math only — no numpy/torch import,
# neither at module scope nor at call time.
# ---------------------------------------------------------------------------

def test_ac_hy_10_ranker_module_source_has_no_numpy_or_torch_import() -> None:
    """AC-HY-10: Given the ranker.py source file.
    When read as text.
    Then it contains no 'import numpy' / 'import torch' / 'from numpy' /
    'from torch' statement — ranker.py sits on the lexical search path and
    must remain importable with the optional [embed] extra (numpy/torch via
    sentence-transformers) absent. Cosine similarity must be computed with
    stdlib math only.
    """
    import pathlib

    import partgraph.query.ranker as ranker_mod

    source = pathlib.Path(ranker_mod.__file__).read_text(encoding="utf-8")
    for banned in ("import numpy", "from numpy", "import torch", "from torch"):
        assert banned not in source, (
            f"AC-HY-10: ranker.py must not contain {banned!r} — cosine "
            f"similarity must use stdlib math only (no [embed]-only import "
            f"at module scope)."
        )


def test_ac_hy_10_fresh_ranker_import_and_call_succeed_with_numpy_torch_hidden() -> None:
    """AC-HY-10: Given numpy AND torch are made UNIMPORTABLE
    (patch.dict(sys.modules, {"numpy": None, "torch": None}) — mirrors
    tests/unit/test_embed.py's AC-IM-1 pattern for sentence_transformers)
    and partgraph.query.ranker is loaded FRESH from its source file under
    that condition, via importlib into a PRIVATE sys.modules key so the
    session-wide cached partgraph.query.ranker module is never touched
    (avoiding the class-identity pollution a real reload() of the shared
    module would risk for other test files' isinstance/import checks — the
    same collection-order sys.modules hygiene fixed in PR #22).
    When the freshly-loaded module's rank_results(blocks, parsed,
    query_vector=<vector>) is called on a semantic-tier row carrying a raw
    'embedding'.
    Then the fresh import itself succeeds (proving no MODULE-SCOPE numpy/
    torch import) and the call computes a correct cosine similarity with no
    ImportError (proving no call-time/lazy numpy/torch import either) —
    cosine is stdlib math only.
    """
    import importlib.util
    import sys as _sys
    from unittest.mock import patch as _patch

    import partgraph.query.ranker as _real_ranker_mod

    parsed = _make_parsed(text_tokens=["rs232"])
    vector = [1.0] + [0.0] * 383
    blocks = {"semantic": [_sem_part("0xNT1", "MAX232CPE", [1.0] + [0.0] * 383)]}

    private_name = "partgraph_query_ranker_ac_hy_10_isolated_reimport"
    spec = importlib.util.spec_from_file_location(private_name, _real_ranker_mod.__file__)
    fresh_module = importlib.util.module_from_spec(spec)

    with _patch.dict(_sys.modules, {"numpy": None, "torch": None, private_name: fresh_module}):
        spec.loader.exec_module(fresh_module)  # the "fresh import" under numpy/torch-hidden.
        result = fresh_module.rank_results(blocks, parsed, query_vector=vector)

    assert result.rows, "AC-HY-10: expected at least one row from the semantic block."
    assert result.rows[0].similarity == pytest.approx(1.0), (
        f"AC-HY-10: identical vectors must give cosine similarity 1.0 from "
        f"a fresh, isolated ranker import with numpy/torch hidden. "
        f"Got: {result.rows[0].similarity!r}"
    )


# ---------------------------------------------------------------------------
# AC-HY-11: result_limit truncates AFTER the cosine re-rank.
# ---------------------------------------------------------------------------

def test_ac_hy_11_result_limit_truncates_after_cosine_rerank() -> None:
    """AC-HY-11: Given a semantic block with 5 parts carrying DISTINCT,
    strictly-decreasing cosine similarities to query_vector, and
    result_limit=3.
    When rank_results(blocks, parsed, query_vector=query_vector,
    result_limit=3) is called.
    Then exactly 3 rows are returned — the TOP 3 by cosine (truncation
    happens AFTER the cosine re-rank, so the highest-similarity rows
    survive, not an arbitrary/insertion-order-based subset).
    """
    query_vector = [1.0] + [0.0] * 383

    def _emb(scale: float) -> list[float]:
        # Shared direction with the query plus a per-row-unique off-axis
        # component -> cosine = 1/sqrt(1+scale**2), strictly decreasing as
        # scale increases.
        return [1.0, scale] + [0.0] * 382

    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "semantic": [
            _sem_part("0xL0", "R0", _emb(0.0)),
            _sem_part("0xL1", "R1", _emb(1.0)),
            _sem_part("0xL2", "R2", _emb(2.0)),
            _sem_part("0xL3", "R3", _emb(3.0)),
            _sem_part("0xL4", "R4", _emb(4.0)),
        ],
    }

    result = rank_results(blocks, parsed, query_vector=query_vector, result_limit=3)

    mpn_norms = [row.mpn_norm for row in result.rows]
    assert len(result.rows) == 3, (
        f"AC-HY-11: result_limit=3 must truncate to exactly 3 rows. "
        f"Got {len(result.rows)}: {mpn_norms}"
    )
    assert mpn_norms == ["R0", "R1", "R2"], (
        f"AC-HY-11: truncation must keep the TOP 3 rows BY COSINE (highest "
        f"similarity survives), not an arbitrary subset. Got: {mpn_norms}"
    )


def test_ac_hy_11_result_limit_caps_at_max_result_limit_200_even_when_larger() -> None:
    """AC-HY-11: Given 250 semantic rows with distinct cosine values and
    result_limit=99999 (far above MAX_RESULT_LIMIT=200).
    When rank_results(blocks, parsed, query_vector=query_vector,
    result_limit=99999) is called.
    Then AT MOST 200 rows are returned — result_limit itself is clamped to
    MAX_RESULT_LIMIT (the RESULT bound is never bypassed by an oversized
    result_limit).
    """
    from partgraph.query.dql_builder import MAX_RESULT_LIMIT

    query_vector = [1.0] + [0.0] * 383
    row_count = MAX_RESULT_LIMIT + 50

    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "semantic": [
            _sem_part(f"0xM{idx:04d}", f"R{idx:04d}", [1.0, float(idx)] + [0.0] * 382)
            for idx in range(row_count)
        ]
    }

    result = rank_results(blocks, parsed, query_vector=query_vector, result_limit=99999)

    assert len(result.rows) <= MAX_RESULT_LIMIT, (
        f"AC-HY-11: result_limit=99999 must still be capped at "
        f"MAX_RESULT_LIMIT={MAX_RESULT_LIMIT}. Got {len(result.rows)} rows."
    )


def test_ac_hy_11_edge_fewer_survivors_than_result_limit_shows_all() -> None:
    """AC-HY-11 edge case: Given only 2 semantic rows and result_limit=50
    (far more than the available rows).
    When rank_results(blocks, parsed, query_vector=query_vector,
    result_limit=50) is called.
    Then BOTH rows are returned — result_limit never pads or errors when
    there are fewer survivors than the requested limit.
    """
    query_vector = [1.0] + [0.0] * 383
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "semantic": [
            _sem_part("0xF1", "ONLY1", [1.0] + [0.0] * 383),
            _sem_part("0xF2", "ONLY2", [0.0, 1.0] + [0.0] * 382),
        ],
    }

    result = rank_results(blocks, parsed, query_vector=query_vector, result_limit=50)

    assert len(result.rows) == 2, (
        f"AC-HY-11: fewer survivors than result_limit must show ALL of "
        f"them (no padding/error). Got {len(result.rows)} rows."
    )


# ---------------------------------------------------------------------------
# AC-HY-12: sort="stock"/"price" re-sorts the cosine-chosen survivors (never
# changes WHICH rows survive); query_vector=None is a byte-identical
# regression guard for the lexical path.
# ---------------------------------------------------------------------------

def test_ac_hy_12_sort_stock_re_sorts_cosine_survivors_not_the_full_pool() -> None:
    """AC-HY-12: Given 4 semantic rows: two are HIGH-cosine (survive a
    result_limit=2 truncation) with LOW stock, and two are LOW-cosine
    (truncated away) with HIGH stock.
    When rank_results(blocks, parsed, sort="stock", query_vector=query_vector,
    result_limit=2) is called.
    Then:
    - Exactly 2 rows are returned — the two HIGH-cosine rows (the SURVIVOR
      SET is chosen by cosine rank, never by stock — a highly-stocked but
      semantically-irrelevant part must NOT displace a more relevant one).
    - Among those 2 survivors, the DISPLAY order is stock DESCENDING (the
      requested sort="stock" re-orders the survivors; it does not change
      WHICH rows survived).
    """
    query_vector = [1.0] + [0.0] * 383

    def _stocked(uid: str, mpn_norm: str, *, scale: float, stock: int) -> dict:
        return _sem_part(uid, mpn_norm, [1.0, scale] + [0.0] * 382, stock=stock)

    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "semantic": [
            _stocked("0xS1", "HIGH-LOWSTOCK-A", scale=0.0, stock=1),    # cosine 1.0, survives
            _stocked("0xS2", "HIGH-LOWSTOCK-B", scale=0.1, stock=2),    # cosine ~0.995, survives
            _stocked("0xS3", "LOW-HIGHSTOCK-A", scale=9.0, stock=500),  # cosine ~0.11, truncated
            _stocked("0xS4", "LOW-HIGHSTOCK-B", scale=9.5, stock=999),  # cosine ~0.10, truncated
        ],
    }

    result = rank_results(
        blocks, parsed, sort="stock", query_vector=query_vector, result_limit=2
    )

    mpn_norms = [row.mpn_norm for row in result.rows]
    assert len(result.rows) == 2, (
        f"AC-HY-12: result_limit=2 must keep exactly 2 survivors. Got: {mpn_norms}"
    )
    assert set(mpn_norms) == {"HIGH-LOWSTOCK-A", "HIGH-LOWSTOCK-B"}, (
        f"AC-HY-12: the SURVIVOR SET must be chosen by cosine rank (the two "
        f"HIGH-cosine rows), regardless of the far-higher stock on the "
        f"truncated-away rows. Got: {mpn_norms}"
    )
    # Among the 2 survivors: stock DESC -> B (stock=2) before A (stock=1).
    assert mpn_norms == ["HIGH-LOWSTOCK-B", "HIGH-LOWSTOCK-A"], (
        f"AC-HY-12: sort='stock' must re-order the SURVIVORS by stock DESC "
        f"(B has stock=2, A has stock=1). Got: {mpn_norms}"
    )


def test_ac_hy_12_query_vector_none_is_byte_identical_regression() -> None:
    """AC-HY-12 (regression): Given the EXACT AC-SF-19 golden fixture (tier +
    stock boost + is_basic boost + mpn_norm/uid tie-break).
    When rank_results is called WITHOUT query_vector (its default, None) —
    both with query_vector/result_limit entirely omitted, and with both
    explicitly passed as None.
    Then the row order is IDENTICAL to today's pinned
    (-tier, stock>0, is_basic, mpn_norm, uid) order — query_vector=None (the
    lexical path's default) must never change existing behaviour, and every
    row's similarity is None.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    blocks = {
        "exact": [
            _part("0xF4", "ZZZ", stock=0, is_basic=False),
            _part("0xF3", "MMM", stock=0, is_basic=True),
            _part("0xF2", "AAA", stock=5, is_basic=False),
        ],
        "trig": [
            _part("0xF1", "AAA", stock=100, is_basic=True),
        ],
    }

    baseline = rank_results(blocks, parsed)
    explicit_none = rank_results(blocks, parsed, query_vector=None, result_limit=None)

    baseline_uids = [row.uid for row in baseline.rows]
    explicit_uids = [row.uid for row in explicit_none.rows]

    assert explicit_uids == baseline_uids == ["0xF2", "0xF3", "0xF4", "0xF1"], (
        f"AC-HY-12: query_vector=None/result_limit=None must be byte-"
        f"identical to today's no-kwarg behaviour. "
        f"baseline={baseline_uids} explicit={explicit_uids}"
    )
    assert all(row.similarity is None for row in explicit_none.rows), (
        "AC-HY-12: without query_vector, every row.similarity must be None."
    )


# ---------------------------------------------------------------------------
# AC-HY edge cases: zero-norm query_vector, zero-norm stored embedding,
# missing raw embedding, and a genuine cosine tie broken by uid.
# ---------------------------------------------------------------------------

def test_ac_hy_edge_zero_norm_query_vector_gives_cosine_zero_no_crash() -> None:
    """AC-HY edge case: Given a ZERO-NORM query_vector (all zeros).
    When rank_results(blocks, parsed, query_vector=zero_vector) is called on
    a semantic row with a normal, non-zero embedding.
    Then no ZeroDivisionError is raised; row.similarity == 0.0 and the row
    is still returned.
    """
    zero_vector = [0.0] * _EMBED_DIM
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {"semantic": [_sem_part("0xZ1", "NORMALEMB", [1.0] + [0.0] * 383)]}

    result = rank_results(blocks, parsed, query_vector=zero_vector)

    assert result.rows, "Expected the row to still be present."
    assert result.rows[0].similarity == 0.0, (
        f"AC-HY edge: a zero-norm query_vector must give cosine=0.0 (never "
        f"raise ZeroDivisionError). Got: {result.rows[0].similarity!r}"
    )


def test_ac_hy_edge_zero_norm_embedding_gives_cosine_zero_sorts_last() -> None:
    """AC-HY edge case: Given a semantic block with one NORMAL-embedding row
    and one ZERO-VECTOR-embedding row (embedding=[0.0]*384 — a degenerate,
    all-zero stored embedding).
    When rank_results(blocks, parsed, query_vector=query_vector) is called.
    Then no ZeroDivisionError is raised; the zero-embedding row's similarity
    is 0.0 and it sorts AFTER the normal (positive-cosine) row.
    """
    query_vector = [1.0] + [0.0] * 383
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "semantic": [
            _sem_part("0xZ2", "ZEROEMB", [0.0] * _EMBED_DIM),
            _sem_part("0xZ3", "NORMALEMB", [1.0] + [0.0] * 383),
        ],
    }

    result = rank_results(blocks, parsed, query_vector=query_vector)

    mpn_norms = [row.mpn_norm for row in result.rows]
    assert mpn_norms == ["NORMALEMB", "ZEROEMB"], (
        f"AC-HY edge: a zero-norm stored embedding must sort LAST (cosine "
        f"0.0), behind the normal positive-cosine row. Got: {mpn_norms}"
    )
    zero_row = next(row for row in result.rows if row.mpn_norm == "ZEROEMB")
    assert zero_row.similarity == 0.0, (
        f"AC-HY edge: zero-norm embedding must give similarity=0.0 (never "
        f"raise). Got: {zero_row.similarity!r}"
    )


def test_ac_hy_edge_missing_raw_embedding_similarity_none_sorts_last() -> None:
    """AC-HY edge case: Given a semantic-tier raw part dict with NO
    'embedding' key at all (e.g. a defensive/malformed response).
    When rank_results(blocks, parsed, query_vector=query_vector) is called
    alongside a normal row that DOES carry an embedding.
    Then the row missing 'embedding':
    - has row.similarity is None (never 0.0 — 0.0 is reserved for a
      genuine, computed zero-norm cosine; None means "could not compute at
      all").
    - sorts LAST, after the row with a real (positive) cosine.
    """
    query_vector = [1.0] + [0.0] * 383
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "semantic": [
            {"uid": "0xN1", "mpn_norm": "NOEMBED"},  # no 'embedding' key.
            _sem_part("0xN2", "HASEMBED", [1.0] + [0.0] * 383),
        ],
    }

    result = rank_results(blocks, parsed, query_vector=query_vector)

    mpn_norms = [row.mpn_norm for row in result.rows]
    assert mpn_norms == ["HASEMBED", "NOEMBED"], (
        f"AC-HY edge: a row with NO raw 'embedding' must sort LAST. "
        f"Got: {mpn_norms}"
    )
    missing_row = next(row for row in result.rows if row.mpn_norm == "NOEMBED")
    assert missing_row.similarity is None, (
        f"AC-HY edge: missing raw embedding must give similarity=None (not "
        f"0.0 — 0.0 is reserved for a genuinely-computed zero-norm cosine). "
        f"Got: {missing_row.similarity!r}"
    )


def test_ac_hy_edge_cosine_tie_breaks_by_mpn_norm_then_uid() -> None:
    """AC-HY edge case: Given two semantic rows with IDENTICAL cosine
    similarity (embeddings pointing in the same direction) and IDENTICAL
    mpn_norm, differing only by uid.
    When rank_results(blocks, parsed, query_vector=query_vector) is called.
    Then the row with the lexicographically SMALLER uid sorts first
    (deterministic secondary/tertiary tie-break: mpn_norm then uid).
    """
    query_vector = [1.0] + [0.0] * 383
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "semantic": [
            _sem_part("0xTB2", "SAMEDIR", [1.0] + [0.0] * 383),
            _sem_part("0xTB1", "SAMEDIR", [2.0] + [0.0] * 383),
        ],
    }

    result = rank_results(blocks, parsed, query_vector=query_vector)
    uids = [row.uid for row in result.rows]

    assert uids == ["0xTB1", "0xTB2"], (
        f"AC-HY edge: equal cosine + equal mpn_norm must tie-break by uid "
        f"ascending. Got: {uids}"
    )


# ---------------------------------------------------------------------------
# Drift-guard (architecture; Gate-3 MUST-fix): the ranker's PRIVATE result
# ceiling is a deliberate DUPLICATE of dql_builder.MAX_RESULT_LIMIT — NOT an
# import of it, to avoid a sibling module-import cycle (ADR-0020). This guard
# keeps the two in lockstep so the duplicate can never silently drift from the
# canonical source of truth.
# ---------------------------------------------------------------------------

def test_drift_guard_ranker_result_ceiling_equals_dql_builder_max_result_limit() -> None:
    """Drift-guard (ADR-0020): Given the ranker's private ``_MAX_RESULT_ROWS``
    and ``dql_builder.MAX_RESULT_LIMIT``.
    When both are read.
    Then they are EQUAL — the ranker duplicates (never imports) the RESULT
    bound to avoid a sibling import cycle, and this guard fails loudly if the
    duplicate ever drifts from the canonical dql_builder value.
    """
    from partgraph.query.dql_builder import MAX_RESULT_LIMIT
    from partgraph.query.ranker import _MAX_RESULT_ROWS

    assert _MAX_RESULT_ROWS == MAX_RESULT_LIMIT, (
        f"Drift-guard: ranker._MAX_RESULT_ROWS ({_MAX_RESULT_ROWS}) must equal "
        f"dql_builder.MAX_RESULT_LIMIT ({MAX_RESULT_LIMIT}) — the ranker "
        f"duplicates (not imports) the RESULT bound (ADR-0020); the two must be "
        f"updated together."
    )


# ---------------------------------------------------------------------------
# Security F3 (Gate-3 MUST-fix): result_limit=0 / negative must yield a clean
# empty result, never a negative-index slice artifact (e.g. rows[:-5] silently
# returning the first N-5 rows instead of []).
# ---------------------------------------------------------------------------

def test_security_f3_result_limit_zero_and_negative_give_clean_empty_never_negative_slice() -> None:
    """Security F3: Given 4 semantic rows with distinct cosine similarities and
    a query_vector.
    When rank_results is called with result_limit=0 and again with
    result_limit=-5.
    Then BOTH return an empty list — the clamp is
    ``max(0, min(result_limit, _MAX_RESULT_ROWS))``, so a zero/negative --limit
    can never become a negative Python slice (``rows[:-5]`` would wrongly return
    the first N-5 rows instead of []).
    """
    query_vector = [1.0] + [0.0] * 383
    parsed = _make_parsed(text_tokens=["rs232"])
    blocks = {
        "semantic": [
            _sem_part("0xF30", "R0", [1.0, 0.0] + [0.0] * 382),
            _sem_part("0xF31", "R1", [1.0, 1.0] + [0.0] * 382),
            _sem_part("0xF32", "R2", [1.0, 2.0] + [0.0] * 382),
            _sem_part("0xF33", "R3", [1.0, 3.0] + [0.0] * 382),
        ],
    }

    zero = rank_results(blocks, parsed, query_vector=query_vector, result_limit=0)
    assert zero.rows == [], (
        f"Security F3: result_limit=0 must give an empty result, never a "
        f"negative-slice artifact. Got {len(zero.rows)} rows: "
        f"{[r.mpn_norm for r in zero.rows]}"
    )

    negative = rank_results(blocks, parsed, query_vector=query_vector, result_limit=-5)
    assert negative.rows == [], (
        f"Security F3: result_limit=-5 must clamp to 0 (empty), never slice "
        f"rows[:-5] (which would wrongly return the first N-5 rows). Got "
        f"{len(negative.rows)} rows: {[r.mpn_norm for r in negative.rows]}"
    )
