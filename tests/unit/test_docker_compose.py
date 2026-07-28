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
- init: true is declared, and only WITH it is stop_grace_period meaningful
  for this image (Gate 8 — see below)
- restart: "no" (PR-B2, ADR-0022 Section 7, AC B-11 — see below)

PR-B2 addition (AC B-11): `restart` changes from `unless-stopped` to `"no"`.
Under rootless podman, `unless-stopped` never actually restarts the
container across a HOST reboot anyway (podman has no boot-time service that
would revive it — only the SEPARATE quadlet/systemd unit ADR-0021/ADR-0022
already document does that), so the value was advertising a lifecycle
guarantee Compose never actually provided on this host. PR-B2 also gives
every DB-touching `partgraph` command its OWN lazy-start path
(`ensure_running()`), which makes even an in-session engine-level restart
unnecessary: the next command that needs the database starts it itself. No
EXISTING test in this file pinned the previous `unless-stopped` value before
this addition (confirmed by re-reading this file end to end), so this is a
new pin, not a change to one — the actual `docker/docker-compose.yml` edit
is out of scope for this test-only change and is expected to leave this new
test RED until it lands.

Gate 7 addition: a live 14h-run journal entry first surfaced this
(`StopSignal SIGTERM failed to stop container partgraph-dgraph in 10
seconds, resorting to SIGKILL`, unit exit status 137). Without an explicit
`stop_grace_period:`, a Compose-started instance inherits Compose's own 10s
default (the same default a bare `docker/podman stop` without an explicit
`-t` uses), so it needs the SAME budget
`partgraph.util.lifecycle.STOP_GRACE_SECONDS` (our own engine `stop`
sweep's budget) already pins. `STOP_GRACE_SECONDS` is imported directly
(never re-declared as a second literal) so the two pins can never silently
drift apart — a change to one without the other fails this test.

Gate 8 CORRECTION: the original "10s is insufficient because Dgraph needs
more time to flush under load" story was DISPROVEN by a direct
re-measurement — a container up for barely one minute, essentially nothing
to flush, ALSO timed out on `podman stop -t 60` and was SIGKILLed (60.2s,
exit 137). The real, structural cause: dgraph/standalone:v25.3.4's
`/run.sh` runs `dgraph alpha` in the FOREGROUND under bash as PID 1
(upstream's own comment: `# TODO properly handle SIGTERM for all three
processes`), and bash defers delivering a signal to a still-running
foreground command — SIGTERM is NEVER forwarded to `dgraph alpha`,
independent of uptime or load. So `stop_grace_period` ALONE, without
`init: true`, is close to useless for this image — pinned below,
separately from the raw value match, and pinned TOGETHER.

HONESTY BOUNDARY: this ONLY covers Compose-started instances, and even
then only once `init: true` is set (see above). It does NOT reach the
quadlet/systemd path — that unit's `ExecStop=podman rm -v -f` uses
whatever stop-timeout was baked in at quadlet-generation time, independent
of this file, and remains PR-B1 territory.
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
    """Gate 7 (corrected under Gate 8): Given a Compose-started instance
    must get the SAME shutdown budget as our own engine `stop` sweep
    (`partgraph.util.lifecycle.STOP_GRACE_SECONDS`) — without an explicit
    `stop_grace_period`, Compose falls back to its own 10s default, the
    same default that first surfaced this via a live SIGKILL (unit exit
    status 137) after a 14h run. NOTE: raising this value ALONE does not
    make shutdown graceful for this image — see
    `test_dgraph_service_declares_init_true_so_sigterm_can_reach_dgraph`
    below for why; this test only pins that the NUMBER matches, not that
    the number is sufficient by itself.
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
                "Without it, Compose falls back to its own 10s default."
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


# ---------------------------------------------------------------------------
# Gate 8 — init: true is required for stop_grace_period to mean anything on
# this image. Live-verified on this host: WITHOUT init, `podman stop -t 60`
# on a container up for barely a minute (essentially nothing to flush) still
# timed out and SIGKILLed (60.2s, exit 137) — disproving the original
# "Dgraph needs more time to flush under load" hypothesis outright. WITH
# `init: true`, the SAME stop completed in 0.2s with a genuine SIGTERM exit
# (143). The experimental compose edit used to measure this was reverted;
# this test pins the fix as a requirement for the pipeline to apply, not as
# a record of a host change.
# ---------------------------------------------------------------------------

def test_dgraph_service_declares_init_true_so_sigterm_can_reach_dgraph(
    compose_config: dict,
) -> None:
    """Gate 8: WHY this flag exists — recorded here so a future reader who
    does not know the reason does not delete it as noise.

    dgraph/standalone:v25.3.4 has no ENTRYPOINT and no STOPSIGNAL; its
    CMD is `/run.sh`, which reads (verbatim, upstream's OWN comment):

        # TODO properly handle SIGTERM for all three processes.
        dgraph zero &
        dgraph alpha

    PID 1 is bash running that script, with `dgraph alpha` as bash's
    FOREGROUND command. Bash defers delivering a received signal to its
    own handling until the foreground command exits — so a SIGTERM sent to
    PID 1 is NEVER forwarded to `dgraph alpha` at all, regardless of grace
    period or how long the container has been running. This is why simply
    raising STOP_GRACE_SECONDS (Gate 7) did not fix graceful shutdown: a
    container up for barely a minute, with essentially nothing to flush,
    still failed `podman stop -t 60` and was SIGKILLed (60.2s, exit 137,
    live-measured on this host) — disproving "needs more time to flush"
    as the cause.

    `init: true` inserts a real init process (podman's `podman-init`/tini)
    as PID 1 instead of bash. An init process DOES forward SIGTERM to its
    child, so `dgraph alpha` actually receives it. With `init: true` set,
    the SAME stop completed in 0.2s with exit code 143 (a genuine SIGTERM
    exit, not a SIGKILL) — live-measured on this host. No stop_grace_period
    value can substitute for this: the signal must physically reach the
    process first.

    When we inspect the dgraph service configuration.
    Then it must declare `init: true`.
    """
    services = compose_config.get("services", {})
    assert services, "No services defined in docker-compose.yml"

    for svc_name, svc in services.items():
        image = svc.get("image", "")
        if "dgraph" in image.lower():
            assert svc.get("init") is True, (
                f"Service '{svc_name}' does not declare 'init: true'. "
                "Without an init process as PID 1, SIGTERM never reaches "
                "`dgraph alpha` — bash (dgraph/standalone's own PID 1 via "
                "/run.sh) defers signal delivery until its foreground "
                "command exits, and upstream's own /run.sh carries the "
                "acknowledged TODO 'properly handle SIGTERM for all three "
                "processes'. No stop_grace_period value can compensate: "
                "the container will always be SIGKILLed without this."
            )
            return

    pytest.fail(
        "No dgraph service found in docker-compose.yml — cannot verify "
        f"'init: true'. Services: {list(services.keys())}"
    )


def test_stop_grace_period_and_init_true_are_declared_together(
    compose_config: dict,
) -> None:
    """Gate 8: Given a `stop_grace_period` on this image is close to
    useless without `init: true` alongside it (SIGTERM never reaches
    `dgraph alpha` otherwise — see the previous test's docstring for the
    full mechanism), the two configuration keys are not independent
    concerns; they are one fix with two parts.
    When we inspect the dgraph service configuration.
    Then `init` must be truthy WHENEVER `stop_grace_period` is declared —
    catches a future regression where one is bumped/removed without the
    other (e.g. someone raises `stop_grace_period` further, or removes
    `init: true` during an unrelated edit, without realising the budget is
    then meaningless again).
    """
    services = compose_config.get("services", {})
    assert services, "No services defined in docker-compose.yml"

    for svc_name, svc in services.items():
        image = svc.get("image", "")
        if "dgraph" in image.lower():
            if svc.get("stop_grace_period") is not None:
                assert svc.get("init") is True, (
                    f"Service '{svc_name}' declares 'stop_grace_period' "
                    "without 'init: true'. For dgraph/standalone, a grace "
                    "period alone cannot produce a graceful shutdown — "
                    "SIGTERM never reaches `dgraph alpha` without an init "
                    "process forwarding it. The two belong together."
                )
            return

    pytest.fail(
        "No dgraph service found in docker-compose.yml — cannot verify the "
        f"stop_grace_period/init relationship. Services: {list(services.keys())}"
    )


# ---------------------------------------------------------------------------
# PR-B2 (ADR-0022 Section 7, AC B-11) — restart: "no". Rootless podman gives
# `unless-stopped` no boot-time reviver on this host at all (that job belongs
# to the SEPARATE quadlet/systemd unit ADR-0021/ADR-0022 already document),
# so the previous value advertised a lifecycle guarantee Compose does not
# provide here; every DB-touching command now starts the database itself via
# `ensure_running()` when it is needed.
# ---------------------------------------------------------------------------

def test_dgraph_service_restart_policy_is_no(compose_config: dict) -> None:
    """AC B-11: Given rootless podman gives `restart: unless-stopped` no
    boot-time reviver on this host, and every DB-touching `partgraph`
    command now lazily starts the database itself (ADR-0022 Section 7).
    When we inspect the dgraph service configuration.
    Then `restart` is declared EXACTLY as the string `"no"` — never the
    bare YAML value `no` (which PyYAML would otherwise parse as the
    boolean `False`, a genuinely different, incorrect Compose value; the
    Compose file itself must quote it), and never `unless-stopped`,
    `always`, or `on-failure`.
    """
    services = compose_config.get("services", {})
    assert services, "No services defined in docker-compose.yml"

    for svc_name, svc in services.items():
        image = svc.get("image", "")
        if "dgraph" in image.lower():
            restart = svc.get("restart")
            assert restart == "no", (
                f"Service '{svc_name}' restart={restart!r}; expected the "
                "STRING \"no\" (quoted in the YAML source, so PyYAML parses "
                "it as the string 'no', never the boolean False)."
            )
            return

    pytest.fail(
        "No dgraph service found in docker-compose.yml — cannot verify "
        f"restart policy. Services: {list(services.keys())}"
    )


def test_dgraph_service_restart_policy_is_quoted_in_the_raw_yaml_source(
    compose_raw_text: str,
) -> None:
    """AC B-11 [belt-and-suspenders]: Given an UNQUOTED `restart: no` in
    YAML source parses as the boolean `False`, not the string `"no"` —
    `docker compose config`'s own canonical-form output (used by
    `compose_config` when a container engine is on PATH) would silently
    paper over that mistake by re-serialising it as `"no"` regardless, so
    the raw source text itself must be checked directly, independent of
    which loader path `compose_config` took on this run.
    When docker/docker-compose.yml's raw text is scanned.
    Then a `restart:` line for the dgraph service is present and its value
    is the QUOTED string `"no"` (single or double quotes), never the bare,
    boolean-parsed token `no`.
    """
    match = re.search(r'^\s*restart:\s*(\S.*)$', compose_raw_text, flags=re.MULTILINE)
    assert match, "docker-compose.yml declares no 'restart:' key at all."
    value = match.group(1).strip()
    assert value in ('"no"', "'no'"), (
        f"docker-compose.yml's restart value must be the QUOTED string "
        f'"no" (never the bare YAML token `no`, which parses as the '
        f"boolean False): got {value!r}"
    )


# ---------------------------------------------------------------------------
# [Docker-parity investigation, documentation honesty] The `restart: "no"`
# comment's reasoning is currently PODMAN-ONLY and reads as inapplicable on a
# genuine Docker host.
#
# This is NOT a runtime/behavioural check — `restart: "no"` is plain YAML and
# applies identically to whichever engine reads it, which is already pinned
# above. It is a DOCUMENTATION-HONESTY check: the comment immediately above
# `restart: "no"` (as of this test being written) justifies the choice with
# EXACTLY one reason — "under rootless podman nothing revives a container at
# boot ... What actually restarted the database was the separate quadlet
# unit" — and ADR-0022 §7f repeats the identical, podman-only framing. Read on
# its own by someone running Docker rather than podman, that reasoning states
# a fact about podman that is simply true and, taken at face value, implies
# `restart: "no"` is a podman-specific workaround that does not concern them.
#
# It is not a workaround for podman alone. On a real Docker host — HERMETIC
# CLAIM, not measured on this host (there is no `dockerd` here; see this
# file's own `_load_compose_yaml` fallback and PartGraph's central Docker
# constraint, documented in tests/unit/test_container.py) — `dockerd` is
# commonly itself an ENABLED systemd (or init) service, so `restart:
# unless-stopped` genuinely DOES revive the container at every host boot,
# with NO quadlet or second lifecycle owner involved at all. That is a
# DIFFERENT mechanism producing the SAME "the database is always running"
# complaint this entire lifecycle body of work exists to fix (ADR-0021,
# ADR-0022) — so `restart: "no"` is independently necessary for a Docker user
# too, not merely inherited collateral from a podman-specific fix. A
# Docker-only reader who trusts the comment as written has no reason to
# believe reverting to `unless-stopped` would reintroduce the exact problem
# this repository's history is built around solving.
# ---------------------------------------------------------------------------


def test_restart_no_comment_also_explains_the_docker_daemon_case_not_only_podman(
    compose_raw_text: str,
) -> None:
    """[Docker-parity, documentation honesty — see section note above] Given
    the comment block preceding `restart: "no"` in docker/docker-compose.yml.
    When that comment (from its "Quoted on purpose" opening line through the
    `restart: "no"` line itself) is scanned.
    Then it must ALSO name Docker's own daemon-level restart mechanism — the
    word "Docker" together with "daemon" or "boot", in that same window — not
    only rootless podman's lack of a boot-time reviver, so a Docker-only
    reader is told the value is necessary for their engine too, and why,
    rather than reading a reason that appears to be someone else's problem.

    Currently RED: the comment (docker/docker-compose.yml, the block ending
    at `restart: "no"`) mentions "rootless podman" and the quadlet unit only;
    it never once mentions Docker or a real daemon's own restart behaviour.
    ADR-0022 §7f repeats the identical gap in prose form (out of scope for
    this test — no test in this repository pins ADR markdown content
    directly; only docs/db-lifecycle.md, the operator runbook, is
    content-pinned, by tests/unit/test_db_lifecycle_docs.py).
    """
    restart_match = re.search(r'restart:\s*"no"', compose_raw_text)
    assert restart_match, 'docker-compose.yml declares no restart: "no" line to anchor this check on.'
    anchor = compose_raw_text.find("Quoted on purpose")
    assert anchor != -1 and anchor < restart_match.start(), (
        "expected the 'Quoted on purpose' comment to precede restart: \"no\" "
        "in docker/docker-compose.yml; the comment this test scans may have "
        "moved or been reworded."
    )
    window = compose_raw_text[anchor:restart_match.start()]
    low = window.lower()
    mentions_docker = "docker" in low
    mentions_daemon_or_boot_mechanism = "daemon" in low or "boot" in low
    assert mentions_docker and mentions_daemon_or_boot_mechanism, (
        "the comment preceding restart: \"no\" must explain why the value is "
        "ALSO necessary on a genuine Docker daemon (which commonly restarts "
        "containers at boot via its own enabled systemd/init service), not "
        "only for rootless podman's lack of a boot-time reviver — otherwise "
        "a Docker-only reader is given a reason that reads as inapplicable "
        f"to their engine. Window scanned:\n{window!r}"
    )
