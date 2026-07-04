"""
Tests: GATE-PR5-1..5 — issue #15 PR1 "structured search filters" acceptance
gates (AC-SF-29 / AC-SF-30 / AC-SF-31).

@pytest.mark.integration — all tests require:
  - A running Dgraph instance (dgraph_available fixture).
  - The JLCPCB catalogue to have been ingested (PR2 ingest complete).
  - Tests SKIP cleanly when DB is down.

PURE READ-ONLY (unlike test_gate_pr4.py, which embeds+writes+deletes): this
suite only calls build_search_dql with the new manufacturer/package/category
keyword arguments against the REAL catalogue and reads results back. No
txn.mutate is ever called; no teardown step is required.

Real values used below were independently verified read-only against the live
catalogue (127.0.0.1:9081) before writing these assertions:
  - Category "RS232 ICs" is the real category name attached to MAX232-family
    parts (including Texas-Instruments-made ones, e.g. MAX232ECDR, MAX232DR).
  - Package "SOIC-16" (17 rows) and "PDIP-16" (4 rows) are both non-empty for
    Texas-Instruments-made MAX232-family parts.
  - Package "0000" is syntactically valid (matches the package charset) but
    does not exist for any Texas-Instruments MAX232-family part -> empty
    result, confirmed live.
  - allofterms(name, $var) with a lowercase-cased bound value
    ("texas instruments") matches all three differently-cased manufacturer
    nodes present in the catalogue ("Texas Instruments" / "TEXAS INSTRUMENTS"
    / "texas instruments") -> confirms case-insensitive recall (AC-SF-2) live.

Assertions are STRUCTURAL/subset properties ONLY (never exact row counts —
the catalogue can drift between ingest runs), per AC-SF-29/30/31:
  - every returned row's manufacturer/package/category allofterms/eq-matches
    the filter value applied;
  - a combo (manufacturer+package) result is a non-empty SUBSET (by uid) of
    the manufacturer-only result for the same base query (adding an AND-ed
    filter can only shrink the result set — a property independent of the
    catalogue's exact contents);
  - a contradictory combo (real manufacturer + a package that does not exist
    for it) returns an EMPTY result WITHOUT raising.

ROOT-LEVEL count(func:) IS BROKEN IN DGRAPH V25.
Always use: { q(func: type(Part)) { count(uid) } } -> {"q": [{"count": N}]} or [] -> 0.

NOTE: build_search_dql does not yet accept manufacturer=/package=/category=
keyword arguments (issue #15 PR1 is not yet implemented), so every test below
will fail with a TypeError at the build_search_dql(...) call site until PR1 is
implemented. That is the correct RED state for this gate.
"""

from __future__ import annotations

import json
import sys

import pytest

from partgraph.query.dql_builder import build_search_dql  # noqa: F401
from partgraph.query.parser import parse_query  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers (read-only, mirrored from test_gate_pr3.py's pure-read-only pattern)
# ---------------------------------------------------------------------------

_suite_state: dict[str, int | None] = {"part_count_before": None}


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
    """Execute a DQL query (read-only) and return the parsed JSON response."""
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query_text, variables=variables)
        return json.loads(resp.json)
    finally:
        txn.discard()


def _all_rows(data: dict) -> list[dict]:
    """Flatten every named block's rows (exact/trig/fts/...) into one list."""
    rows: list[dict] = []
    for block in data.values():
        if isinstance(block, list):
            rows.extend(r for r in block if isinstance(r, dict))
    return rows


# ---------------------------------------------------------------------------
# GATE-PR5-1: manufacturer filter — allofterms case-insensitive recall
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr5_1_manufacturer_filter_allofterms_matches_both_ti_tokens(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR5-1: search "MAX232" + manufacturer="texas instruments"
    (lowercase) -> non-empty, and EVERY returned row's manufacturer name
    contains both "texas" and "instruments" (case-insensitive), proving the
    allofterms filter is real (not a no-op) and case-insensitive.
    """
    client = dgraph_pydgraph_client
    _suite_state["part_count_before"] = _dgraph_part_count(client)

    parsed = parse_query("MAX232")
    query_text, variables = build_search_dql(
        parsed, manufacturer="texas instruments", limit=50
    )
    data = _run_search_dql(client, query_text, variables)
    rows = _all_rows(data)

    print(
        f"\n[GATE-PR5-1] MAX232 + manufacturer='texas instruments': "
        f"{len(rows)} rows",
        file=sys.stderr,
    )

    assert rows, (
        "GATE-PR5-1 FAILED: expected non-empty results for MAX232 + "
        "manufacturer 'texas instruments'. Verify ingest completed and "
        "Texas-Instruments-made MAX232 parts exist."
    )

    manufacturers_seen = sorted(
        {
            (row.get("made_by") or [{}])[0].get("name")
            for row in rows
            if row.get("made_by")
        }
    )
    print(f"[GATE-PR5-1] Manufacturers seen: {manufacturers_seen}", file=sys.stderr)

    bad_rows = []
    for row in rows:
        made_by = row.get("made_by") or []
        name = (made_by[0].get("name") if made_by else "") or ""
        lname = name.lower()
        if "texas" not in lname or "instruments" not in lname:
            bad_rows.append((row.get("mpn_norm"), name))

    assert not bad_rows, (
        f"GATE-PR5-1 FAILED: rows whose manufacturer does not allofterms-match "
        f"'texas instruments' (both tokens, case-insensitive): {bad_rows}"
    )


# ---------------------------------------------------------------------------
# GATE-PR5-2: manufacturer + package combo -> non-empty SUBSET of mfr-only
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr5_2_manufacturer_and_package_combo_nonempty_subset(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR5-2: search "MAX232" + manufacturer="Texas Instruments" +
    package="SOIC-16" (and, separately, "PDIP-16") -> non-empty; every row's
    package name contains the given package; and the combo result's uids are
    a SUBSET of the manufacturer-only (no package) result's uids for the same
    base query (an AND-ed filter can only shrink the result set — a
    structural property independent of exact catalogue counts).
    """
    client = dgraph_pydgraph_client
    parsed = parse_query("MAX232")

    query_mfr_only, vars_mfr_only = build_search_dql(
        parsed, manufacturer="Texas Instruments", limit=50
    )
    data_mfr_only = _run_search_dql(client, query_mfr_only, vars_mfr_only)
    uids_mfr_only = {r.get("uid") for r in _all_rows(data_mfr_only)}

    assert uids_mfr_only, (
        "GATE-PR5-2 FAILED: manufacturer-only baseline ('MAX232' + Texas "
        "Instruments) returned no rows; cannot test the subset property."
    )

    for package in ("SOIC-16", "PDIP-16"):
        query_combo, vars_combo = build_search_dql(
            parsed, manufacturer="Texas Instruments", package=package, limit=50
        )
        data_combo = _run_search_dql(client, query_combo, vars_combo)
        rows_combo = _all_rows(data_combo)

        print(
            f"\n[GATE-PR5-2] MAX232 + Texas Instruments + package={package!r}: "
            f"{len(rows_combo)} rows",
            file=sys.stderr,
        )

        assert rows_combo, (
            f"GATE-PR5-2 FAILED: expected non-empty results for MAX232 + "
            f"Texas Instruments + package={package!r} (LIVE-CONFIRMED "
            f"non-empty before this test was written)."
        )

        for row in rows_combo:
            pkg_names = [p.get("name") for p in (row.get("in_package") or [])]
            assert any(package in (p or "") for p in pkg_names), (
                f"GATE-PR5-2 FAILED: row {row.get('mpn_norm')!r} package(s) "
                f"{pkg_names} do not contain {package!r}."
            )

        uids_combo = {r.get("uid") for r in rows_combo}
        assert uids_combo <= uids_mfr_only, (
            f"GATE-PR5-2 FAILED: combo (Texas Instruments + package={package!r}) "
            f"uids must be a subset of the manufacturer-only uids. Extra uids "
            f"not present in the manufacturer-only set: "
            f"{uids_combo - uids_mfr_only}"
        )


# ---------------------------------------------------------------------------
# GATE-PR5-3: contradictory manufacturer+package combo -> empty, no error
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr5_3_contradictory_manufacturer_package_combo_returns_empty_without_error(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR5-3: search "MAX232" + manufacturer="Texas Instruments" +
    package="0000" (syntactically valid per the package charset regex, but no
    such package exists in the catalogue for any Texas-Instruments MAX232-
    family part — LIVE-CONFIRMED) -> returns an EMPTY result, no exception.
    """
    client = dgraph_pydgraph_client
    parsed = parse_query("MAX232")

    query_text, variables = build_search_dql(
        parsed, manufacturer="Texas Instruments", package="0000", limit=50
    )
    data = _run_search_dql(client, query_text, variables)
    rows = _all_rows(data)

    print(
        f"\n[GATE-PR5-3] MAX232 + Texas Instruments + package='0000' "
        f"(contradictory): {len(rows)} rows",
        file=sys.stderr,
    )

    assert rows == [], (
        f"GATE-PR5-3 FAILED: a contradictory manufacturer+package combo must "
        f"return an EMPTY result, not {len(rows)} rows: {rows[:3]}"
    )


# ---------------------------------------------------------------------------
# GATE-PR5-4: category filter — structural allofterms match
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr5_4_category_filter_structural_rs232_ics(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR5-4: search "MAX232" + category="RS232 ICs" -> non-empty, and
    every returned row's category name contains both the "rs232" and "ic"
    tokens (case-insensitive) — proving the in_category allofterms filter is
    real and correctly scoped (LIVE-CONFIRMED: "RS232 ICs" is the real
    category attached to MAX232-family parts).
    """
    client = dgraph_pydgraph_client
    parsed = parse_query("MAX232")

    query_text, variables = build_search_dql(parsed, category="RS232 ICs", limit=50)
    data = _run_search_dql(client, query_text, variables)
    rows = _all_rows(data)

    print(
        f"\n[GATE-PR5-4] MAX232 + category='RS232 ICs': {len(rows)} rows",
        file=sys.stderr,
    )

    assert rows, (
        "GATE-PR5-4 FAILED: expected non-empty results for MAX232 + category "
        "'RS232 ICs'. Verify ingest completed and MAX232 parts are "
        "categorised (LIVE-CONFIRMED non-empty before this test was written)."
    )

    bad_rows = []
    for row in rows:
        categories = [c.get("name") for c in (row.get("in_category") or [])]
        matches = any(
            "rs232" in (c or "").lower() and "ic" in (c or "").lower()
            for c in categories
        )
        if not matches:
            bad_rows.append((row.get("mpn_norm"), categories))

    assert not bad_rows, (
        f"GATE-PR5-4 FAILED: rows whose category does not match 'RS232 ICs': "
        f"{bad_rows}"
    )


# ---------------------------------------------------------------------------
# GATE-PR5-5: Part count unchanged before/after suite (pure read-only proof)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr5_5_part_count_unchanged_read_only(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR5-5: The Part count in Dgraph is identical before and after the
    GATE-PR5 suite, proving this suite is PURE READ-ONLY — no mutate, no
    teardown (unlike test_gate_pr4.py's embed-write-then-delete cycle).
    """
    count_before = _suite_state["part_count_before"]
    count_after = _dgraph_part_count(dgraph_pydgraph_client)

    print(
        f"\n[GATE-PR5-5] Part count before={count_before} after={count_after:,}",
        file=sys.stderr,
    )

    assert count_after > 0, (
        "GATE-PR5-5 FAILED: no Part nodes found after suite. Has the DB been reset?"
    )

    if count_before is None:
        pytest.skip(
            "GATE-PR5-1 did not run (DB unavailable); cannot compare before/after counts."
        )

    assert count_before == count_after, (
        f"GATE-PR5-5 FAILED: Part count changed from {count_before:,} to "
        f"{count_after:,}. GATE-PR5 must be purely read-only."
    )
