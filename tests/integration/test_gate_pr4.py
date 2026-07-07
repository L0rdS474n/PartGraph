"""
Tests: GATE-PR4-1..3 — PR4 Semantic search acceptance gates (READ-ONLY rework;
ADR-0019, index-integrity PR C, regression vector 2).

@pytest.mark.integration — all tests require:
  - A running Dgraph instance (dgraph_available fixture).
  - The JLCPCB catalogue to have been ingested AND already embedded (PR2
    ingest + a prior `partgraph embed` run).
  - sentence_transformers installed (pytest.importorskip("sentence_transformers")).
  - Tests SKIP cleanly when DB is down, sentence_transformers is absent, or
    (GATE-PR4-1 only) no MAX232-family part has been embedded yet.

STRICTLY READ-ONLY CONTRACT (this is the fix for regression vector 2 — the
original version of this file embedded up to 2000 parts through the real
model, wrote their `embedding` predicate by uid, and then removed it again via
a raw graph-deletion teardown call, run as a plain "acceptance gate" against
what may be a live/shared Dgraph instance. That write-then-remove cycle could
race concurrent readers and, per the project's own `embedding` predicate
teardown notes (see tests/conftest.py's `cleanup_marker_nodes`), a
remove-then-reinsert cycle on an indexed `float32vector` predicate risks
leaving a stale vector lingering in the hnsw index):
  - GATE-PR4-1 no longer embeds anything. It queries for MAX232-family parts
    (`mpn_norm` contains "232") that are ALREADY embedded (a precondition on
    prior `partgraph embed` runs, not something this test performs), embeds
    ONLY the query text "rs232 transceiver", and asserts a MAX232-family row
    is in the semantic search TOP-10 against Dgraph's EXISTING production
    vectors.
  - There is no embedding-write call, no predicate-deletion helper (removed
    entirely), and no graph-mutation construct of any kind anywhere in this
    file. Every transaction is `client.txn(read_only=True)`, always
    `.discard()`-ed — never committed.
  - GATE-PR4-3 is an "edge-aware" bookend: a module-scoped, autouse fixture
    captures BOTH the `type(Part)` count AND the `has(embedding)` count once,
    UNCONDITIONALLY, before any test body runs (independent of whether
    GATE-PR4-1 itself executes its assertion body or `pytest.skip()`s on its
    own precondition) — GATE-PR4-3 then asserts both counts are unchanged and
    strictly positive at the end.

GATE-PR4-2: get_system_reader() real snapshot: cpu_count >= 1, fractions in
            [0,1] (or None), regulate returns bounded directive. UNCHANGED by
            this rework (it never touched Dgraph or the embedding predicate).

Part/embedding count bookend: uses the same
{ q(func: type(Part)) { count(uid) } } / { q(func: has(embedding)) { count(uid) } }
named-block aggregation form as GATE-PR3 / GATE-PR4's original bookend (safe
in Dgraph v25, never root-level count(func:...)).
"""

from __future__ import annotations

import json
import sys

import pytest

# Skip the entire module if sentence_transformers is not installed.
sentence_transformers = pytest.importorskip(
    "sentence_transformers",
    reason=(
        "sentence_transformers not installed; skipping GATE-PR4 tests. "
        'Install with: pip install -e ".[embed]"'
    ),
)

from partgraph.query.dql_builder import build_semantic_dql  # noqa: E402
from partgraph.util.resources import ResourceController, SystemSnapshot, get_system_reader  # noqa: E402


# ---------------------------------------------------------------------------
# Suite-level state (part/embedding count bookend, mirrors the GATE-PR3
# pattern; extended with an embedding_count_before slot — ADR-0019).
# ---------------------------------------------------------------------------

_suite_state: dict[str, int | None] = {
    "part_count_before": None,
    "embedding_count_before": None,
}

# The embed dimension required by all PR4 components.
_EMBED_DIM = 384

# MAX232-family regexp match on mpn_norm, reused from the original gate.
_MAX232_REGEXP = "/232/"


# ---------------------------------------------------------------------------
# Helpers (ALL read-only, mirrors test_gate_pr3.py pattern — no graph-mutation
# call of any kind anywhere in this file)
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


def _dgraph_embedding_count(client) -> int:
    """Return the number of parts carrying an `embedding` (has(embedding))."""
    query = "{ q(func: has(embedding)) { count(uid) } }"
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        block = data.get("q", [])
        return block[0]["count"] if block else 0
    finally:
        txn.discard()


def _select_embedded_max232_parts(client, limit: int = 50) -> list[dict]:
    """Return up to *limit* MAX232-family parts that ALREADY carry an embedding.

    Read-only precondition query for GATE-PR4-1 (ADR-0019): roots on the same
    `regexp(mpn_norm, $rx)` MAX232-family match the original gate used, now
    narrowed with `@filter(has(embedding))` so ONLY already-embedded parts are
    returned — this test never embeds anything itself. A single
    `txn(read_only=True)`, always discarded.
    """
    query = (
        'query search($rx: string) { '
        'q(func: regexp(mpn_norm, $rx)) @filter(has(embedding)) { '
        'uid mpn_norm '
        '} }'
    )
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query, variables={"$rx": _MAX232_REGEXP})
        data = json.loads(resp.json)
        return data.get("q", [])[:limit]
    finally:
        txn.discard()


def _encode_text(model, text: str) -> list[float]:
    """Encode a single text string using the real sentence_transformers model."""
    result = model.encode([text])
    # result is a numpy array of shape (1, dim); convert to list[float].
    return result[0].tolist()


# ---------------------------------------------------------------------------
# Module-start baseline (edge-aware bookend, ADR-0019). autouse + module-
# scoped: runs exactly once, BEFORE the first test in this module, regardless
# of which test triggers fixture setup first, and regardless of whether
# GATE-PR4-1 later pytest.skip()s on its own precondition — so GATE-PR4-3's
# comparison is never starved of a baseline.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _gate_pr4_baseline(dgraph_available, dgraph_pydgraph_client) -> None:
    """Capture the Part count and has(embedding) count ONCE, unconditionally,
    before any GATE-PR4 test body runs.

    Given: Dgraph is reachable.
    When: this module is collected and set up.
    Then: `_suite_state["part_count_before"]` and
        `_suite_state["embedding_count_before"]` are populated exactly once.
    """
    client = dgraph_pydgraph_client
    _suite_state["part_count_before"] = _dgraph_part_count(client)
    _suite_state["embedding_count_before"] = _dgraph_embedding_count(client)


# ===========================================================================
# GATE-PR4-1: read-only semantic search against EXISTING production vectors
# ===========================================================================

@pytest.mark.integration
def test_gate_pr4_1_semantic_search_finds_already_embedded_max232(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR4-1 (READ-ONLY, ADR-0019): semantic search for "rs232
    transceiver" must surface an ALREADY-embedded MAX232-family part in the
    TOP-10. This gate no longer embeds anything itself (the fix for
    regression vector 2 — see the module docstring for why the original
    embed-then-delete version was unsafe against a live/shared instance).

    Given: Dgraph contains the ingested JLCPCB catalogue AND at least one
        MAX232-family part (mpn_norm contains "232") has ALREADY been
        embedded by a prior `partgraph embed` run.
    When:
      1. Query (read-only) for MAX232-family parts that already carry
         `embedding`. If none exist, SKIP with an actionable message rather
         than embedding anything ourselves.
      2. Encode ONLY the query text "rs232 transceiver" (never any part's
         text) with the real sentence-transformers model.
      3. Build the semantic DQL (`build_semantic_dql`, k=10) and execute it
         READ-ONLY against Dgraph's EXISTING production vectors.
    Then:
      - A MAX232-family row (mpn_norm contains "232") appears in the TOP-10.
      - The transaction used is `read_only=True` and `.discard()`-ed; there is
        no mutation, no embedding write, and no teardown step in this test.
    """
    client = dgraph_pydgraph_client

    # --- Precondition (read-only): at least one ALREADY-embedded MAX232 part ---
    max232_embedded = _select_embedded_max232_parts(client)
    if not max232_embedded:
        pytest.skip("no embedded MAX232-family parts; run `partgraph embed` first")

    print(
        f"\n[GATE-PR4-1] Found {len(max232_embedded)} already-embedded "
        "MAX232-family part(s) (read-only precondition).",
        file=sys.stderr,
    )

    # --- Load the real model and encode ONLY the query text ---
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    model = SentenceTransformer("all-MiniLM-L6-v2")

    # Verify model output dimension matches our 384 contract.
    test_vec = model.encode(["test"])
    assert test_vec.shape[1] == _EMBED_DIM, (
        f"GATE-PR4-1: model must produce {_EMBED_DIM}-dim vectors; "
        f"got {test_vec.shape[1]}. Choose a 384-dim model."
    )

    query_text_embed = "rs232 transceiver"
    query_vector = _encode_text(model, query_text_embed)
    assert len(query_vector) == _EMBED_DIM, (
        f"GATE-PR4-1: query vector must be {_EMBED_DIM}-dim; got {len(query_vector)}"
    )

    # --- Semantic search against Dgraph's EXISTING production vectors ---
    # build_semantic_dql uses an inline literal; "rs232" must NOT appear in
    # the query text (the vector is inline; no text injection).
    dql, variables = build_semantic_dql(query_vector, k=10)

    assert "rs232" not in dql.lower(), (
        f"GATE-PR4-1: query text must not contain the literal 'rs232' "
        f"(vector is inline; no text injection). Got query:\n{dql}"
    )

    txn = client.txn(read_only=True)
    try:
        resp = txn.query(dql, variables=variables if variables else None)
        data = json.loads(resp.json)
    finally:
        txn.discard()

    # Extract semantic block results.
    semantic_rows = data.get("semantic", data.get("similar", []))

    print(
        f"[GATE-PR4-1] Semantic search 'rs232 transceiver' returned "
        f"{len(semantic_rows)} rows.",
        file=sys.stderr,
    )
    for i, row in enumerate(semantic_rows[:10]):
        print(
            f"[GATE-PR4-1] Top-{i+1}: {row.get('mpn_norm', '?')!r}",
            file=sys.stderr,
        )

    # Assert a MAX232-family row (mpn_norm contains "232") is in TOP-10.
    top10_mpn_norms = [row.get("mpn_norm", "") for row in semantic_rows[:10]]
    max232_hits = [m for m in top10_mpn_norms if "232" in (m or "").upper()]

    assert max232_hits, (
        f"GATE-PR4-1 FAILED: No MAX232-family row (mpn_norm contains '232') "
        f"found in TOP-10 semantic results for 'rs232 transceiver'. "
        f"Top-10 mpn_norms: {top10_mpn_norms}. "
        "Verify: (1) `partgraph embed` has embedded MAX232-family parts, "
        "(2) the model produces useful embeddings, "
        "(3) the Dgraph vector index is active."
    )


# ===========================================================================
# GATE-PR4-2: real SystemSnapshot from get_system_reader (UNCHANGED)
# ===========================================================================

@pytest.mark.integration
def test_gate_pr4_2_get_system_reader_real_snapshot_bounded(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR4-2: get_system_reader() returns a live reader; its snapshot has
    cpu_count >= 1, fractions in [0, 1] (or None), and regulate returns a bounded
    directive.

    Given: the test machine has at least 1 CPU.
    When: get_system_reader() is called and the reader is invoked.
    Then:
    - cpu_count >= 1.
    - load_avg_1m >= 0.0 (or None if unavailable).
    - ram_available_fraction in [0.0, 1.0] or None (psutil unavailable).
    - regulate(32, snapshot) returns next_batch_size in [1, 256] and
      pause_seconds in [0, 30].
    """
    reader = get_system_reader()
    assert callable(reader), "GATE-PR4-2: get_system_reader must return a callable."

    snapshot = reader()
    assert isinstance(snapshot, SystemSnapshot), (
        f"GATE-PR4-2: reader must return SystemSnapshot; got {type(snapshot)!r}"
    )

    print(
        f"\n[GATE-PR4-2] SystemSnapshot: cpu_count={snapshot.cpu_count}, "
        f"load_avg_1m={snapshot.load_avg_1m:.3f}, "
        f"ram_available_fraction={snapshot.ram_available_fraction}",
        file=sys.stderr,
    )

    assert snapshot.cpu_count >= 1, (
        f"GATE-PR4-2: cpu_count must be >= 1; got {snapshot.cpu_count}"
    )
    assert snapshot.load_avg_1m >= 0.0, (
        f"GATE-PR4-2: load_avg_1m must be >= 0; got {snapshot.load_avg_1m}"
    )
    if snapshot.ram_available_fraction is not None:
        assert 0.0 <= snapshot.ram_available_fraction <= 1.0, (
            f"GATE-PR4-2: ram_available_fraction must be in [0,1]; "
            f"got {snapshot.ram_available_fraction}"
        )

    # Verify regulate produces bounded output.
    controller = ResourceController(min_batch=1, max_batch=256, max_pause=30.0)
    directive = controller.regulate(32, snapshot)

    print(
        f"[GATE-PR4-2] regulate(32, snapshot) -> "
        f"next_batch_size={directive.next_batch_size}, "
        f"pause_seconds={directive.pause_seconds}",
        file=sys.stderr,
    )

    assert 1 <= directive.next_batch_size <= 256, (
        f"GATE-PR4-2: next_batch_size must be in [1, 256]; "
        f"got {directive.next_batch_size}"
    )
    assert 0.0 <= directive.pause_seconds <= 30.0, (
        f"GATE-PR4-2: pause_seconds must be in [0, 30]; "
        f"got {directive.pause_seconds}"
    )


# ===========================================================================
# GATE-PR4-3: edge-aware bookend — Part count AND embedding count unchanged
# ===========================================================================

@pytest.mark.integration
def test_gate_pr4_3_part_and_embedding_counts_unchanged_after_suite(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR4-3 (edge-aware bookend, ADR-0019): both the Part count and the
    has(embedding) count are identical before and after the GATE-PR4 suite —
    proving this READ-ONLY suite never wrote, deleted, or otherwise mutated
    ANY Part node or ANY embedding, regardless of whether GATE-PR4-1 itself
    ran its assertion body or SKIPPED on its own precondition.

    Given: `_gate_pr4_baseline` captured both counts, UNCONDITIONALLY, at
        module start (before GATE-PR4-1/2 ran).
    When: we re-count both after the suite.
    Then: both counts are identical AND strictly greater than zero (an empty
        DB, or a catalogue with zero embedded parts, would make this bookend
        meaningless rather than a genuine no-op proof).
    """
    count_before = _suite_state["part_count_before"]
    embedding_before = _suite_state["embedding_count_before"]
    assert count_before is not None and embedding_before is not None, (
        "GATE-PR4-3: baseline was not captured. The _gate_pr4_baseline "
        "fixture (autouse, module-scoped) must run before any test in this "
        "module."
    )

    count_after = _dgraph_part_count(dgraph_pydgraph_client)
    embedding_after = _dgraph_embedding_count(dgraph_pydgraph_client)

    print(
        f"\n[GATE-PR4-3] Part count before={count_before:,} after={count_after:,}; "
        f"embedding count before={embedding_before:,} after={embedding_after:,}",
        file=sys.stderr,
    )

    assert count_after > 0, (
        "GATE-PR4-3 FAILED: No Part nodes found after suite. Has the DB been reset?"
    )
    assert embedding_after > 0, (
        "GATE-PR4-3 FAILED: No embedded parts found after suite. Has "
        "`partgraph embed` never been run against this DB?"
    )
    assert count_before == count_after, (
        f"GATE-PR4-3 FAILED: Part count changed from {count_before:,} to "
        f"{count_after:,} after the READ-ONLY GATE-PR4 suite."
    )
    assert embedding_before == embedding_after, (
        f"GATE-PR4-3 FAILED: has(embedding) count changed from "
        f"{embedding_before:,} to {embedding_after:,} after the READ-ONLY "
        "GATE-PR4 suite."
    )
