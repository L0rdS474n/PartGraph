"""
Tests: GATE-PR6-1..5 — issue #15 PR2 "search --sort / --json" acceptance
gate (AC-SF-39).

@pytest.mark.integration — all tests require:
  - A running Dgraph instance (dgraph_available fixture).
  - The JLCPCB catalogue to have been ingested.
  - Tests SKIP cleanly when DB is down.

PURE READ-ONLY (mirrors test_gate_pr5.py): this suite invokes `partgraph
search` end-to-end via Typer's CliRunner, IN-PROCESS, against the REAL
Dgraph client (never mocked — cli.py's own `_build_dgraph_client()` connects
to 127.0.0.1:9081), with the NEW --json/--sort flags, and reads results back.
No txn.mutate is ever called; no teardown step is required.

Real queries used below were independently verified read-only against the
live catalogue (127.0.0.1:9081) before writing these assertions:
  - "MAX232" (limit 50, via the CURRENT pre-PR2 `partgraph search` CLI and a
    direct read-only pydgraph DQL query) returns 50 exact/trigram-tier rows.
    At the time this test was written EVERY one of those 50 rows carried a
    non-null price_usd (0 unpriced), with stock spanning at least the
    distinct values [5, 12, 13, 22, 24, 38, 58, 59, 73, 76, ...] — a wide
    enough spread to make a monotone stock-ordering check meaningful. The
    price-ordering assertions below are written as GENERAL structural
    properties (ascending among priced rows; any nulls form one contiguous
    trailing block) so they hold true today (vacuously, for the null-block
    part) AND would catch a future regression if a price-null MAX232 row is
    ever ingested.
  - "10kohm 01005 0.01%" triggers the relaxed (nearest-match) pass on the
    CURRENT (pre-PR2) CLI: confirmed live output starts with
    "No exact match for: 10kohm 01005 0.01%" followed by
    "Nearest matches (by parameter distance):".

ROOT-LEVEL count(func:) IS BROKEN IN DGRAPH V25.
Always use: { q(func: type(Part)) { count(uid) } } -> {"q": [{"count": N}]} or [] -> 0.

NOTE: `partgraph search` does not yet accept --json/--sort (issue #15 PR2 is
not yet implemented), so every test below that passes these flags will fail
with a Click/Typer usage error (exit code 2, "No such option") at the
RUNNER.invoke(...) call site until PR2 is implemented. That is the correct
RED state for this gate — a per-test runtime failure, never a collection
error (partgraph.cli itself imports fine today, and `search`/`--limit`
already exist).
"""

from __future__ import annotations

import json
import re
import sys

import pytest
from typer.testing import CliRunner

from partgraph.cli import app

RUNNER = CliRunner()

_suite_state: dict[str, int | None] = {"part_count_before": None}

_EXPECTED_ROW_KEYS = {
    "mpn", "mpn_norm", "manufacturer", "package", "category", "stock",
    "is_basic", "price_usd", "match_type", "similarity", "datasheets", "params",
}

# ---------------------------------------------------------------------------
# Helpers (read-only, mirrored from test_gate_pr5.py's pure-read-only pattern)
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


def _invoke_json(args: list[str]) -> tuple[int, str, dict | None]:
    """Invoke the CLI (real Dgraph client, no mocks) and best-effort parse
    stdout as JSON.

    Returns (exit_code, raw_output, envelope_or_None). envelope is None when
    stdout is not valid JSON (e.g. a pre-implementation Click usage error) so
    callers can assert cleanly on exit_code/output instead of crashing on a
    JSONDecodeError.
    """
    result = RUNNER.invoke(app, args)
    try:
        envelope = json.loads(result.output)
    except ValueError:
        envelope = None
    return result.exit_code, result.output, envelope


# ---------------------------------------------------------------------------
# GATE-PR6-1: --json envelope validates (keys/types), no "uid" anywhere
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr6_1_json_envelope_validates_keys_types_no_uid(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR6-1: `partgraph search "MAX232" --json --limit 50` end-to-end
    against the live catalogue -> exit 0, stdout is one JSON object with the
    documented envelope keys/types, every result row has the documented
    keys/types, and the literal "uid" / any "0x..." value appears NOWHERE in
    stdout.
    """
    client = dgraph_pydgraph_client
    _suite_state["part_count_before"] = _dgraph_part_count(client)

    exit_code, output, envelope = _invoke_json(
        ["search", "MAX232", "--json", "--limit", "50"]
    )

    print(f"\n[GATE-PR6-1] exit_code={exit_code}", file=sys.stderr)

    assert exit_code == 0, (
        f"GATE-PR6-1 FAILED: `search MAX232 --json` must exit 0. "
        f"Got {exit_code}.\n{output}"
    )
    assert envelope is not None, (
        f"GATE-PR6-1 FAILED: stdout must be valid JSON. Got:\n{output}"
    )

    assert set(envelope) == {"version", "query", "nearest_match", "count", "results"}, (
        f"GATE-PR6-1 FAILED: envelope must have exactly the documented keys. "
        f"Got: {sorted(envelope)}"
    )
    assert envelope["version"] == 1
    assert isinstance(envelope["query"], str)
    assert isinstance(envelope["nearest_match"], bool)
    assert isinstance(envelope["count"], int)
    assert isinstance(envelope["results"], list)
    assert envelope["count"] == len(envelope["results"])

    assert envelope["results"], (
        "GATE-PR6-1 FAILED: expected non-empty results for 'MAX232' "
        "(LIVE-CONFIRMED non-empty before this test was written)."
    )

    for row in envelope["results"]:
        assert set(row) == _EXPECTED_ROW_KEYS, (
            f"GATE-PR6-1 FAILED: row keys must be exactly "
            f"{sorted(_EXPECTED_ROW_KEYS)}. Got: {sorted(row)} for row={row}"
        )
        assert isinstance(row["mpn_norm"], str) and row["mpn_norm"], (
            f"GATE-PR6-1 FAILED: mpn_norm must be a non-empty string. Got: {row!r}"
        )
        assert isinstance(row["datasheets"], list)
        assert isinstance(row["params"], dict)
        # ADR-0020: `similarity` is a cosine float on semantic-tier rows and
        # null otherwise; this lexical "MAX232" search must yield None.
        assert row["similarity"] is None or isinstance(row["similarity"], float), (
            f"GATE-PR6-1 FAILED: similarity must be a float (semantic rows) "
            f"or null (lexical rows). Got: {row['similarity']!r}"
        )
        assert row["match_type"] in {"exact", "trigram", "fulltext", "semantic", "nearest"}, (
            f"GATE-PR6-1 FAILED: match_type must be a machine-safe tier "
            f"name. Got: {row['match_type']!r}"
        )

    assert "uid" not in output, (
        f"GATE-PR6-1 FAILED: the string 'uid' must never appear in --json "
        f"stdout. Got:\n{output}"
    )
    assert not re.search(r"0x[0-9a-fA-F]+", output), (
        f"GATE-PR6-1 FAILED: no Dgraph uid-shaped '0x...' value may appear "
        f"in --json stdout. Got:\n{output}"
    )


# ---------------------------------------------------------------------------
# GATE-PR6-2: --sort stock -> monotone non-increasing stock
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr6_2_sort_stock_monotone_non_increasing(dgraph_available) -> None:
    """GATE-PR6-2: `partgraph search "MAX232" --json --sort stock --limit 50`
    end-to-end -> the stock sequence (None treated as 0) is monotone
    NON-INCREASING (each row's stock >= the next row's stock).
    """
    exit_code, output, envelope = _invoke_json(
        ["search", "MAX232", "--json", "--sort", "stock", "--limit", "50"]
    )

    assert exit_code == 0, (
        f"GATE-PR6-2 FAILED: `--json --sort stock` must exit 0. "
        f"Got {exit_code}.\n{output}"
    )
    assert envelope is not None and envelope["results"], (
        f"GATE-PR6-2 FAILED: expected a non-empty JSON envelope. Got:\n{output}"
    )

    stocks = [
        row["stock"] if row["stock"] is not None else 0
        for row in envelope["results"]
    ]
    print(f"\n[GATE-PR6-2] stock sequence: {stocks}", file=sys.stderr)

    violations = [
        (i, stocks[i], stocks[i + 1])
        for i in range(len(stocks) - 1)
        if stocks[i] < stocks[i + 1]
    ]
    assert not violations, (
        f"GATE-PR6-2 FAILED: --sort stock must be monotone non-increasing. "
        f"Violations (index, stock[i], stock[i+1]): {violations}. "
        f"Full sequence: {stocks}"
    )
    assert len(set(stocks)) > 1, (
        "GATE-PR6-2 FAILED (test-fixture sanity): expected more than one "
        "distinct stock value in the 'MAX232' result set (LIVE-CONFIRMED "
        "wide stock spread before this test was written) — a single-value "
        "sequence would make the monotone check vacuous."
    )


# ---------------------------------------------------------------------------
# GATE-PR6-3: --sort price -> ascending priced rows, price-null rows LAST
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr6_3_sort_price_ascending_priced_precede_null(dgraph_available) -> None:
    """GATE-PR6-3: `partgraph search "MAX232" --json --sort price --limit 50`
    end-to-end -> among rows with a non-null price_usd, the values are
    ascending; and once a null price_usd is seen, EVERY subsequent row is
    also null (nulls form one contiguous trailing block — "all priced rows
    precede all price-null rows"). LIVE-CONFIRMED: at time of writing every
    'MAX232' row is price_usd-populated (zero nulls), so this also verifies
    the ascending-order half of the contract directly; the null-trailing
    check holds vacuously today and guards against a future regression if a
    price-null MAX232 row is ever ingested.
    """
    exit_code, output, envelope = _invoke_json(
        ["search", "MAX232", "--json", "--sort", "price", "--limit", "50"]
    )

    assert exit_code == 0, (
        f"GATE-PR6-3 FAILED: `--json --sort price` must exit 0. "
        f"Got {exit_code}.\n{output}"
    )
    assert envelope is not None and envelope["results"], (
        f"GATE-PR6-3 FAILED: expected a non-empty JSON envelope. Got:\n{output}"
    )

    prices = [row["price_usd"] for row in envelope["results"]]
    print(f"\n[GATE-PR6-3] price_usd sequence (first 10): {prices[:10]}", file=sys.stderr)

    # Nulls (if any) must form a single trailing block: once None appears,
    # every remaining entry must also be None.
    seen_null = False
    for idx, price in enumerate(prices):
        if price is None:
            seen_null = True
            continue
        assert not seen_null, (
            f"GATE-PR6-3 FAILED: priced row at index {idx} (price_usd={price}) "
            f"appears AFTER a price-null row — all priced rows must precede "
            f"all price-null rows. Full sequence: {prices}"
        )

    priced = [p for p in prices if p is not None]
    violations = [
        (i, priced[i], priced[i + 1])
        for i in range(len(priced) - 1)
        if priced[i] > priced[i + 1]
    ]
    assert not violations, (
        f"GATE-PR6-3 FAILED: priced rows must be ascending by price_usd. "
        f"Violations (index, price[i], price[i+1]): {violations}. "
        f"Full priced sequence: {priced}"
    )
    assert len(priced) > 1, (
        "GATE-PR6-3 FAILED (test-fixture sanity): expected more than one "
        "priced row for 'MAX232' (LIVE-CONFIRMED all 50 rows priced before "
        "this test was written)."
    )


# ---------------------------------------------------------------------------
# GATE-PR6-4: nearest-match mode -> --sort is a no-op
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr6_4_nearest_mode_sort_is_a_no_op(dgraph_available) -> None:
    """GATE-PR6-4: `partgraph search "10kohm 01005 0.01%" --json` (nearest
    match — LIVE-CONFIRMED: this query triggers "No exact match for:" /
    "Nearest matches (by parameter distance):" on the current, pre-PR2 CLI)
    end-to-end, invoked with no --sort, --sort stock, and --sort price.
    -> nearest_match is True in every envelope, and the mpn_norm row order is
    IDENTICAL across all three invocations (sort is a no-op in nearest mode;
    the parameter-distance order always wins).
    """
    query = "10kohm 01005 0.01%"
    orders: dict[str, list[str]] = {}
    for label, extra_args in (
        ("no-sort", []),
        ("sort-stock", ["--sort", "stock"]),
        ("sort-price", ["--sort", "price"]),
    ):
        exit_code, output, envelope = _invoke_json(["search", query, "--json", *extra_args])
        assert exit_code == 0, (
            f"GATE-PR6-4 FAILED: `--json {' '.join(extra_args)}` must exit 0. "
            f"Got {exit_code}.\n{output}"
        )
        assert envelope is not None, (
            f"GATE-PR6-4 FAILED: expected valid JSON for {label!r}. Got:\n{output}"
        )
        assert envelope["nearest_match"] is True, (
            f"GATE-PR6-4 FAILED: expected nearest_match=True for {label!r} "
            f"(LIVE-CONFIRMED nearest-match trigger before this test was "
            f"written). Got: {envelope['nearest_match']!r}"
        )
        orders[label] = [row["mpn_norm"] for row in envelope["results"]]

    print(f"\n[GATE-PR6-4] orders: {orders}", file=sys.stderr)

    assert orders["no-sort"], (
        "GATE-PR6-4 FAILED: expected a non-empty nearest-match result set."
    )
    assert orders["sort-stock"] == orders["no-sort"], (
        f"GATE-PR6-4 FAILED: --sort stock must be a no-op in nearest-match "
        f"mode. no-sort={orders['no-sort']} sort-stock={orders['sort-stock']}"
    )
    assert orders["sort-price"] == orders["no-sort"], (
        f"GATE-PR6-4 FAILED: --sort price must be a no-op in nearest-match "
        f"mode. no-sort={orders['no-sort']} sort-price={orders['sort-price']}"
    )


# ---------------------------------------------------------------------------
# GATE-PR6-5: Part count unchanged before/after (pure read-only proof)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gate_pr6_5_part_count_unchanged_read_only(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR6-5: The Part count in Dgraph is identical before and after the
    GATE-PR6 suite, proving this suite is PURE READ-ONLY — no mutate, no
    teardown (mirrors GATE-PR5-5).
    """
    count_before = _suite_state["part_count_before"]
    count_after = _dgraph_part_count(dgraph_pydgraph_client)

    print(
        f"\n[GATE-PR6-5] Part count before={count_before} after={count_after:,}",
        file=sys.stderr,
    )

    assert count_after > 0, (
        "GATE-PR6-5 FAILED: no Part nodes found after suite. Has the DB been reset?"
    )

    if count_before is None:
        pytest.skip(
            "GATE-PR6-1 did not run (DB unavailable); cannot compare before/after counts."
        )

    assert count_before == count_after, (
        f"GATE-PR6-5 FAILED: Part count changed from {count_before:,} to "
        f"{count_after:,}. GATE-PR6 must be purely read-only."
    )
