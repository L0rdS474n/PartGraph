"""
Tests: G2

G2 — Applying schema/partgraph.dql via the project's schema-apply path
     (pydgraph gRPC on 127.0.0.1:9081) succeeds.
     Live schema (HTTP /admin or /alter on 8081) shows `embedding` as
     float32vector with hnsw index metric cosine.
     Smoke test: write >=2 ephemeral marker nodes with fixed 384-dim vectors
     (matching the production embedding dimension), run DQL
     `similar_to(embedding, 2, "<fixed vector>")`, expect >=1 result, then CLEAN
     UP marker nodes — including the embedding predicate — in teardown.

All tests are marked @pytest.mark.integration and skip gracefully when
Dgraph is not reachable or pydgraph is not installed.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import requests

# Guard pydgraph at module level; tests in this file will skip (not error)
# if pydgraph is absent.
pydgraph = pytest.importorskip(
    "pydgraph",
    reason="pydgraph not installed; skipping G2 integration tests.",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DGRAPH_HTTP_BASE = "http://127.0.0.1:8081"
DGRAPH_GRPC_ADDR = "127.0.0.1:9081"
SCHEMA_REL = "schema/partgraph.dql"
MARKER_PREDICATE = "partgraph_test_marker"

# The smoke-test vectors MUST match the production embedding dimension
# (all-MiniLM-L6-v2, 384-dim — ADR-0008). A single hnsw cosine index can only
# hold vectors of one dimension, so writing a shorter smoke vector into the same
# `embedding` predicate poisons later 384-dim inserts ("can not compute cosine
# distance on vectors of different lengths"). Keeping the smoke vectors at 384-dim
# makes the index dimensionally uniform.
EMBED_DIM = 384

# Fixed, deterministic vectors (no randomness); two distinct directions so
# similar_to has >=2 nodes to rank.
FIXED_VECTOR = [round(0.1 + 0.0001 * i, 4) for i in range(EMBED_DIM)]
FIXED_VECTOR_STR = "[" + ",".join(str(v) for v in FIXED_VECTOR) + "]"
SECOND_VECTOR = [round(0.2 + 0.0001 * i, 4) for i in range(EMBED_DIM)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _query_live_schema_text() -> str:
    """Return live Dgraph schema text via HTTP; try multiple endpoints."""
    endpoints = [
        ("POST", f"{DGRAPH_HTTP_BASE}/admin", {"query": "{ getGQLSchema { schema } }"}),
        ("GET",  f"{DGRAPH_HTTP_BASE}/query", None),
    ]
    for method, url, payload in endpoints:
        try:
            if method == "POST" and payload:
                resp = requests.post(url, json=payload, timeout=10)
            else:
                resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                return resp.text
        except requests.RequestException:
            continue
    return ""


def _query_dgraph_schema_via_dql(client) -> str:
    """Return the Dgraph schema string via DQL `schema {}` query.

    pydgraph returns ``resp.json`` as bytes; decode to ``str`` so callers can do
    plain substring / regex checks.
    """
    txn = client.txn(read_only=True)
    try:
        resp = txn.query("schema { }")
        raw = resp.json
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw
    finally:
        txn.discard()


def _write_vector_node(client, node_id: str, vector: list[float]) -> str:
    """Write a node with embedding and marker predicate; return the uid."""
    vector_str = "[" + ",".join(str(v) for v in vector) + "]"
    nquads = (
        f'_:n <{MARKER_PREDICATE}> "true" .\n'
        f'_:n <xid> "{node_id}" .\n'
        f'_:n <embedding> "{vector_str}"^^<geo:Point> .\n'
    )
    # Dgraph float32vector uses direct value syntax in NQuads:
    # <uid> <embedding> "[0.1,0.2,0.3,0.4]"^^<xs:float32vector> .
    # We use a flexible format — the actual type URI depends on the Dgraph version.
    # Try with plain string first; the schema type coerces it.
    nquads_v2 = (
        f'_:n <{MARKER_PREDICATE}> "true" .\n'
        f'_:n <xid> "{node_id}" .\n'
        f'_:n <embedding> "{vector_str}" .\n'
    )
    txn = client.txn()
    try:
        mutation = pydgraph.Mutation(set_nquads=nquads_v2.encode())
        response = txn.mutate(mutation=mutation)
        txn.commit()
        uids = response.uids
        return uids.get("n", "")
    finally:
        txn.discard()


def _delete_all_marker_nodes(client) -> None:
    """Delete all nodes carrying MARKER_PREDICATE (best-effort)."""
    query = f'{{ nodes(func: has({MARKER_PREDICATE})) {{ uid }} }}'
    txn = client.txn()
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        uids = [node["uid"] for node in data.get("nodes", [])]
        if uids:
            # Delete the named predicates explicitly as well as the whole node;
            # `<uid> * * .` alone does not reliably clear an unindexed predicate
            # from the Dgraph v25 `has()` posting list.
            parts: list[str] = []
            for uid in uids:
                parts.append(f"<{uid}> <{MARKER_PREDICATE}> * .")
                parts.append(f"<{uid}> <xid> * .")
                parts.append(f"<{uid}> <embedding> * .")
                parts.append(f"<{uid}> * * .")
            mutation = pydgraph.Mutation(del_nquads="\n".join(parts).encode())
            txn.mutate(mutation=mutation)
        txn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        txn.discard()


# ---------------------------------------------------------------------------
# G2 — schema apply and live schema verification
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_schema_apply_via_pydgraph_succeeds(
    dgraph_available: bool,
    dgraph_pydgraph_client,
    repo_root: pathlib.Path,
) -> None:
    """Given Dgraph is running and pydgraph is installed.
    When we read schema/partgraph.dql and apply it via pydgraph's alter operation.
    Then the operation must complete without raising an exception.
    """
    schema_path = repo_root / SCHEMA_REL
    assert schema_path.exists(), f"{SCHEMA_REL} does not exist — cannot apply schema."
    schema_dql = schema_path.read_text(encoding="utf-8")
    assert schema_dql.strip(), f"{SCHEMA_REL} is empty."

    operation = pydgraph.Operation(schema=schema_dql)
    # Must not raise.
    dgraph_pydgraph_client.alter(operation)


@pytest.mark.integration
def test_live_schema_contains_embedding_float32vector(
    dgraph_available: bool,
    dgraph_pydgraph_client,
) -> None:
    """Given the schema has been applied.
    When we query the live schema via DQL.
    Then 'embedding' must appear with 'float32vector' type.
    """
    schema_json = _query_dgraph_schema_via_dql(dgraph_pydgraph_client)
    assert "embedding" in schema_json, (
        "Predicate 'embedding' not found in live Dgraph schema. "
        "Apply the schema first."
    )
    assert "float32vector" in schema_json, (
        "Type 'float32vector' not found for 'embedding' in live schema. "
        f"Schema response excerpt: {schema_json[:500]}"
    )


@pytest.mark.integration
def test_live_schema_embedding_has_hnsw_cosine(
    dgraph_available: bool,
    dgraph_pydgraph_client,
) -> None:
    """Given the schema has been applied.
    When we query the live schema via DQL.
    Then the embedding predicate must list 'hnsw' as index type and 'cosine'
    as the metric.
    """
    schema_json = _query_dgraph_schema_via_dql(dgraph_pydgraph_client)
    assert "hnsw" in schema_json, (
        f"'hnsw' index not found in live schema. Got: {schema_json[:500]}"
    )
    assert "cosine" in schema_json, (
        f"'cosine' metric not found in live schema. Got: {schema_json[:500]}"
    )


# ---------------------------------------------------------------------------
# G2 — vector smoke test: write, query, cleanup
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_vector_similarity_smoke_test(
    dgraph_available: bool,
    dgraph_pydgraph_client,
    cleanup_marker_nodes,
) -> None:
    """Given the schema with hnsw cosine embedding is applied.
    When we write >=2 ephemeral marker nodes with fixed 384-dim vectors and
    run a DQL similar_to query.
    Then we must get >=1 result back.
    After the test, all marker nodes (and their embedding predicate) are removed
    by the cleanup_marker_nodes fixture (runs in teardown regardless of outcome).

    Vectors used (384-dim, matching the production embedding dimension):
      Node A: FIXED_VECTOR  = [0.1, 0.1001, 0.1002, ...]
      Node B: SECOND_VECTOR = [0.2, 0.2001, 0.2002, ...]
    Query vector: FIXED_VECTOR — top-2 similar_to.
    """
    client = dgraph_pydgraph_client

    # Write two marker nodes.
    uid_a = _write_vector_node(client, "g2_smoke_node_a", FIXED_VECTOR)
    uid_b = _write_vector_node(client, "g2_smoke_node_b", SECOND_VECTOR)

    assert uid_a or uid_b, (
        "Neither vector node was written successfully — check embedding schema."
    )

    # Run similar_to query.
    query_vector_str = FIXED_VECTOR_STR
    dql_query = (
        f'{{ similar(func: similar_to(embedding, 2, "{query_vector_str}")) '
        f'{{ uid xid {MARKER_PREDICATE} }} }}'
    )
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(dql_query)
        data = json.loads(resp.json)
        results = data.get("similar", [])
    finally:
        txn.discard()

    assert len(results) >= 1, (
        f"similar_to query returned 0 results. "
        f"Expected >=1 from {2} written nodes. "
        f"Query: {dql_query}\n"
        f"Response: {resp.json}"
    )


@pytest.mark.integration
def test_vector_marker_nodes_cleaned_up_after_test(
    dgraph_available: bool,
    dgraph_pydgraph_client,
) -> None:
    """Given the cleanup_marker_nodes fixture was active for the previous test.
    When we count nodes with the marker predicate after that test completed.
    Then the count should be 0 (teardown removed all marker nodes).

    NOTE: This test is only meaningful if run AFTER test_vector_similarity_smoke_test.
    It verifies the cleanup mechanism itself is working.
    """
    # This test does not use cleanup_marker_nodes itself — it verifies the
    # state left by the previous test's cleanup.
    client = dgraph_pydgraph_client
    query = f'{{ count(func: has({MARKER_PREDICATE})) {{ count }} }}'
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        # Dgraph v25 returns an empty `count` block ({"count": []}) when zero
        # nodes match, rather than [{"count": 0}]. Treat that as a count of 0.
        count_block = data.get("count", [])
        count = count_block[0]["count"] if count_block else 0
    finally:
        txn.discard()

    assert count == 0, (
        f"Found {count} marker nodes after cleanup — teardown did not remove all nodes. "
        "Check the cleanup_marker_nodes fixture in conftest.py."
    )


# ---------------------------------------------------------------------------
# Negative path: schema predicate 'embedding' is not a plain string type
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_embedding_predicate_is_not_plain_string(
    dgraph_available: bool,
    dgraph_pydgraph_client,
) -> None:
    """Given the schema has been applied.
    When we inspect the live schema's embedding type declaration.
    Then the type must NOT be 'string' — it must be 'float32vector'.

    This prevents regression where embedding is defined as a generic string,
    which would silently accept data but not support vector similarity search.
    """
    schema_json = _query_dgraph_schema_via_dql(dgraph_pydgraph_client)
    # Look for any declaration that would indicate embedding is a plain string.
    # A valid float32vector declaration will contain "float32vector", not "string".
    import re  # noqa: PLC0415
    # Find the embedding predicate entry in the schema JSON.
    match = re.search(r'"predicate"\s*:\s*"embedding"[^}]*"type"\s*:\s*"([^"]+)"', schema_json)
    if match:
        pred_type = match.group(1)
        assert pred_type == "float32vector", (
            f"Predicate 'embedding' has type '{pred_type}' in live schema, "
            "expected 'float32vector'."
        )
