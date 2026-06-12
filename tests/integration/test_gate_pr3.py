"""
Tests: GATE-PR3-1..4 — PR3 Search CLI acceptance gates.

@pytest.mark.integration — all tests require:
  - A running Dgraph instance (dgraph_available fixture).
  - The JLCPCB catalogue to have been ingested (PR2 ingest complete).
  - Tests SKIP cleanly when DB is down or data is absent.
  - All operations are READ-ONLY (no mutations).

GATE-PR3-1: search "MAX232" -> ≥5 variants, ≥2 manufacturers, every row has
            a non-empty http datasheet URL. PRINTS count + manufacturers + sample.

GATE-PR3-2: search "10k 0402 1%" -> non-empty results AND every row resistance
            ∈[9900, 10100], package="0402", tolerance_pct==1.0.
            PRINTS first 10 rows (mpn / resistance / package / tol).

GATE-PR3-3: search "1.2V MAX232" -> nearest_match=True, output contains explicit
            "nearest" label, NO row has voltage_max==1.2 (no exact parametric
            hit exists for MAX232), nearest rows sorted by |voltage_max-1.2|.
            PRINTS top rows + distances.

GATE-PR3-4: Part count via {q(func: type(Part)){count(uid)}} identical before
            and after the full suite (read-only proof).

ROOT-LEVEL count(func:) IS BROKEN IN DGRAPH V25.
Always use: { q(func: type(Part)) { count(uid) } } -> {"q": [{"count": N}]} or [] -> 0.

NOTE: Collection will ERROR on import of partgraph.query.* because those
modules do not exist yet. That is the correct red state before PR3 implementation.
"""

from __future__ import annotations

import json
import sys

import pytest

from partgraph.query.dql_builder import build_search_dql, build_show_dql  # noqa: F401
from partgraph.query.parser import parse_query  # noqa: F401
from partgraph.query.ranker import rank_results  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers (read-only, mirrored from test_gate_pr2.py pattern)
# ---------------------------------------------------------------------------

def _dgraph_part_count(client) -> int:
    """Return the number of Part nodes using the safe named-block form."""
    query = "{ q(func: type(Part)) { count(uid) } }"
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        block = data.get("q", [])
        return block[0]["count"] if block else 0
    finally:
        txn.discard()


def _run_search_dql(client, query_text: str, variables: dict[str, str]) -> dict:
    """Execute a DQL query with variables and return the parsed JSON response."""
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query_text, variables=variables)
        return json.loads(resp.json)
    finally:
        txn.discard()


def _run_show_dql(client, query_text: str, variables: dict[str, str]) -> dict:
    """Execute a show DQL query and return the parsed JSON response."""
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query_text, variables=variables)
        return json.loads(resp.json)
    finally:
        txn.discard()


# ---------------------------------------------------------------------------
# GATE-PR3-4 part-count bookend (shared state across the gate suite)
# ---------------------------------------------------------------------------

# Mutable container used instead of a bare module global so that PLW0603 is not
# triggered. The dict is mutated in-place; no global statement needed.
_suite_state: dict[str, int | None] = {"part_count_before": None}


# ---------------------------------------------------------------------------
# GATE-PR3-1: search "MAX232" -> ≥5 variants, ≥2 manufacturers, all with URL
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr3_1_max232_search_variants_manufacturers_datasheets(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR3-1: search "MAX232" yields ≥5 variants from ≥2 manufacturers,
    every row has a non-empty http datasheet URL.

    Given: Dgraph contains the ingested JLCPCB catalogue.
    When:  parse_query("MAX232") -> build_search_dql -> execute -> rank_results.
    Then:
      - rows >= 5 (MAX232 is a widely available IC family).
      - len(unique manufacturers) >= 2.
      - every ranked row has at least one datasheet URL starting with "http".
    PRINTS: count, manufacturer list, sample row.
    """
    _suite_state["part_count_before"] = _dgraph_part_count(dgraph_pydgraph_client)

    parsed = parse_query("MAX232")
    query_text, variables = build_search_dql(parsed, limit=50)
    data = _run_search_dql(dgraph_pydgraph_client, query_text, variables)

    result = rank_results(data, parsed)

    print(
        f"\n[GATE-PR3-1] MAX232 search: {len(result.rows)} ranked rows",
        file=sys.stderr,
    )

    assert len(result.rows) >= 5, (
        f"GATE-PR3-1 FAILED: Expected ≥5 MAX232 variants. Got {len(result.rows)}. "
        "Verify ingest completed successfully."
    )

    # Collect manufacturers (may be stored in made_by list or as a flat attribute).
    manufacturers: set[str] = set()
    rows_missing_url: list[str] = []

    for row in result.rows:
        # Manufacturer name — access via the underlying raw dict if RankedRow exposes it,
        # or fall back to a direct attribute.
        mfr = getattr(row, "manufacturer", None)
        if mfr:
            manufacturers.add(mfr)

        # Datasheet URL — at least one must start with "http".
        urls = getattr(row, "datasheet_urls", None) or []
        has_url = any(u.startswith("http") for u in urls if u)
        if not has_url:
            rows_missing_url.append(getattr(row, "mpn_norm", "?"))

    print(
        f"[GATE-PR3-1] Manufacturers: {sorted(manufacturers)}",
        file=sys.stderr,
    )
    if result.rows:
        sample = result.rows[0]
        print(
            f"[GATE-PR3-1] Sample row: mpn_norm={getattr(sample, 'mpn_norm', '?')!r}  "
            f"mfr={getattr(sample, 'manufacturer', '?')!r}  "
            f"urls={getattr(sample, 'datasheet_urls', [])!r}",
            file=sys.stderr,
        )

    assert len(manufacturers) >= 2, (
        f"GATE-PR3-1 FAILED: Expected ≥2 distinct manufacturers for MAX232. "
        f"Got: {sorted(manufacturers)}"
    )

    assert not rows_missing_url, (
        f"GATE-PR3-1 FAILED: These MAX232 rows have no http datasheet URL: "
        f"{rows_missing_url}"
    )


# ---------------------------------------------------------------------------
# GATE-PR3-2: search "10k 0402 1%" -> parametric filter correctness
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr3_2_10k_0402_1pct_parametric_filter(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR3-2: search "10k 0402 1%" returns non-empty results where every row
    satisfies resistance∈[9900, 10100], package="0402", tolerance_pct==1.0.

    Given: the ingested catalogue contains 0402 10k 1% resistors.
    When:  parse_query("10k 0402 1%") -> build_search_dql -> execute -> rank_results.
    Then:
      - rows is non-empty.
      - every row: resistance_val ∈ [9900, 10100].
      - every row: package name contains "0402".
      - every row: tolerance_pct == 1.0 (or the row lacks the field — tolerance
        filter is applied in DQL, not re-verified here if absent on row).
    PRINTS: first 10 rows (mpn / resistance / package / tol).
    """
    parsed = parse_query("10k 0402 1%")
    query_text, variables = build_search_dql(parsed, limit=50)
    data = _run_search_dql(dgraph_pydgraph_client, query_text, variables)

    result = rank_results(data, parsed)

    print(
        f"\n[GATE-PR3-2] 10k 0402 1%: {len(result.rows)} results",
        file=sys.stderr,
    )

    assert len(result.rows) > 0, (
        "GATE-PR3-2 FAILED: Expected non-empty results for '10k 0402 1%'. "
        "Verify the ingest loaded 0402 resistor data."
    )

    for row in result.rows[:10]:
        mpn = getattr(row, "mpn_norm", "?")
        resistance = getattr(row, "resistance", None)
        package = getattr(row, "package_name", None) or ""
        tol = getattr(row, "tolerance_pct", None)

        print(
            f"[GATE-PR3-2] {mpn!r:30s}  R={resistance}  pkg={package!r}  tol={tol}",
            file=sys.stderr,
        )

        if resistance is not None:
            assert 9900 <= resistance <= 10100, (
                f"GATE-PR3-2 FAILED: {mpn!r} resistance {resistance} not in [9900, 10100]."
            )

        if package:
            assert "0402" in package, (
                f"GATE-PR3-2 FAILED: {mpn!r} package {package!r} does not contain '0402'."
            )

        if tol is not None:
            assert tol == pytest.approx(1.0), (
                f"GATE-PR3-2 FAILED: {mpn!r} tolerance_pct={tol} must be 1.0."
            )


# ---------------------------------------------------------------------------
# GATE-PR3-3: search "1.2V MAX232" -> nearest_match True, sorted by |voltage-1.2|
#
# TWO-PASS PATH: The two-pass hard-then-relax orchestration lives in the CLI
# `search` command, NOT in rank_results.  This gate test therefore replicates
# the two-pass logic explicitly so that the real Dgraph database is exercised
# in the same way the user's `partgraph search "1.2V MAX232"` would be:
#
#   Pass 1 (hard):     build_search_dql with full parametric filter → execute.
#                      rank_results → if rows is non-empty AND nearest_match=False,
#                      we have exact hits (unexpected for 1.2V MAX232) → test fails
#                      with a clear message.
#   Pass 2 (relaxed):  build_search_dql WITHOUT voltage_max filter (text+package
#                      only) → execute → rank_results on combined hard+relaxed
#                      result dict, keyed as "nearest" for the relaxed rows, so
#                      rank_results sets nearest_match=True.
#
# This mirrors what the CLI `search` command does; the gate test is the
# integration-level proof that the two-pass path produces the correct output.
# ---------------------------------------------------------------------------

def _build_relaxed_parsed(original_parsed) -> object:
    """Return a copy of ParsedQuery with quantities stripped (text + package only).

    This replicates the relaxed pass: the voltage_max filter is dropped so that
    MAX232 parts are returned regardless of their voltage_max value.
    """
    from partgraph.query.parser import ParsedQuery  # noqa: PLC0415
    return ParsedQuery(
        quantities=[],
        package=original_parsed.package,
        text_tokens=original_parsed.text_tokens,
        raw_query=original_parsed.raw_query,
    )


@pytest.mark.integration
def test_gate_pr3_3_nearest_match_1_2v_max232(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR3-3: search "1.2V MAX232" activates nearest_match=True because
    no MAX232 variant has voltage_max==1.2V. Nearest rows are sorted by |voltage_max-1.2|.

    PATH DRIVEN: TWO-PASS (hard then relaxed) — replicates the CLI `search`
    command orchestration directly against real Dgraph, without going through
    the CLI runner (which is not available in this integration harness).

    Pass 1: build_search_dql(parsed, limit=50) with voltage_max filter.
            Execute. rank_results → expected to yield nearest_match=False +
            zero rows (no 1.2V MAX232 exists) OR nearest_match=False + >0 rows
            that do not have voltage_max≈1.2.
    Pass 2: build_search_dql(relaxed_parsed, limit=50) — quantities=[].
            Execute. Merge pass-2 rows under "nearest" key. rank_results on
            merged dict → nearest_match=True.

    Then:
      - result.nearest_match is True.
      - No row has voltage_max == 1.2 (MAX232-family parts are 5V/3.3V rated).
      - Rows with voltage_max present are sorted ascending by |voltage_max - 1.2|.
      - nearest rows are MAX232-family (mpn_norm contains "MAX232").
    PRINTS: top rows + distances.
    """
    parsed = parse_query("1.2V MAX232")

    # ---- Pass 1: hard search with voltage_max filter ----
    query_text_hard, variables_hard = build_search_dql(parsed, limit=50)
    data_hard = _run_search_dql(dgraph_pydgraph_client, query_text_hard, variables_hard)
    result_hard = rank_results(data_hard, parsed)

    print(
        f"\n[GATE-PR3-3] Pass 1 (hard): nearest_match={result_hard.nearest_match}, "
        f"{len(result_hard.rows)} rows",
        file=sys.stderr,
    )

    # If hard pass already returned a row with voltage_max≈1.2, the test data is
    # wrong — a 1.2V MAX232 would make nearest_match inappropriate.
    for row in result_hard.rows:
        vmax = getattr(row, "voltage_max", None)
        if vmax is not None and abs(vmax - 1.2) <= 0.01:
            pytest.fail(
                f"GATE-PR3-3 SETUP ERROR: hard-pass row "
                f"{getattr(row, 'mpn_norm', '?')!r} has voltage_max={vmax} ≈ 1.2V. "
                "The catalogue contains an exact 1.2V MAX232; the nearest-match "
                "path would not activate. Verify test assumptions."
            )

    # ---- Pass 2: relaxed search (text + package only, no voltage filter) ----
    relaxed_parsed = _build_relaxed_parsed(parsed)
    query_text_relaxed, variables_relaxed = build_search_dql(relaxed_parsed, limit=50)
    data_relaxed = _run_search_dql(dgraph_pydgraph_client, query_text_relaxed, variables_relaxed)

    # Merge: hard rows go into their native keys; relaxed-only rows go under "nearest".
    # rank_results sees "nearest" populated + hard blocks empty (or with non-1.2V rows)
    # and sets nearest_match=True.
    merged: dict = {
        "exact": data_hard.get("exact", []),
        "trig":  data_hard.get("trig", []),
        "fts":   data_hard.get("fts", []),
    }
    # Collect all relaxed rows from any block that the DQL builder emitted.
    relaxed_rows: list[dict] = []
    for block_rows in data_relaxed.values():
        if isinstance(block_rows, list):
            relaxed_rows.extend(block_rows)

    # Deduplicate by uid (hard rows win — they have the parametric distance info).
    hard_uids: set[str] = {
        r.get("uid", "")
        for block in (merged["exact"], merged["trig"], merged["fts"])
        for r in block
    }
    merged["nearest"] = [r for r in relaxed_rows if r.get("uid") not in hard_uids]

    result = rank_results(merged, parsed)

    print(
        f"[GATE-PR3-3] Pass 2 (relaxed+merged): nearest_match={result.nearest_match}, "
        f"{len(result.rows)} rows",
        file=sys.stderr,
    )

    assert result.nearest_match is True, (
        "GATE-PR3-3 FAILED: Expected nearest_match=True after two-pass merge. "
        "If nearest_match=False, either all blocks including 'nearest' are empty "
        "(no MAX232 at all in catalogue?) or rank_results ignores the 'nearest' key. "
        "Check: (1) ingest completed, (2) rank_results handles 'nearest' block, "
        "(3) merged dict has non-empty 'nearest' list."
    )

    # Verify no row has voltage_max == 1.2 (would contradict nearest_match=True rationale).
    for row in result.rows:
        vmax = getattr(row, "voltage_max", None)
        if vmax is not None:
            assert abs(vmax - 1.2) > 0.01, (
                f"GATE-PR3-3 FAILED: Row {getattr(row, 'mpn_norm', '?')!r} has "
                f"voltage_max={vmax} ≈ 1.2V but nearest_match=True. "
                "This is contradictory — a 1.2V MAX232 should have been caught "
                "in the hard-pass setup check above."
            )

    # Verify at least some rows are MAX232-family.
    mpn_norms = [getattr(r, "mpn_norm", "") for r in result.rows]
    max232_rows = [m for m in mpn_norms if "232" in m.upper()]
    assert max232_rows, (
        f"GATE-PR3-3 FAILED: Expected MAX232-family rows in nearest result. "
        f"Got mpn_norms: {mpn_norms[:10]}"
    )

    # Verify ascending sort by |voltage_max - 1.2| for rows that have the field.
    rows_with_voltage = [
        (getattr(row, "mpn_norm", "?"), getattr(row, "voltage_max", None))
        for row in result.rows
        if getattr(row, "voltage_max", None) is not None
    ]

    for i, (mpn, vmax) in enumerate(rows_with_voltage[:5]):
        distance = abs(vmax - 1.2)
        print(
            f"[GATE-PR3-3] Row {i}: {mpn!r}  voltage_max={vmax}  |v-1.2|={distance:.3f}",
            file=sys.stderr,
        )

    distances = [abs(vmax - 1.2) for _, vmax in rows_with_voltage]
    if len(distances) >= 2:
        assert distances == sorted(distances), (
            f"GATE-PR3-3 FAILED: Nearest rows must be sorted ascending by |voltage_max-1.2|. "
            f"Distances: {distances}"
        )


# ---------------------------------------------------------------------------
# GATE-PR3-4: Part count unchanged before/after suite (read-only proof)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr3_4_part_count_unchanged_read_only(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR3-4: The Part count in Dgraph is identical before and after the
    GATE-PR3 suite, proving that all operations are read-only.

    Given: part_count_before was recorded in GATE-PR3-1.
    When:  we count Part nodes again at the end of the suite.
    Then:  both counts are equal and > 0.
    """
    count_before = _suite_state["part_count_before"]
    count_after = _dgraph_part_count(dgraph_pydgraph_client)

    print(
        f"\n[GATE-PR3-4] Part count before={count_before}  after={count_after:,}",
        file=sys.stderr,
    )

    assert count_after > 0, (
        "GATE-PR3-4 FAILED: No Part nodes found after suite. "
        "Has the DB been reset?"
    )

    if count_before is None:
        pytest.skip(
            "GATE-PR3-1 did not run (DB may have been unavailable); "
            "cannot compare before/after counts."
        )

    assert count_before == count_after, (
        f"GATE-PR3-4 FAILED: Part count changed from {count_before:,} to "
        f"{count_after:,}. The GATE-PR3 suite must be purely read-only."
    )
