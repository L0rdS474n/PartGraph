"""
Tests: GATE-PR4-1..2 — PR4 Semantic search acceptance gates.

@pytest.mark.integration — all tests require:
  - A running Dgraph instance (dgraph_available fixture).
  - The JLCPCB catalogue to have been ingested (PR2 ingest complete).
  - sentence_transformers installed (pytest.importorskip("sentence_transformers")).
  - Tests SKIP cleanly when DB is down or sentence_transformers is absent.

GATE-PR4-1: Embed ≤2000 parts including the MAX232 family via REAL model through
            the adaptive controller. Write embedding by uid. Then
            build_semantic_dql(encode("rs232 transceiver"), 10) — MAX232 NOT in
            query text — and assert a MAX232-family row (mpn_norm contains "232")
            is in the TOP-10. Print top-10. TEARDOWN: delete ONLY the embedding
            predicate on the embedded uids. Assert Part count unchanged.

GATE-PR4-2: get_system_reader() real snapshot: cpu_count >= 1, fractions in
            [0,1] (or None), regulate returns bounded directive.

Part count bookend: uses the same { q(func: type(Part)) { count(uid) } } form
as GATE-PR3 (safe in Dgraph v25, never root-level count(func:...)).
"""

from __future__ import annotations

import json
import sys
import time

import pytest

# Skip the entire module if sentence_transformers is not installed.
sentence_transformers = pytest.importorskip(
    "sentence_transformers",
    reason=(
        "sentence_transformers not installed; skipping GATE-PR4 tests. "
        'Install with: pip install -e ".[embed]"'
    ),
)

from partgraph.embed import build_embed_text, embed_write, generate_embeddings  # noqa: E402, F401
from partgraph.query.dql_builder import build_semantic_dql  # noqa: E402, F401
from partgraph.util.resources import ResourceController, SystemSnapshot, get_system_reader  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Suite-level state (part count bookend, mirrors GATE-PR3 pattern)
# ---------------------------------------------------------------------------

_suite_state: dict[str, int | None] = {"part_count_before": None}

# Maximum parts to embed in the gate test (bounded to keep CI tractable).
_MAX_EMBED_PARTS = 2000

# The embed dimension required by all PR4 components.
_EMBED_DIM = 384


# ---------------------------------------------------------------------------
# Helpers (read-only, mirrors test_gate_pr3.py pattern)
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


def _select_parts_for_embed(client, max_parts: int) -> list[dict]:
    """Select up to max_parts Part nodes including the MAX232 family.

    Strategy:
    1. Query MAX232-family parts (mpn_norm contains '232') first.
    2. Fill remaining slots deterministically from the full catalogue.

    Returns a list of raw part dicts with uid, xid, description, category, etc.
    """
    # Step 1: fetch MAX232-family parts.
    max232_query = (
        'query search($rx: string) { '
        'q(func: regexp(mpn_norm, $rx), first: 50) { '
        'uid xid mpn_norm description in_package { name } '
        '} }'
    )
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(max232_query, variables={"$rx": "/232/"})
        data = json.loads(resp.json)
        max232_parts = data.get("q", [])
    finally:
        txn.discard()

    # Step 2: fill up to max_parts from the full catalogue.
    remaining_slots = max_parts - len(max232_parts)
    max232_uids = {p["uid"] for p in max232_parts}

    if remaining_slots > 0:
        fill_query = (
            f"{{ q(func: type(Part), first: {max_parts}) "
            "{ uid xid mpn_norm description in_package { name } } }"
        )
        txn2 = client.txn(read_only=True)
        try:
            resp2 = txn2.query(fill_query)
            data2 = json.loads(resp2.json)
            all_parts = data2.get("q", [])
        finally:
            txn2.discard()

        # Add non-MAX232 parts deterministically (sorted by uid for reproducibility).
        fill_parts = sorted(
            [p for p in all_parts if p.get("uid") not in max232_uids],
            key=lambda p: p.get("uid", ""),
        )[:remaining_slots]

        combined = max232_parts + fill_parts
    else:
        combined = max232_parts[:max_parts]

    return combined[:max_parts]


def _encode_text(model, text: str) -> list[float]:
    """Encode a single text string using the real sentence_transformers model."""
    result = model.encode([text])
    # result is a numpy array of shape (1, dim); convert to list[float].
    return result[0].tolist()


def _delete_embedding_predicates(client, uids: list[str]) -> None:
    """Delete ONLY the <uid> <embedding> * . triples for the given uids.

    This teardown leaves all other Part predicates intact.
    """
    if not uids:
        return

    # Build del_nquads: one line per uid.
    nquads = "\n".join(f"<{uid}> <embedding> * ." for uid in uids)

    txn = client.txn()
    try:
        import pydgraph  # noqa: PLC0415
        mutation = pydgraph.Mutation(del_nquads=nquads.encode("utf-8"))
        txn.mutate(mutation=mutation)
        txn.commit()
    except Exception:  # noqa: BLE001 — best-effort teardown
        pass
    finally:
        txn.discard()


# ===========================================================================
# GATE-PR4-1: end-to-end embed + semantic search
# ===========================================================================

@pytest.mark.integration
def test_gate_pr4_1_embed_and_semantic_search_finds_max232(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR4-1: Embed ≤2000 parts, write embeddings, then semantic search for
    "rs232 transceiver" must return a MAX232-family part in the TOP-10.

    Given: Dgraph contains the ingested JLCPCB catalogue including MAX232 parts.
    When:
      1. Select ≤2000 parts including MAX232 family.
      2. Embed via real sentence-transformers model through adaptive controller.
      3. Write embedding predicate by uid (uid+embedding payload only).
      4. Build semantic DQL for "rs232 transceiver" with k=10.
      5. Execute the DQL against Dgraph.
      6. Assert a MAX232-family row (mpn_norm contains "232") is in the TOP-10.
    Then:
      - Wall seconds measured and printed.
      - TOP-10 results printed.
    Teardown:
      - Delete ONLY the embedding predicate on embedded uids.
      - Assert Part count unchanged (bookend).
    """
    client = dgraph_pydgraph_client
    _suite_state["part_count_before"] = _dgraph_part_count(client)

    # --- Step 1: Select parts ---
    parts_raw = _select_parts_for_embed(client, _MAX_EMBED_PARTS)
    assert parts_raw, "GATE-PR4-1: No parts found in Dgraph. Verify ingest completed."

    print(
        f"\n[GATE-PR4-1] Selected {len(parts_raw)} parts for embedding "
        f"(including MAX232 family).",
        file=sys.stderr,
    )

    # Convert raw dicts to namespace-like objects for build_embed_text.
    from types import SimpleNamespace  # noqa: PLC0415
    parts = []
    for raw in parts_raw:
        p = SimpleNamespace(
            uid=raw.get("uid"),
            xid=raw.get("xid"),
            description=raw.get("description"),
            category=None,  # not in selection for brevity
            package=(raw.get("in_package") or [{}])[0].get("name"),
            tags=[],
            mpn_norm=raw.get("mpn_norm", ""),
        )
        parts.append(p)

    # --- Step 2: Load real model and embed ---
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    model = SentenceTransformer("all-MiniLM-L6-v2")

    # Verify model output dimension matches our 384 contract.
    test_vec = model.encode(["test"])
    assert test_vec.shape[1] == _EMBED_DIM, (
        f"GATE-PR4-1: model must produce {_EMBED_DIM}-dim vectors; "
        f"got {test_vec.shape[1]}. Choose a 384-dim model."
    )

    # Build an adaptive controller.
    controller = ResourceController(min_batch=8, max_batch=64, max_pause=5.0)

    # Time the embedding + write.
    t_start = time.monotonic()

    # We use embed_write which handles batching, controller, and uid-only writes.
    embed_write(
        iter(parts),
        client,
        encoder=lambda texts: model.encode(texts).tolist(),
        controller=controller,
        sleep=time.sleep,
        progress=lambda done, total: print(
            f"[GATE-PR4-1] Embedded {done}/{total}", file=sys.stderr, end="\r"
        ) if done % 100 == 0 else None,
    )

    wall_seconds = time.monotonic() - t_start
    print(
        f"\n[GATE-PR4-1] Embedding + write completed in {wall_seconds:.1f}s",
        file=sys.stderr,
    )

    # --- Step 3: Semantic search for "rs232 transceiver" ---
    query_text_embed = "rs232 transceiver"
    query_vector = _encode_text(model, query_text_embed)
    assert len(query_vector) == _EMBED_DIM, (
        f"GATE-PR4-1: query vector must be {_EMBED_DIM}-dim; got {len(query_vector)}"
    )

    # build_semantic_dql uses inline literal; "rs232" must NOT appear in query text.
    dql, variables = build_semantic_dql(query_vector, k=10)

    assert "rs232" not in dql.lower(), (
        f"GATE-PR4-1: query text must not contain the literal 'rs232' "
        f"(vector is inline; no text injection). Got query:\n{dql}"
    )

    # Execute.
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(dql, variables=variables if variables else None)
        data = json.loads(resp.json)
    finally:
        txn.discard()

    # Extract semantic block results.
    semantic_rows = data.get("semantic", data.get("similar", []))

    print(
        f"[GATE-PR4-1] Semantic search 'rs232 transceiver' returned {len(semantic_rows)} rows.",
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
        "Verify: (1) MAX232 parts were embedded, (2) model produces useful embeddings, "
        "(3) Dgraph vector index is active."
    )

    # --- Teardown: delete embedding predicates ---
    embedded_uids = [p.uid for p in parts if p.uid is not None]
    print(
        f"[GATE-PR4-1] Teardown: deleting embedding predicate on "
        f"{len(embedded_uids)} uids.",
        file=sys.stderr,
    )
    _delete_embedding_predicates(client, embedded_uids)


# ===========================================================================
# GATE-PR4-2: real SystemSnapshot from get_system_reader
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
# GATE-PR4-3: Part count unchanged after the suite (read-only proof + teardown)
# ===========================================================================

@pytest.mark.integration
def test_gate_pr4_3_part_count_unchanged_after_suite(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """GATE-PR4-3: The Part count in Dgraph is identical before and after the
    GATE-PR4 suite, proving teardown (embedding predicate deletion) did not
    remove any Part nodes.

    Given: part_count_before was recorded in GATE-PR4-1.
    When:  we count Part nodes again after teardown.
    Then:  both counts are equal and > 0.
    """
    count_before = _suite_state["part_count_before"]
    count_after = _dgraph_part_count(dgraph_pydgraph_client)

    print(
        f"\n[GATE-PR4-3] Part count before={count_before}  after={count_after:,}",
        file=sys.stderr,
    )

    assert count_after > 0, (
        "GATE-PR4-3 FAILED: No Part nodes found after suite. Has the DB been reset?"
    )

    if count_before is None:
        pytest.skip(
            "GATE-PR4-1 did not run (DB unavailable or sentence_transformers absent); "
            "cannot compare before/after counts."
        )

    assert count_before == count_after, (
        f"GATE-PR4-3 FAILED: Part count changed from {count_before:,} to "
        f"{count_after:,} after GATE-PR4 suite. "
        "The embedding teardown must delete ONLY the embedding predicate, "
        "not the Part nodes themselves."
    )
