"""
Shared fixtures for the PartGraph test suite.

Provides:
- repo_root: pathlib.Path to the repository root (absolute, not derived from CWD).
- dgraph_available: session-scoped marker that skips integration tests when
  Dgraph is not reachable at http://127.0.0.1:8081/health.
- dgraph_pydgraph_client: a pydgraph client connected to 127.0.0.1:9081,
  skipped if pydgraph is not installed or Dgraph is not reachable.
- cleanup_marker_nodes: function-scoped helper that deletes all nodes carrying
  the `partgraph_test_marker` predicate after each integration test.
"""

from __future__ import annotations

import pathlib
import time

import pytest
import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DGRAPH_HTTP_HEALTH_URL = "http://127.0.0.1:8081/health"
DGRAPH_GRPC_ADDR = "127.0.0.1:9081"

# Bounded retry configuration for health polling (no fixed sleeps in tests).
HEALTH_POLL_MAX_ATTEMPTS = 20
HEALTH_POLL_BACKOFF_BASE_S = 0.5
HEALTH_POLL_BACKOFF_MAX_S = 8.0

# Predicate added to every ephemeral test node so teardown is targeted.
TEST_MARKER_PREDICATE = "partgraph_test_marker"

# ---------------------------------------------------------------------------
# Repository root
# ---------------------------------------------------------------------------

# Derive from this file's location: tests/conftest.py -> project root is one
# level up.  This is an absolute path and never contains user-specific segments
# at import time — it is constructed from __file__ which pytest controls.
_TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = _TESTS_DIR.parent


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    """Return the absolute path to the repository root.

    Given: the conftest.py lives at tests/conftest.py inside the repo.
    When: any test requests this fixture.
    Then: the returned path points to the repository root and exists.
    """
    assert REPO_ROOT.is_dir(), f"repo_root {REPO_ROOT} is not a directory"
    return REPO_ROOT


# ---------------------------------------------------------------------------
# Dgraph availability
# ---------------------------------------------------------------------------

def _poll_dgraph_health() -> bool:
    """Return True if Dgraph health endpoint responds 200 within retry budget."""
    delay = HEALTH_POLL_BACKOFF_BASE_S
    for _ in range(HEALTH_POLL_MAX_ATTEMPTS):
        try:
            resp = requests.get(DGRAPH_HTTP_HEALTH_URL, timeout=2)
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(delay)
        delay = min(delay * 2, HEALTH_POLL_BACKOFF_MAX_S)
    return False


@pytest.fixture(scope="session")
def dgraph_available():
    """Skip the calling test (and all that depend on this fixture) when Dgraph
    is not reachable.

    Given: an integration test requires a running Dgraph instance.
    When: http://127.0.0.1:8081/health does not return 200 after bounded retry.
    Then: the test is skipped with a clear reason rather than failing.
    """
    try:
        resp = requests.get(DGRAPH_HTTP_HEALTH_URL, timeout=2)
        reachable = resp.status_code == 200
    except requests.RequestException:
        reachable = False

    if not reachable:
        pytest.skip(
            reason=(
                f"Dgraph not reachable at {DGRAPH_HTTP_HEALTH_URL}. "
                "Run `partgraph db up` before executing integration tests."
            )
        )
    return True


# ---------------------------------------------------------------------------
# pydgraph client
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def dgraph_pydgraph_client(dgraph_available):
    """Yield a connected pydgraph client; skip if pydgraph is not installed.

    Given: Dgraph is reachable and pydgraph is installed.
    When: an integration test requests this fixture.
    Then: a live pydgraph.DgraphClient connected to 127.0.0.1:9081 is returned,
          and the connection is closed after the session ends.
    """
    pydgraph = pytest.importorskip(
        "pydgraph",
        reason="pydgraph not installed; skipping integration tests that require it.",
    )
    stub = pydgraph.DgraphClientStub(DGRAPH_GRPC_ADDR)
    client = pydgraph.DgraphClient(stub)
    yield client
    stub.close()


# ---------------------------------------------------------------------------
# Marker-node cleanup helper
# ---------------------------------------------------------------------------

@pytest.fixture
def cleanup_marker_nodes(dgraph_pydgraph_client):
    """Delete all nodes tagged with TEST_MARKER_PREDICATE after each test.

    Given: one or more ephemeral marker nodes were written during a test.
    When: the test function completes (pass or fail).
    Then: all nodes with `partgraph_test_marker` set to "true" are deleted from
          Dgraph so subsequent tests start with a clean slate.

    Yields control to the test body; cleanup runs in the finally block.
    """
    pydgraph = pytest.importorskip("pydgraph")
    client = dgraph_pydgraph_client

    yield  # --- test body runs here ---

    # Teardown: delete all marker nodes regardless of test outcome.
    delete_query = (
        f'{{ nodes(func: has({TEST_MARKER_PREDICATE})) {{ uid }} }}'
    )
    txn = client.txn()
    try:
        resp = txn.query(delete_query)
        import json
        data = json.loads(resp.json)
        uids_to_delete = [
            node["uid"] for node in data.get("nodes", [])
        ]
        if uids_to_delete:
            # Explicitly delete the marker, xid and embedding predicates as well
            # as the whole node. In Dgraph v25, `<uid> * * .` alone does NOT
            # reliably clear an indexed `float32vector` predicate (the embedding
            # value survives and lingers in the hnsw index — a stale vector of a
            # different dimension then poisons later inserts). Deleting the named
            # predicates explicitly is required for deterministic teardown.
            parts: list[str] = []
            for uid in uids_to_delete:
                parts.append(f"<{uid}> <{TEST_MARKER_PREDICATE}> * .")
                parts.append(f"<{uid}> <xid> * .")
                parts.append(f"<{uid}> <embedding> * .")
                parts.append(f"<{uid}> * * .")
            mutation = pydgraph.Mutation(del_nquads="\n".join(parts).encode())
            txn.mutate(mutation=mutation)
        txn.commit()
    except Exception:  # noqa: BLE001 — best-effort cleanup must not mask test failures
        pass
    finally:
        txn.discard()
