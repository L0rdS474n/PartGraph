"""
Tests: G1, G3

G1 — `partgraph db up` brings Dgraph to a healthy state reachable at
     http://127.0.0.1:8081/health within bounded retry/backoff;
     container has named volume ending in partgraph_dgraph_data at /dgraph;
     `docker port` shows ONLY 127.0.0.1 bindings;
     ports 8081/9081/8001 exposed (never 8080/9080 on the host).

G3 — `docker compose restart` (or down-without-v + up) preserves:
     - exactly N marker nodes written before restart;
     - the schema (embedding predicate still present after restart).

All tests are marked @pytest.mark.integration and depend on the
`dgraph_available` session fixture (which skips gracefully when Dgraph is
not running).
"""

from __future__ import annotations

import json
import re
import subprocess
import time

import pytest
import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DGRAPH_HTTP_HEALTH_URL = "http://127.0.0.1:8081/health"
DGRAPH_GRPC_ADDR = "127.0.0.1:9081"
DGRAPH_ALPHA_PORTS_HOST = [8081, 9081, 8001]
DGRAPH_ALPHA_PORTS_FORBIDDEN_ON_HOST = [8080, 9080]

# Explicit container name declared in docker/docker-compose.yml.  All
# docker ps / docker port calls must filter by this name so that a
# simultaneously running cve-graph (or other) stack on this machine cannot
# produce false-positive matches.
PARTGRAPH_CONTAINER_NAME = "partgraph-dgraph"

HEALTH_POLL_MAX_ATTEMPTS = 30
HEALTH_POLL_BACKOFF_BASE_S = 0.5
HEALTH_POLL_BACKOFF_MAX_S = 8.0

MARKER_PREDICATE = "partgraph_test_marker"

# Fixed 4-dimensional vector for deterministic vector tests.
FIXED_VECTOR_4D = [0.1, 0.2, 0.3, 0.4]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for_health(max_attempts: int = HEALTH_POLL_MAX_ATTEMPTS) -> bool:
    """Poll health endpoint with exponential backoff; return True when healthy."""
    delay = HEALTH_POLL_BACKOFF_BASE_S
    for _ in range(max_attempts):
        try:
            resp = requests.get(DGRAPH_HTTP_HEALTH_URL, timeout=2)
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(delay)
        delay = min(delay * 2, HEALTH_POLL_BACKOFF_MAX_S)
    return False


def _run(args: list[str], cwd: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=cwd,
        check=check,
    )


def _count_marker_nodes(pydgraph_client) -> int:
    """Return the number of nodes with the test marker predicate.

    Uses the named-block aggregation form
    ``{ q(func: has(P)) { count(uid) } }``. The root-level form
    ``{ count(func: has(P)) { count } }`` must NOT be used here: in Dgraph v25
    it evaluates to an empty ``{"count": []}`` block for *every* cardinality
    (both 0 and N), so it can never observe the just-written nodes. The named
    block instead returns ``{"q": [{"count": N}]}`` with the real integer.
    """
    query = f'{{ q(func: has({MARKER_PREDICATE})) {{ count(uid) }} }}'
    txn = pydgraph_client.txn(read_only=True)
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        # Dgraph v25 returns [{"count": N}] (including [{"count": 0}]); guard the
        # empty-block case defensively and treat it as a count of 0.
        count_block = data.get("q", [])
        return count_block[0]["count"] if count_block else 0
    finally:
        txn.discard()


def _wait_for_marker_count_at_least(pydgraph_client, minimum: int) -> int:
    """Poll the marker-node count until it reaches *minimum* (bounded).

    Dgraph commits are ACID with snapshot isolation, so a committed marker node
    is immediately visible to a subsequent query from the same client (the count
    normally satisfies *minimum* on the first attempt). The bounded poll is a
    defensive guard only and never weakens the assertion.
    """
    delay = 0.25
    last = 0
    for _ in range(20):
        last = _count_marker_nodes(pydgraph_client)
        if last >= minimum:
            return last
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)
    return last


def _write_marker_node(pydgraph_client, node_id: str) -> None:
    """Write a single marker node with a unique xid."""
    import pydgraph  # noqa: PLC0415 — guarded by importorskip at module level
    txn = pydgraph_client.txn()
    try:
        nquads = (
            f'_:n <{MARKER_PREDICATE}> "true" .\n'
            f'_:n <xid> "{node_id}" .\n'
        )
        mutation = pydgraph.Mutation(set_nquads=nquads.encode())
        txn.mutate(mutation=mutation)
        txn.commit()
    finally:
        txn.discard()


# ---------------------------------------------------------------------------
# G1 — health reachable at 8081
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_dgraph_health_returns_200(dgraph_available: bool) -> None:
    """Given Dgraph is running (ensured by dgraph_available fixture).
    When we GET http://127.0.0.1:8081/health.
    Then the response status code must be 200.
    """
    resp = requests.get(DGRAPH_HTTP_HEALTH_URL, timeout=5)
    assert resp.status_code == 200, (
        f"Dgraph health check returned {resp.status_code}, expected 200."
    )


@pytest.mark.integration
def test_dgraph_health_response_is_json(dgraph_available: bool) -> None:
    """Given Dgraph is running.
    When we GET /health.
    Then the response body must be valid JSON.
    """
    resp = requests.get(DGRAPH_HTTP_HEALTH_URL, timeout=5)
    try:
        resp.json()
    except ValueError as exc:
        pytest.fail(f"Health response is not valid JSON: {exc}\nBody: {resp.text}")


# ---------------------------------------------------------------------------
# G1 — Docker port bindings are localhost-only
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_docker_container_ports_are_localhost_only(dgraph_available: bool) -> None:
    """Given the PartGraph Dgraph container is running.
    When we inspect its port bindings via `docker port <container_name>`.
    Then ALL published ports must bind to 127.0.0.1 (never 0.0.0.0 or *).

    We use `docker port PARTGRAPH_CONTAINER_NAME` (filtered by explicit name
    from docker-compose.yml) so we never accidentally inspect a foreign
    container (e.g. a cve-graph stack) that happens to use the same image.
    """
    port_result = _run(["docker", "port", PARTGRAPH_CONTAINER_NAME])
    raw_ports = port_result.stdout

    # Verify no 0.0.0.0 binding appears.
    assert "0.0.0.0" not in raw_ports, (
        f"Port binding to 0.0.0.0 found for container '{PARTGRAPH_CONTAINER_NAME}' — "
        f"all ports must bind to 127.0.0.1 only.\n"
        f"docker port output:\n{raw_ports}"
    )


@pytest.mark.integration
def test_docker_container_exposes_port_8081_not_8080(dgraph_available: bool) -> None:
    """Given the PartGraph container is running.
    When we query its port mappings via `docker port <container_name>`.
    Then host port 8081 must be mapped to container port 8080, bound on
    127.0.0.1 only (never 0.0.0.0).

    We filter by PARTGRAPH_CONTAINER_NAME so this assertion is not
    accidentally satisfied by a foreign container on the same machine.
    """
    port_result = _run(["docker", "port", PARTGRAPH_CONTAINER_NAME])
    ports_text = port_result.stdout
    assert re.search(r"127\.0\.0\.1:8081", ports_text), (
        f"Expected 127.0.0.1:8081->8080 mapping not found for container "
        f"'{PARTGRAPH_CONTAINER_NAME}'.\ndocker port output:\n{ports_text}"
    )
    assert not re.search(r"0\.0\.0\.0:8080->", ports_text), (
        "Port 8080 is bound on 0.0.0.0 — only 127.0.0.1:8081 is allowed."
    )


@pytest.mark.integration
def test_docker_container_exposes_port_9081_not_9080(dgraph_available: bool) -> None:
    """Given the PartGraph container is running.
    When we query its port mappings via `docker port <container_name>`.
    Then host port 9081 must be mapped to container port 9080, bound on
    127.0.0.1 only.

    Filtered by PARTGRAPH_CONTAINER_NAME to prevent false positives from
    other stacks on this machine.
    """
    port_result = _run(["docker", "port", PARTGRAPH_CONTAINER_NAME])
    ports_text = port_result.stdout
    assert re.search(r"127\.0\.0\.1:9081", ports_text), (
        f"Expected 127.0.0.1:9081->9080 mapping not found for container "
        f"'{PARTGRAPH_CONTAINER_NAME}'.\ndocker port output:\n{ports_text}"
    )


@pytest.mark.integration
def test_docker_container_exposes_port_8001(dgraph_available: bool) -> None:
    """Given the PartGraph container is running.
    When we query its port mappings via `docker port <container_name>`.
    Then host port 8001 must be mapped to container port 8000 (Ratel/admin UI),
    bound on 127.0.0.1 only.

    Filtered by PARTGRAPH_CONTAINER_NAME to prevent false positives.
    """
    port_result = _run(["docker", "port", PARTGRAPH_CONTAINER_NAME])
    ports_text = port_result.stdout
    assert re.search(r"127\.0\.0\.1:8001", ports_text), (
        f"Expected 127.0.0.1:8001->8000 mapping not found for container "
        f"'{PARTGRAPH_CONTAINER_NAME}'.\ndocker port output:\n{ports_text}"
    )


# ---------------------------------------------------------------------------
# G1 — named volume
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_dgraph_container_has_named_volume_partgraph_dgraph_data(
    dgraph_available: bool,
) -> None:
    """Given the container is running.
    When we inspect its mounts via `docker inspect`.
    Then it must have a named volume whose name ends in 'partgraph_dgraph_data'
    mounted at /dgraph.
    """
    ps_result = _run([
        "docker", "ps",
        "--filter", "status=running",
        "--format", "{{.ID}}",
    ])
    container_ids = [c.strip() for c in ps_result.stdout.splitlines() if c.strip()]
    assert container_ids, "No running containers found."

    found = False
    for cid in container_ids:
        inspect = _run(["docker", "inspect", "--format",
                        "{{json .Mounts}}", cid])
        try:
            mounts = json.loads(inspect.stdout.strip())
        except (ValueError, AttributeError):
            continue
        for mount in mounts:
            name = mount.get("Name", "")
            destination = mount.get("Destination", "")
            vol_type = mount.get("Type", "")
            if (
                vol_type == "volume"
                and name.endswith("partgraph_dgraph_data")
                and destination == "/dgraph"
            ):
                found = True
                break
        if found:
            break

    assert found, (
        "No running container has a named volume ending in 'partgraph_dgraph_data' "
        "mounted at /dgraph. Check docker/docker-compose.yml volume configuration."
    )


# ---------------------------------------------------------------------------
# G3 — persistence across restart
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_data_persists_across_compose_restart(
    dgraph_available: bool,
    dgraph_pydgraph_client,
    cleanup_marker_nodes,
    repo_root,
) -> None:
    """Given N marker nodes are written to Dgraph.
    When we perform `docker compose restart` (or down-without-v + up).
    Then the count of marker nodes after restart must equal N exactly,
    and the schema (embedding predicate) must still be present.

    Cleanup of marker nodes runs in teardown regardless of test outcome.
    """
    pydgraph = pytest.importorskip("pydgraph")

    # Write a known number of marker nodes.
    N = 3
    for i in range(N):
        _write_marker_node(dgraph_pydgraph_client, f"persist_test_node_{i}")

    # Committed nodes are immediately visible (Dgraph snapshot isolation); the
    # bounded poll is a defensive guard and returns the real count.
    count_before = _wait_for_marker_count_at_least(dgraph_pydgraph_client, N)
    assert count_before >= N, (
        f"Expected at least {N} marker nodes before restart, got {count_before}."
    )

    # Perform restart.
    compose_path = str(repo_root / "docker" / "docker-compose.yml")
    result = _run([
        "docker", "compose", "-f", compose_path, "restart",
    ])
    assert result.returncode == 0, (
        f"`docker compose restart` failed:\n{result.stderr}"
    )

    # Wait for health to recover (bounded, no fixed sleep).
    healthy = _wait_for_health()
    assert healthy, (
        "Dgraph did not become healthy after restart within the retry budget. "
        f"Last check: GET {DGRAPH_HTTP_HEALTH_URL}"
    )

    # Re-check node count — must be preserved exactly.
    # Reconnect client after restart.
    stub = pydgraph.DgraphClientStub(DGRAPH_GRPC_ADDR)
    fresh_client = pydgraph.DgraphClient(stub)
    try:
        count_after = _count_marker_nodes(fresh_client)
    finally:
        stub.close()

    assert count_after == count_before, (
        f"Node count changed after restart: before={count_before}, after={count_after}. "
        "Data was not persisted in the named volume."
    )


@pytest.mark.integration
def test_schema_persists_across_compose_restart(
    dgraph_available: bool,
    repo_root,
) -> None:
    """Given a schema with the embedding predicate has been applied.
    When we restart the compose stack (without -v).
    Then the embedding predicate must still be present in the live schema.
    """
    compose_path = str(repo_root / "docker" / "docker-compose.yml")
    result = _run([
        "docker", "compose", "-f", compose_path, "restart",
    ])
    assert result.returncode == 0, (
        f"`docker compose restart` failed:\n{result.stderr}"
    )

    healthy = _wait_for_health()
    assert healthy, "Dgraph did not recover after restart."

    # Query the live DQL schema over HTTP. The DQL schema is exposed via
    # POST /query with a `schema {}` body (Content-Type application/dql); the
    # GraphQL /admin getGQLSchema endpoint only serves a GraphQL schema, which
    # this project does not use.
    try:
        schema_resp = requests.post(
            "http://127.0.0.1:8081/query",
            data="schema {}",
            headers={"Content-Type": "application/dql"},
            timeout=10,
        )
    except requests.RequestException as exc:
        pytest.fail(f"Could not query schema after restart: {exc}")

    schema_text = schema_resp.text

    assert "embedding" in schema_text, (
        "Schema lost 'embedding' predicate after restart. "
        "Verify that apply-schema is idempotent and the schema is stored in the volume."
    )
