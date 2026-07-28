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
- stop_grace_period matches STOP_GRACE_SECONDS (Gate 7 — see below)

Gate 7 addition: live journal evidence on this host proved Compose's own
10s stop_grace_period DEFAULT (the same default a bare `docker/podman stop`
without an explicit `-t` uses) insufficient under load — after a 14h run,
`StopSignal SIGTERM failed to stop container partgraph-dgraph in 10
seconds, resorting to SIGKILL` (unit exit status 137). Without an explicit
`stop_grace_period:`, a Compose-started instance inherits that same 10s
default, so it needs the SAME fix `partgraph.util.lifecycle.
STOP_GRACE_SECONDS` (our own engine `stop` sweep's budget) already pins.
`STOP_GRACE_SECONDS` is imported directly (never re-declared as a second
literal) so the two pins can never silently drift apart — a change to one
without the other fails this test.

HONESTY BOUNDARY: this ONLY covers Compose-started instances. It does NOT
reach the quadlet/systemd path — that unit's `ExecStop=podman rm -v -f`
uses whatever stop-timeout was baked in at quadlet-generation time,
independent of this file, and remains PR-B1 territory.
"""

from __future__ import annotations

import math
import pathlib
import re
import subprocess

import pytest
import yaml

from partgraph.util.container import ContainerEngineError, compose_command
from partgraph.util.lifecycle import STOP_GRACE_SECONDS


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

    # Attempt docker/podman compose config for canonical validation.
    # compose_command() is called INSIDE the try so that ContainerEngineError
    # (raised when no engine is on PATH) is caught by the except clause and the
    # raw-YAML fallback fires — keeping the structural tests hermetic on CI.
    try:
        result = subprocess.run(
            [*compose_command(), "-f", str(compose_path), "config"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return yaml.safe_load(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, ContainerEngineError):
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


# ---------------------------------------------------------------------------
# Gate 7 — stop_grace_period matches STOP_GRACE_SECONDS (load-tolerant
# shutdown budget; live SIGKILL/exit-137 incident on this host).
# ---------------------------------------------------------------------------

def _normalize_duration_seconds(value: object) -> float:
    """Best-effort parse a Compose duration value into a float of seconds.

    Accepts a bare int/float (already seconds — some `compose config`
    expansions normalize durations this way) or a Go-duration-style string
    such as ``"60s"``/``"1m"``/``"1m30s"``. Returns ``math.nan`` for
    anything else (including ``None``) so a comparison against the
    expected value fails loudly rather than silently passing.
    """
    if isinstance(value, bool):
        return math.nan
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(
            r"(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+(?:\.\d+)?)s)?",
            value.strip(),
        )
        if match and any(match.groupdict().values()):
            hours = match.group("hours")
            minutes = match.group("minutes")
            seconds = match.group("seconds")
            total = 0.0
            if hours:
                total += int(hours) * 3600
            if minutes:
                total += int(minutes) * 60
            if seconds:
                total += float(seconds)
            return total
    return math.nan


def test_dgraph_service_declares_stop_grace_period_matching_engine_budget(
    compose_config: dict,
) -> None:
    """Gate 7: Given a Compose-started instance must get the SAME shutdown
    window as our own engine `stop` sweep
    (`partgraph.util.lifecycle.STOP_GRACE_SECONDS`) — without an explicit
    `stop_grace_period`, Compose falls back to its own 10s default, the
    same insufficient window that produced a live SIGKILL (unit exit
    status 137) after a 14h run on this host.
    When we inspect the dgraph service configuration.
    Then it must declare `stop_grace_period` equal to STOP_GRACE_SECONDS
    (imported directly from partgraph.util.lifecycle, never re-declared as
    an independent literal here, so the two pins cannot silently drift
    apart).
    """
    services = compose_config.get("services", {})
    assert services, "No services defined in docker-compose.yml"

    for svc_name, svc in services.items():
        image = svc.get("image", "")
        if "dgraph" in image.lower():
            stop_grace_period = svc.get("stop_grace_period")
            assert stop_grace_period is not None, (
                f"Service '{svc_name}' does not declare 'stop_grace_period'. "
                "Without it, Compose falls back to its own 10s default — the "
                "same insufficient window that produced a live SIGKILL (exit "
                "137) after a 14h run on this host."
            )
            normalized = _normalize_duration_seconds(stop_grace_period)
            assert normalized == float(STOP_GRACE_SECONDS), (
                f"Service '{svc_name}' stop_grace_period={stop_grace_period!r} "
                f"(parsed as {normalized}s) does not match "
                f"STOP_GRACE_SECONDS ({STOP_GRACE_SECONDS}s) — the two "
                "budgets must stay in lockstep."
            )
            return

    pytest.fail(
        "No dgraph service found in docker-compose.yml — cannot verify "
        f"stop_grace_period. Services: {list(services.keys())}"
    )
