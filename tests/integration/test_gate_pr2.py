"""
Tests: GATE-1, GATE-2, GATE-3 — PR2 acceptance gates.

@pytest.mark.integration — all tests require:
  - A running Dgraph instance (dgraph_available fixture).
  - data/raw/jlcpcb-components.sqlite3 to be present AND ingested.
    Tests are SKIPPED with a clear reason if the file is absent.

GATE-1: Row count in SQLite components table == Dgraph Part count (via
        { q(func: type(Part)) { count(uid) } } — the safe named-block form).
        Both counts are PRINTED.

GATE-2: At least one Part whose mpn_norm matches "MAX232" (substring / eq / trigram
        search on mpn_norm predicate) has a non-empty datasheet URL.
        The matching part's xid and url are PRINTED.

GATE-3: data/state/load_metrics.json must exist (written by the Loader at load
        time) and contain:
          {"parts_loaded": N, "wall_seconds": S, "parts_per_second": R}
        where N == the GATE-1 SQLite count.
        This test documents the loader metrics contract: the Loader MUST write
        this file on every successful load run.

ROOT-LEVEL count(func:) IS BROKEN IN DGRAPH V25.
Always use: { q(func: type(X)) { count(uid) } } -> {"q": [{"count": N}]} or [] -> 0.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SQLITE_PATH = REPO_ROOT / "data" / "raw" / "jlcpcb-components.sqlite3"
LOAD_METRICS_PATH = REPO_ROOT / "data" / "state" / "load_metrics.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sqlite_component_count() -> int:
    """Return the number of rows in the components table."""
    conn = sqlite3.connect(str(SQLITE_PATH))
    try:
        row = conn.execute("SELECT count(*) FROM components").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _dgraph_part_count(client) -> int:
    """Return the number of Part nodes in Dgraph using the safe named-block form.

    Uses { q(func: type(Part)) { count(uid) } } per the canonical pattern
    documented in test_dgraph_lifecycle.py ~L84-104.
    The root-level count(func:) form is NOT used: in Dgraph v25 it returns
    {"count": []} for every cardinality, making it unreliable.
    """
    query = "{ q(func: type(Part)) { count(uid) } }"
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        block = data.get("q", [])
        return block[0]["count"] if block else 0
    finally:
        txn.discard()


def _dgraph_find_max232(client) -> list[dict]:
    """Find Part nodes whose mpn_norm contains 'MAX232' using trigram or eq search."""
    # Use anyofterms on mpn_norm (trigram index) for substring match.
    query = """
    {
      q(func: anyofterms(mpn_norm, "MAX232")) {
        uid
        xid
        mpn
        mpn_norm
        datasheet { url }
      }
    }
    """
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        return data.get("q", [])
    finally:
        txn.discard()


# ---------------------------------------------------------------------------
# GATE-1
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate1_sqlite_count_equals_dgraph_part_count(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-1: The number of rows in SQLite components == the number of Part
    nodes in Dgraph after a full ingest.

    Given: data/raw/jlcpcb-components.sqlite3 exists and has been ingested.
    When:  we count rows in SQLite and Part nodes in Dgraph.
    Then:  both counts are equal and > 0.
           Both values are PRINTED so they appear in --verbose output.

    Uses the safe named-block DQL form: { q(func: type(Part)) { count(uid) } }
    """
    if not SQLITE_PATH.exists():
        pytest.skip(
            f"Source file absent: {SQLITE_PATH}. "
            "Run `partgraph ingest jlcparts --fetch` then re-run integration tests."
        )

    sqlite_count = _sqlite_component_count()
    dgraph_count = _dgraph_part_count(dgraph_pydgraph_client)

    print(
        f"\n[GATE-1] SQLite components: {sqlite_count:,}  |  "
        f"Dgraph Part nodes: {dgraph_count:,}",
        file=sys.stderr,
    )

    assert sqlite_count > 0, (
        f"SQLite components table is empty; has ingest run? Path: {SQLITE_PATH}"
    )
    assert dgraph_count > 0, (
        "No Part nodes in Dgraph; has `partgraph ingest jlcparts` been run?"
    )
    assert sqlite_count == dgraph_count, (
        f"GATE-1 FAILED: SQLite has {sqlite_count:,} rows but Dgraph has "
        f"{dgraph_count:,} Part nodes. Counts must be equal after full ingest."
    )


# ---------------------------------------------------------------------------
# GATE-2
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate2_max232_part_has_datasheet_url(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-2: At least one Part matching 'MAX232' in mpn_norm has a non-empty
    datasheet URL.

    Given: the DB has been ingested.
    When:  we query for Parts with mpn_norm containing 'MAX232'.
    Then:  at least one result exists and has a non-empty datasheet.url.
           Matching xid and url are PRINTED.
    """
    if not SQLITE_PATH.exists():
        pytest.skip(
            f"Source file absent: {SQLITE_PATH}. "
            "Run `partgraph ingest jlcparts --fetch` first."
        )

    results = _dgraph_find_max232(dgraph_pydgraph_client)

    assert results, (
        "GATE-2 FAILED: No Part nodes matching 'MAX232' in mpn_norm found in Dgraph. "
        "The MAX232 IC family is a canonical presence test for JLC data quality."
    )

    parts_with_datasheet = []
    for r in results:
        datasheets = r.get("datasheet", [])
        for ds in datasheets:
            url = ds.get("url", "")
            if url and url.startswith("http"):
                parts_with_datasheet.append((r.get("xid"), url))

    print(
        f"\n[GATE-2] MAX232 matches: {len(results)}  |  "
        f"With datasheet: {len(parts_with_datasheet)}",
        file=sys.stderr,
    )
    if parts_with_datasheet:
        xid, url = parts_with_datasheet[0]
        print(f"[GATE-2] Sample: xid={xid!r}  url={url!r}", file=sys.stderr)

    assert parts_with_datasheet, (
        f"GATE-2 FAILED: Found {len(results)} MAX232 Part(s) but none has a "
        "non-empty datasheet URL. All MAX232 parts must have datasheet links after ingest."
    )


# ---------------------------------------------------------------------------
# GATE-3
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate3_load_metrics_file_exists_and_matches_gate1(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-3: data/state/load_metrics.json exists and its parts_loaded field
    matches the SQLite component count (== GATE-1 count).

    This test documents the Loader metrics contract:

        The Loader MUST write data/state/load_metrics.json after every
        successful load run with the structure:
          {
            "parts_loaded": <int>,
            "wall_seconds": <float>,
            "parts_per_second": <float>
          }

    Given: a full ingest has been run.
    When:  we read data/state/load_metrics.json.
    Then:
      - The file exists.
      - It is valid JSON with keys: parts_loaded, wall_seconds, parts_per_second.
      - parts_loaded == SQLite component count.
      - wall_seconds > 0.
      - parts_per_second > 0.
    """
    if not SQLITE_PATH.exists():
        pytest.skip(
            f"Source file absent: {SQLITE_PATH}. "
            "Run `partgraph ingest jlcparts --fetch` first."
        )

    assert LOAD_METRICS_PATH.exists(), (
        f"GATE-3 FAILED: load_metrics.json not found at {LOAD_METRICS_PATH}. "
        "The Loader must write this file after every successful load.\n"
        "Contract: Loader writes data/state/load_metrics.json with "
        "{'parts_loaded': N, 'wall_seconds': S, 'parts_per_second': R}."
    )

    metrics_text = LOAD_METRICS_PATH.read_text(encoding="utf-8")
    try:
        metrics = json.loads(metrics_text)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"GATE-3 FAILED: {LOAD_METRICS_PATH} is not valid JSON: {exc}\n"
            f"Content: {metrics_text[:200]}"
        )

    # Verify required keys.
    for key in ("parts_loaded", "wall_seconds", "parts_per_second"):
        assert key in metrics, (
            f"GATE-3 FAILED: load_metrics.json missing key '{key}'. "
            f"Keys found: {list(metrics.keys())}"
        )

    parts_loaded = metrics["parts_loaded"]
    wall_seconds = metrics["wall_seconds"]
    parts_per_second = metrics["parts_per_second"]

    print(
        f"\n[GATE-3] load_metrics.json: parts_loaded={parts_loaded:,}  "
        f"wall_seconds={wall_seconds:.1f}s  "
        f"parts_per_second={parts_per_second:.0f}/s",
        file=sys.stderr,
    )

    sqlite_count = _sqlite_component_count()

    assert isinstance(parts_loaded, int) and parts_loaded > 0, (
        f"GATE-3 FAILED: parts_loaded must be a positive int, got {parts_loaded!r}."
    )
    assert isinstance(wall_seconds, (int, float)) and wall_seconds > 0, (
        f"GATE-3 FAILED: wall_seconds must be positive, got {wall_seconds!r}."
    )
    assert isinstance(parts_per_second, (int, float)) and parts_per_second > 0, (
        f"GATE-3 FAILED: parts_per_second must be positive, got {parts_per_second!r}."
    )
    assert parts_loaded == sqlite_count, (
        f"GATE-3 FAILED: parts_loaded ({parts_loaded:,}) != SQLite count "
        f"({sqlite_count:,}). The Loader must record the exact count of parts "
        "it wrote so GATE-1 and GATE-3 can be cross-validated."
    )
