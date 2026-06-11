"""
Tests: R5

Verifies that docker/docker-compose.yml is valid and matches the exact
configuration required:
- Image: dgraph/standalone:v25.3.4 (no :latest)
- Port mappings: 127.0.0.1:8081->8080, 127.0.0.1:9081->9080, 127.0.0.1:8001->8000
- Named volume: partgraph_dgraph_data mounted at /dgraph
- No bind mounts to absolute host paths
- Host-side port strings "8080:" and "9080:" must NOT appear
- No "0.0.0.0" anywhere in the file
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixture: parsed compose config
# ---------------------------------------------------------------------------

COMPOSE_REL = "docker/docker-compose.yml"

# Expected exact values
EXPECTED_IMAGE = "dgraph/standalone:v25.3.4"
EXPECTED_VOLUME_NAME = "partgraph_dgraph_data"
EXPECTED_VOLUME_MOUNT = "/dgraph"
EXPECTED_PORT_MAPPINGS = [
    ("127.0.0.1", 8081, 8080),   # host_ip, host_port, container_port
    ("127.0.0.1", 9081, 9080),
    ("127.0.0.1", 8001, 8000),
]
FORBIDDEN_HOST_PORT_STRINGS = ["8080:", "9080:"]
FORBIDDEN_IP = "0.0.0.0"


def _load_compose_yaml(repo_root: pathlib.Path) -> dict:
    """Load docker-compose.yml via `docker compose config` or fallback pyyaml.

    Prefer docker compose config so variable interpolation is resolved;
    fall back to raw yaml.safe_load so tests remain deterministic without
    a Docker daemon.
    """
    compose_path = repo_root / COMPOSE_REL
    assert compose_path.exists(), (
        f"{COMPOSE_REL} does not exist. Create it before running tests."
    )

    # Attempt docker compose config for canonical validation.
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "config"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return yaml.safe_load(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: parse raw YAML deterministically.
    return yaml.safe_load(compose_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose_config(repo_root: pathlib.Path) -> dict:
    """Return the parsed docker-compose configuration."""
    return _load_compose_yaml(repo_root)


@pytest.fixture(scope="module")
def compose_raw_text(repo_root: pathlib.Path) -> str:
    """Return raw text of docker-compose.yml for string-level checks."""
    return (repo_root / COMPOSE_REL).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# R5 — image tag
# ---------------------------------------------------------------------------

def test_dgraph_image_is_exact_pinned_tag(compose_config: dict) -> None:
    """Given the compose file defines a dgraph service.
    When we inspect the image field.
    Then it must be exactly 'dgraph/standalone:v25.3.4' — no :latest.
    """
    services = compose_config.get("services", {})
    assert services, "No services defined in docker-compose.yml"

    for svc_name, svc in services.items():
        image = svc.get("image", "")
        if "dgraph" in image.lower():
            assert image == EXPECTED_IMAGE, (
                f"Service '{svc_name}': image must be '{EXPECTED_IMAGE}', got '{image}'"
            )
            return

    pytest.fail(
        f"No dgraph service found in docker-compose.yml. "
        f"Services present: {list(services.keys())}"
    )


def test_no_latest_tag_used(compose_raw_text: str) -> None:
    """Given docker-compose.yml exists.
    When we scan its text.
    Then ':latest' must not appear anywhere.
    """
    assert ":latest" not in compose_raw_text, (
        "docker-compose.yml must not use ':latest' image tags"
    )


# ---------------------------------------------------------------------------
# R5 — port mappings
# ---------------------------------------------------------------------------

def _extract_port_mappings(compose_config: dict) -> list[tuple[str, int, int]]:
    """Extract (host_ip, host_port, container_port) from all services."""
    results = []
    for svc in compose_config.get("services", {}).values():
        for port_entry in svc.get("ports", []):
            if isinstance(port_entry, dict):
                # docker compose config expanded form
                host_ip = port_entry.get("host_ip", "")
                host_port = int(port_entry.get("published", 0))
                container_port = int(port_entry.get("target", 0))
                results.append((host_ip, host_port, container_port))
            elif isinstance(port_entry, str):
                # short syntax: "127.0.0.1:8081:8080"
                m = re.match(
                    r"^(?P<ip>[^:]+):(?P<hp>\d+):(?P<cp>\d+)$", port_entry
                )
                if m:
                    results.append((
                        m.group("ip"),
                        int(m.group("hp")),
                        int(m.group("cp")),
                    ))
    return results


@pytest.mark.parametrize(
    "expected_ip,expected_host,expected_container",
    EXPECTED_PORT_MAPPINGS,
    ids=["8081->8080", "9081->9080", "8001->8000"],
)
def test_port_mapping_exists_and_is_localhost_only(
    compose_config: dict,
    expected_ip: str,
    expected_host: int,
    expected_container: int,
) -> None:
    """Given the compose file defines port mappings.
    When we inspect each mapping.
    Then each must bind to 127.0.0.1 (not 0.0.0.0 or all interfaces).
    """
    mappings = _extract_port_mappings(compose_config)
    found = any(
        ip == expected_ip and hp == expected_host and cp == expected_container
        for ip, hp, cp in mappings
    )
    assert found, (
        f"Expected port mapping {expected_ip}:{expected_host}->{expected_container} "
        f"not found. Actual mappings: {mappings}"
    )


def test_no_forbidden_host_port_8080(compose_raw_text: str) -> None:
    """Given docker-compose.yml exists.
    When we scan its raw text.
    Then '8080:' must not appear as a host-side port binding (would expose on
    all interfaces or conflict with the expected mapping).
    """
    assert "8080:" not in compose_raw_text, (
        "docker-compose.yml must not bind host port 8080 (use 8081 via 127.0.0.1)"
    )


def test_no_forbidden_host_port_9080(compose_raw_text: str) -> None:
    """Given docker-compose.yml exists.
    When we scan its raw text.
    Then '9080:' must not appear as a host-side port binding.
    """
    assert "9080:" not in compose_raw_text, (
        "docker-compose.yml must not bind host port 9080 (use 9081 via 127.0.0.1)"
    )


def test_no_0000_ip_binding(compose_raw_text: str) -> None:
    """Given docker-compose.yml exists.
    When we scan its raw text.
    Then '0.0.0.0' must not appear anywhere.
    """
    assert FORBIDDEN_IP not in compose_raw_text, (
        f"docker-compose.yml must not contain '{FORBIDDEN_IP}'; "
        "all ports must be bound to 127.0.0.1 only."
    )


# ---------------------------------------------------------------------------
# R5 — named volume
# ---------------------------------------------------------------------------

def test_named_volume_declared(compose_config: dict) -> None:
    """Given the compose file defines volumes.
    When we inspect top-level volume declarations.
    Then 'partgraph_dgraph_data' must be declared.
    """
    top_volumes = compose_config.get("volumes", {})
    assert EXPECTED_VOLUME_NAME in top_volumes, (
        f"Top-level volume '{EXPECTED_VOLUME_NAME}' not declared in docker-compose.yml. "
        f"Declared volumes: {list(top_volumes.keys())}"
    )


def test_named_volume_mounted_at_dgraph(compose_config: dict) -> None:
    """Given the compose file mounts the partgraph_dgraph_data volume.
    When we inspect service volume mounts.
    Then partgraph_dgraph_data must be mounted at /dgraph inside the container.
    """
    services = compose_config.get("services", {})
    for svc_name, svc in services.items():
        for vol in svc.get("volumes", []):
            if isinstance(vol, dict):
                source = vol.get("source", "")
                target = vol.get("target", "")
                if source == EXPECTED_VOLUME_NAME and target == EXPECTED_VOLUME_MOUNT:
                    return
            elif isinstance(vol, str):
                # short syntax: "partgraph_dgraph_data:/dgraph"
                if vol == f"{EXPECTED_VOLUME_NAME}:{EXPECTED_VOLUME_MOUNT}":
                    return
    pytest.fail(
        f"Volume '{EXPECTED_VOLUME_NAME}' is not mounted at '{EXPECTED_VOLUME_MOUNT}' "
        f"in any service. Services: {list(services.keys())}"
    )


# ---------------------------------------------------------------------------
# Security review addition — explicit container_name for unambiguous filtering
# ---------------------------------------------------------------------------

def test_dgraph_service_has_explicit_container_name_containing_partgraph(
    compose_config: dict,
) -> None:
    """Given the compose file defines the Dgraph service.
    When we inspect the service configuration.
    Then the service must declare an explicit 'container_name' whose value
    contains the substring 'partgraph'.

    An explicit container_name is required so that integration-test port and
    volume assertions can filter containers by name (e.g.
    `docker ps --filter name=<container_name>`) and are not accidentally
    satisfied by a foreign container (e.g. a cve-graph stack) that happens to
    be running the same image on this machine.
    """
    services = compose_config.get("services", {})
    assert services, "No services defined in docker-compose.yml"

    for svc_name, svc in services.items():
        image = svc.get("image", "")
        if "dgraph" in image.lower():
            container_name = svc.get("container_name", "")
            assert container_name, (
                f"Service '{svc_name}' does not declare an explicit 'container_name'. "
                "An explicit container_name is required so integration-test filters "
                "target only the PartGraph container."
            )
            assert "partgraph" in container_name.lower(), (
                f"Service '{svc_name}' container_name '{container_name}' must contain "
                "'partgraph' so integration tests can filter by name without false positives."
            )
            return

    pytest.fail(
        "No dgraph service found in docker-compose.yml — "
        f"cannot verify container_name. Services: {list(services.keys())}"
    )


def test_no_absolute_host_bind_mounts(compose_config: dict) -> None:
    """Given the compose file defines volume mounts.
    When we inspect every service volume entry.
    Then no mount must use an absolute host path (bind mount).

    Named volumes and relative paths are acceptable; absolute host paths
    create environment-specific dependencies.
    """
    services = compose_config.get("services", {})
    bind_mounts = []
    for svc_name, svc in services.items():
        for vol in svc.get("volumes", []):
            if isinstance(vol, dict):
                if vol.get("type") == "bind":
                    bind_mounts.append((svc_name, vol))
            elif isinstance(vol, str):
                # short syntax: if source starts with / it's an absolute bind mount
                parts = vol.split(":")
                if parts and parts[0].startswith("/"):
                    bind_mounts.append((svc_name, vol))

    assert not bind_mounts, (
        f"Absolute host bind mounts found (not allowed): {bind_mounts}"
    )
