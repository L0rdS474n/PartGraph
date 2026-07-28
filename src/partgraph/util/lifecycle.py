"""Stop every PartGraph database lifecycle owner, not just Compose (ADR-0021).

This is a **leaf** module: its top-level imports are the Python standard library
plus :mod:`partgraph.util.container` (engine detection, ADR-0009). It imports
:func:`partgraph.util.health.probe_health` **lazily**, inside :func:`stop_all`,
and it must never import :mod:`partgraph.cli` or any of the embed/query/load
layers — so the CLI and future callers can all import it without a cycle
(mirrors :mod:`partgraph.util.container` / :mod:`partgraph.util.health` /
:mod:`partgraph.util.index_health`). ``lifecycle`` is deliberately **not**
re-exported from ``partgraph/util/__init__.py``, following the same precedent.

Why this exists
---------------
``partgraph db down`` used to be exactly one call: ``<engine> compose -f <file>
down``. Compose only knows the containers **Compose itself created and
labelled**. On this host a SECOND lifecycle owner exists: a quadlet-generated
``systemd --user`` unit (``partgraph-dgraph.service``) built from the same
compose file, declaring the same container name, the same image, the same host
ports and the same named data volume, with ``Restart=on-failure`` and
``WantedBy=default.target``. Compose cannot stop it, so ``db down`` reported
success while a PartGraph database kept running unattended.

Selector policy (locked, ADR-0021)
----------------------------------
Ownership is decided in Python by **exact string comparison** over a full
enumeration — never by handing a pattern to the engine (``ps --filter name=``
is a *regex* on both engines and must never be used as ownership authority):

- **S1** — the container name is EXACTLY :data:`PARTGRAPH_CONTAINER_NAME`.
  -> stopped.
- **S2** — the container mounts the named volume :data:`PARTGRAPH_DATA_VOLUME`
  (exact volume-name equality; never a prefix/suffix/substring match, which
  would wrongly catch e.g. a ``..._backup`` volume). -> stopped.
- **S3** — the container holds one of :data:`PARTGRAPH_WATCHED_PORTS` on the
  host but matches neither S1 nor S2. -> **REPORTED ONLY, NEVER STOPPED.**

Image name and Compose project label are FORBIDDEN as selectors: the compose
project name on the affected host is literally ``docker``, and the ``dgraph/*``
image family is shared with an unrelated cve-graph stack on the same machine.

Safety properties
-----------------
- **Verb surface is ``stop`` only.** This module never runs ``rm``, ``volume
  rm``, ``prune``, ``-v`` or ``--volumes``; the named data volume always
  survives. (Compose's own ``down`` still removes what Compose created.)
- **Stops target the opaque container ID, never the name.** S2 classifies
  independently of the name, so enforcing by name would reopen a TOCTOU window
  between the survivor enumeration and the stop call.
- **Every enumerated string is validated at the boundary.** Container names and
  IDs must match a strict positive allow-list before they are used at all;
  a rejected row is excluded and its raw text never reaches a subprocess argv.
- **Every subprocess call carries a finite, named timeout**, and ``ps`` output
  is bounded by :data:`MAX_PS_OUTPUT_BYTES` before it is decoded.
- **``shell=False`` everywhere**, list argv only.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from partgraph.util.container import engine_command

__all__ = [
    "ENUMERATE_TIMEOUT_S",
    "INSPECT_TIMEOUT_S",
    "MAX_PS_OUTPUT_BYTES",
    "PARTGRAPH_CONTAINER_NAME",
    "PARTGRAPH_DATA_VOLUME",
    "PARTGRAPH_UNIT_NAME",
    "PARTGRAPH_WATCHED_PORTS",
    "STOP_GRACE_SECONDS",
    "STOP_TIMEOUT_S",
    "SYSTEMCTL_TIMEOUT_S",
    "DownResult",
    "Instance",
    "UnitState",
    "find_partgraph_instances",
    "stop_all",
    "unit_state",
]

#: Module logger. Absorbed failures (a systemd unit that refuses to stop, an
#: engine ``stop`` that exits non-zero) are recorded here rather than swallowed
#: silently; every record is single-line, path-free, and carries no
#: engine-derived string. Never a print: all user-facing text for ``db down``
#: is composed in ``partgraph.cli`` from :class:`DownResult`'s fields.
_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frozen selector constants — the exact literals docker/docker-compose.yml
# declares. Never derived at runtime from engine output.
# ---------------------------------------------------------------------------

#: S1 selector: the exact ``container_name`` in docker/docker-compose.yml.
PARTGRAPH_CONTAINER_NAME = "partgraph-dgraph"

#: S2 selector: the named data volume in docker/docker-compose.yml. Matched by
#: exact string equality only.
PARTGRAPH_DATA_VOLUME = "partgraph_dgraph_data"

#: The quadlet/systemd user unit that is PartGraph's second lifecycle owner.
#: A FROZEN constant: it is never built from an engine-returned string, so a
#: poisoned container name can never influence which unit gets targeted.
PARTGRAPH_UNIT_NAME = "partgraph-dgraph.service"

#: S3 selector: PartGraph's own reserved host ports (docker/docker-compose.yml).
PARTGRAPH_WATCHED_PORTS: tuple[int, ...] = (8081, 9081, 8001)

# ---------------------------------------------------------------------------
# Bounded constants — finite timeouts and a finite output ceiling. Extends
# ADR-0007's bounded-constant precedent (and HEALTH_PROBE_TIMEOUT_S) to this
# module's subprocess calls and to untrusted engine OUTPUT.
# ---------------------------------------------------------------------------

#: Watchdog for one ``<engine> ps --all --format json`` enumeration.
ENUMERATE_TIMEOUT_S = 15.0

#: Watchdog for one ``<engine> container inspect`` call.
INSPECT_TIMEOUT_S = 10.0

#: Watchdog for one ``<engine> stop`` call. Deliberately larger than
#: :data:`STOP_GRACE_SECONDS`, so the Python side never aborts before the
#: engine's own grace period has elapsed.
STOP_TIMEOUT_S = 45.0

#: Watchdog for one ``systemctl --user`` call (show or stop).
SYSTEMCTL_TIMEOUT_S = 20.0

#: The engine's OWN ``-t`` grace period, in seconds: how long the container
#: gets to shut down cleanly before the engine escalates. Distinct from the
#: Python-level subprocess watchdog :data:`STOP_TIMEOUT_S` above.
STOP_GRACE_SECONDS = 10

#: Finite ceiling (4 MiB) on how much ``ps`` stdout the parser will even
#: attempt to decode. Output at or beyond this bound is treated as malformed
#: and degrades to an empty result, so a wedged or hostile engine can never
#: make ``db down`` spend unbounded time/memory in :func:`json.loads`.
MAX_PS_OUTPUT_BYTES = 4 * 1024 * 1024

# ---------------------------------------------------------------------------
# Private constants
# ---------------------------------------------------------------------------

#: ``systemctl`` is a hard-coded literal ON PURPOSE, unlike the container
#: engine: there is no interchangeable alternative to detect between, so
#: ADR-0009's detection contract does not apply to it (see ADR-0021).
_SYSTEMCTL = "systemctl"

#: The Docker/podman container-name grammar, used as a POSITIVE allow-list for
#: both names and IDs. Anything failing it is excluded before classification
#: and never reaches a subprocess argv (a deny-list of hostile characters could
#: never be exhaustive).
_IDENTIFIER_GRAMMAR = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

#: Finite ceiling on an accepted container name/ID length.
_MAX_IDENTIFIER_LENGTH = 255

#: Ownership tags carried by :attr:`Instance.owned_by`.
_OWNER_NAME_MATCH = "S1"
_OWNER_VOLUME_MATCH = "S2"
_OWNER_PORT_HOLDER = "S3"

#: Fourth tag: the container's volume-mount status could NOT be determined
#: because ``container inspect`` itself failed. Deliberately distinct from S3:
#: "I could not tell" must never be recorded as "I checked, and it is not
#: ours" (Gate 5 finding A).
_OWNER_UNDETERMINED = "UNKNOWN"

#: Ownership tags whose containers this module may stop. Neither S3 nor
#: UNKNOWN is ever here: we only stop what we positively know is ours.
_STOPPABLE_OWNERS = frozenset({_OWNER_NAME_MATCH, _OWNER_VOLUME_MATCH})

#: Host-port grammar for a Docker-shaped ``Ports`` string. Strict ASCII digits
#: only — ``str.isdigit()`` would also accept non-ASCII digit characters.
_HOST_PORT_GRAMMAR = re.compile(r"^[0-9]+$")

#: The separator Docker puts between the host side and the container side of a
#: published port, e.g. ``0.0.0.0:8081->8080/tcp``.
_DOCKER_PORT_ARROW = "->"

#: Inclusive bounds for an accepted TCP/UDP port number.
_MIN_PORT = 1
_MAX_PORT = 65535

#: Engine ``State`` values that positively mean "not currently running", so
#: there is nothing to stop and nothing to report as a survivor. Deliberately a
#: DENY-list rather than an allow-list of running states: an unrecognized state
#: on an already PartGraph-owned container degrades toward stopping it and, if
#: it then persists, toward a loud non-zero exit — never toward a silent
#: "everything is down" when something may still be serving.
_NOT_RUNNING_STATES = frozenset(
    {"exited", "created", "dead", "removing", "stopped", "stopping", "configured"}
)

#: ``systemctl show`` values.
_LOAD_STATE_NOT_FOUND = "not-found"
_ACTIVE_STATE_ACTIVE = "active"
_DEFAULT_TARGET = "default.target"

#: The ``Mounts[].Type`` value that marks a NAMED volume (as opposed to a bind
#: mount, whose ``Name`` is absent).
_MOUNT_TYPE_VOLUME = "volume"


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Instance:
    """One enumerated container that PartGraph's selector policy classified.

    Attributes:
        id: The engine-assigned, opaque container ID. This is the ONLY value
            ever used as an engine ``stop`` / ``container inspect`` TARGET: S2
            classifies by volume mount independently of the name, so targeting
            a name would reopen a TOCTOU window between the enumeration and the
            stop, whereas a live container ID cannot be reused.
        name: The container's first reported name. DISPLAY ONLY — it is
            validated against a positive allow-list but never used as an argv
            target.
        image: The container's image reference, for display. Never a selector.
        status: The engine-reported state (e.g. ``"running"``, ``"exited"``).
        ports: The host ports this container publishes, de-duplicated.
        owned_by: One of ``"S1"`` (exact name), ``"S2"`` (mounts the named data
            volume), ``"UNKNOWN"`` (the mount status could not be determined
            because ``container inspect`` failed) or ``"S3"`` (POSITIVELY
            confirmed not to mount our volume, but holds a watched host port —
            report-only). A container matching none of the four is never
            represented by an ``Instance`` at all.
        mounts_data_volume: True iff the container is CONFIRMED to mount
            :data:`PARTGRAPH_DATA_VOLUME` by exact name. False covers both
            "confirmed not to" and "could not be determined" — ``owned_by`` is
            the authoritative field for that distinction, and it reads
            ``"UNKNOWN"`` in the latter case.
    """

    id: str
    name: str
    image: str
    status: str
    ports: tuple[int, ...]
    owned_by: str
    mounts_data_volume: bool


@dataclass(frozen=True)
class UnitState:
    """Immutable snapshot of the quadlet ``partgraph-dgraph.service`` unit.

    Attributes:
        present: True iff systemd knows a unit by that name at all (a *failed*
            unit is still present — that is distinct from not-found).
        load_state: systemd's ``LoadState`` (e.g. ``"loaded"``), or None when
            absent/empty.
        active_state: systemd's ``ActiveState`` (e.g. ``"active"``,
            ``"failed"``), or None when absent/empty.
        wanted_by_default: True iff the unit is wanted by ``default.target``
            (and would therefore come back on the next login), False when the
            evidence says otherwise, and **None when undeterminable** — never
            guessed.
    """

    present: bool
    load_state: str | None
    active_state: str | None
    wanted_by_default: bool | None


@dataclass(frozen=True)
class DownResult:
    """Immutable outcome of one full :func:`stop_all` sweep.

    Every tuple carries display NAMES, never container IDs. Unlike
    :class:`~partgraph.util.health.HealthResult` and
    :class:`~partgraph.util.index_health.IndexIntegrityResult`, this DTO
    deliberately carries **no** ``message`` field: every user-facing string
    ``db down`` prints is composed in ``partgraph.cli`` from these structured
    fields, which keeps all of that command's text in one place instead of
    splitting it between the leaf and the CLI.

    Attributes:
        stopped: Names of the S1/S2 instances this sweep stopped and then
            verified gone.
        skipped_foreign_port_holders: Names of S3 containers — they hold one of
            PartGraph's host ports but are not PartGraph's, so they were
            reported and deliberately left running.
        unit_stopped: True iff a ``systemctl --user stop`` was actually issued
            for :data:`PARTGRAPH_UNIT_NAME` and reported success.
        survivors: Names of S1/S2 instances still running after verification.
        still_serving_health: True iff Dgraph's health endpoint still answered
            after the sweep.
        undetermined: Names of instances whose ownership could NOT be
            determined during the FINAL verification pass, because
            ``container inspect`` failed on them there. Populated from that
            pass ONLY: an inspect failure confined to the pre-stop sweep, which
            resolves by the time verification runs, never lands here (the same
            "do not over-fire" absorption phase-1 systemctl failures get). A
            non-empty tuple means the sweep cannot honestly claim success —
            distinct from :attr:`survivors`, which means it positively failed.
    """

    stopped: tuple[str, ...]
    skipped_foreign_port_holders: tuple[str, ...]
    unit_stopped: bool
    survivors: tuple[str, ...]
    still_serving_health: bool
    undetermined: tuple[str, ...]


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run_capture(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run *argv* with a list argv, ``shell=False`` and a bounded *timeout*.

    ``check=False``: every caller inspects ``returncode`` explicitly and decides
    for itself whether the outcome is fatal, absorbed, or merely logged.
    """
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# ps / inspect parsing — every helper degrades, none of them raise
# ---------------------------------------------------------------------------


def _parse_ps_rows(stdout: str) -> tuple[dict[str, Any], ...]:
    """Parse ``ps --all --format json`` output into row dicts.

    The outer envelope is undocumented and differs between engines, so BOTH a
    single JSON array and newline-delimited JSON are accepted. Empty, oversized
    or unparseable output degrades to an empty tuple; a malformed individual
    row degrades to the omission of that row only.
    """
    if not stdout:
        return ()
    if len(stdout.encode("utf-8", errors="replace")) >= MAX_PS_OUTPUT_BYTES:
        _LOGGER.warning(
            "Container enumeration output exceeded the %d-byte bound and was "
            "discarded without being decoded.",
            MAX_PS_OUTPUT_BYTES,
        )
        return ()
    text = stdout.strip()
    if not text:
        return ()
    rows = _parse_json_envelope(text)
    if rows is not None:
        return rows
    return _parse_ndjson_envelope(text)


def _parse_json_envelope(text: str) -> tuple[dict[str, Any], ...] | None:
    """Parse *text* as one JSON document, or return None if it is not one."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if isinstance(parsed, list):
        return tuple(row for row in parsed if isinstance(row, dict))
    if isinstance(parsed, dict):
        return (parsed,)
    return ()


def _parse_ndjson_envelope(text: str) -> tuple[dict[str, Any], ...]:
    """Parse *text* as newline-delimited JSON, skipping unparseable lines."""
    rows: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return tuple(rows)


def _accepted_identifier(value: Any) -> str | None:
    """Return *value* iff it is a string matching the positive allow-list.

    Applied to both container names and container IDs, so no engine-derived
    string that could be read as a flag, a shell metacharacter or an unbounded
    blob ever reaches a subprocess argv or a rendered message.
    """
    if not isinstance(value, str):
        return None
    if not value or len(value) > _MAX_IDENTIFIER_LENGTH:
        return None
    if _IDENTIFIER_GRAMMAR.match(value) is None:
        return None
    return value


def _row_id(row: Mapping[str, Any]) -> str | None:
    """Return the row's container ID, or None if unusable.

    Podman spells the key ``Id``; Docker's ``ps --format json`` spells it ``ID``.
    Both are accepted so ``PARTGRAPH_CONTAINER_ENGINE=docker`` genuinely routes
    through this module and not merely nominally (mirrors the live gate helper
    in tests/integration/test_gate_pr7.py). A row carrying NEITHER key is still
    skipped: there is no target to inspect or stop.
    """
    return _accepted_identifier(row.get("Id") or row.get("ID"))


def _row_name(row: Mapping[str, Any]) -> str | None:
    """Return the row's first container name, or None if unusable.

    Podman reports ``Names`` as a list; Docker's ``ps --format json`` reports it
    as a single comma-joined string. Both are accepted, and the docker-shaped
    string is split BEFORE the name is allow-listed — never after, so a
    separator can never smuggle a second value past validation. An empty list,
    an empty string and a missing key are all still skipped.
    """
    names = row.get("Names")
    if isinstance(names, list) and names:
        return _accepted_identifier(names[0])
    if isinstance(names, str) and names:
        return _accepted_identifier(names.split(",")[0])
    return None


def _row_ports(row: Mapping[str, Any]) -> tuple[int, ...]:
    """Return the de-duplicated host ports the row publishes.

    Both observed engine shapes parse identically: Podman's ``list[dict]``
    (each entry carrying a ``host_port`` int) and Docker's comma-joined string
    (``"0.0.0.0:8081->8080/tcp, 0.0.0.0:9081->9080/tcp"``). Anything else — a
    malformed string, an empty string, a missing key — degrades to no ports
    rather than raising. Ports only ever ADD the report-only S3 tag; they never
    override S1/S2/UNKNOWN, so a degraded parse can never mis-stop anything.
    """
    raw_ports = row.get("Ports")
    if isinstance(raw_ports, list):
        return _sorted_ports(_podman_host_ports(raw_ports))
    if isinstance(raw_ports, str):
        return _sorted_ports(_docker_host_ports(raw_ports))
    return ()


def _podman_host_ports(entries: list[Any]) -> set[int]:
    """Extract host ports from podman's ``list[dict]`` ``Ports`` shape."""
    host_ports: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = entry.get("host_port")
        if isinstance(value, int) and not isinstance(value, bool):
            host_ports.add(value)
    return host_ports


def _docker_host_ports(value: str) -> set[int]:
    """Extract host ports from Docker's comma-joined ``Ports`` string.

    Each comma-separated mapping looks like ``<host_ip>:<host_port>-><container
    _port>/<proto>``; the host IP may be IPv6 (``[::]:8081->8080/tcp``), which
    is why the host port is taken after the LAST ``:``. A segment without the
    ``->`` separator publishes no host port (Docker prints bare ``8080/tcp``
    for a merely-exposed port) and is skipped, as is a host side that is not a
    plain in-range integer — including a published RANGE such as
    ``8081-8083``, which degrades to no port for that segment.
    """
    host_ports: set[int] = set()
    for segment in value.split(","):
        mapping = segment.strip()
        if _DOCKER_PORT_ARROW not in mapping:
            continue
        host_side = mapping.split(_DOCKER_PORT_ARROW, 1)[0]
        candidate = host_side.rsplit(":", 1)[-1].strip()
        if _HOST_PORT_GRAMMAR.match(candidate) is None:
            continue
        port = int(candidate)
        if _MIN_PORT <= port <= _MAX_PORT:
            host_ports.add(port)
    return host_ports


def _sorted_ports(host_ports: set[int]) -> tuple[int, ...]:
    """Return *host_ports* as a deterministic, de-duplicated tuple."""
    return tuple(sorted(host_ports))


def _row_text(row: Mapping[str, Any], key: str) -> str:
    """Return a display-only string field, or ``""`` when absent/non-string."""
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _mounts_data_volume(engine_prefix: list[str], container_id: str) -> bool | None:
    """Report whether *container_id* mounts :data:`PARTGRAPH_DATA_VOLUME`.

    TRI-STATE (Gate 5 finding A): True (confirmed to mount it), False
    (confirmed NOT to), or **None — could not be determined**, because the
    ``inspect`` call itself failed: a timeout, a failure to execute, a non-zero
    exit, an unparseable payload, or a shape carrying no ``Mounts`` at all.

    None is never collapsed into False. Doing so is exactly the false success
    this module exists to prevent: an S2 container is recognised ONLY by this
    call, so a failed inspect silently reclassified as "does not mount our
    volume" makes a still-running PartGraph instance invisible to both the
    sweep and the verification, and `db down` then exits 0 while it runs on.

    Targets the opaque container ID, never a name, so a foreign container's
    name never reaches an ``inspect`` argv.
    """
    argv = [*engine_prefix, "container", "inspect", "--format", "json", container_id]
    try:
        result = _run_capture(argv, timeout=INSPECT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        _LOGGER.warning(
            "Inspecting a container timed out or could not be executed; its "
            "ownership is undetermined."
        )
        return None
    if result.returncode != 0:
        _LOGGER.warning(
            "Inspecting a container failed (engine exit code %d); its ownership "
            "is undetermined.",
            result.returncode,
        )
        return None
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        return None
    if not (isinstance(parsed, list) and parsed and isinstance(parsed[0], dict)):
        return None
    mounts = parsed[0].get("Mounts")
    if not isinstance(mounts, list):
        return None
    return any(
        isinstance(mount, dict)
        and mount.get("Type") == _MOUNT_TYPE_VOLUME
        and mount.get("Name") == PARTGRAPH_DATA_VOLUME
        for mount in mounts
    )


def _classify(
    name: str, *, mounts_volume: bool | None, ports: tuple[int, ...]
) -> str | None:
    """Return the ownership tag for a container, or None if it is not ours.

    Exact string equality only — never a prefix, suffix or substring match, and
    never the image name or a Compose project label. The priority order is
    load-bearing:

    1. name equals :data:`PARTGRAPH_CONTAINER_NAME` -> S1;
    2. confirmed to mount :data:`PARTGRAPH_DATA_VOLUME` -> S2;
    3. mount status UNDETERMINED -> ``UNKNOWN`` — checked BEFORE the port test,
       so a container we could not classify is never confidently downgraded to
       "just a port holder, safe to leave" merely because it happens to hold a
       watched port;
    4. confirmed NOT to mount it, but holds a watched host port -> S3;
    5. otherwise not ours, and never returned at all.
    """
    if name == PARTGRAPH_CONTAINER_NAME:
        return _OWNER_NAME_MATCH
    if mounts_volume:
        return _OWNER_VOLUME_MATCH
    if mounts_volume is None:
        return _OWNER_UNDETERMINED
    if any(port in PARTGRAPH_WATCHED_PORTS for port in ports):
        return _OWNER_PORT_HOLDER
    return None


def _instance_from_row(engine_prefix: list[str], row: Mapping[str, Any]) -> Instance | None:
    """Turn one ``ps`` row into a classified :class:`Instance`, or None.

    None means "skip this row": it is malformed (no usable name, no usable
    container ID), its name failed the allow-list, or it matched no selector.
    """
    name = _row_name(row)
    container_id = _row_id(row)
    if name is None or container_id is None:
        return None
    mounts_volume = _mounts_data_volume(engine_prefix, container_id)
    ports = _row_ports(row)
    owned_by = _classify(name, mounts_volume=mounts_volume, ports=ports)
    if owned_by is None:
        return None
    return Instance(
        id=container_id,
        name=name,
        image=_row_text(row, "Image"),
        status=_row_text(row, "State"),
        ports=ports,
        owned_by=owned_by,
        # An undetermined mount status (None) surfaces as False here and as
        # owned_by == "UNKNOWN" above; owned_by is the authoritative field.
        mounts_data_volume=mounts_volume is True,
    )


def _resolve_which(
    which: Callable[[str], str | None] | None,
) -> Callable[[str], str | None]:
    """Return the PATH-lookup callable to use.

    Resolved at CALL time (never captured as a parameter default) so a test that
    patches ``shutil.which`` globally is honoured, exactly like production code
    that relies on the real lookup.
    """
    return which if which is not None else shutil.which


def _resolve_engine_prefix(
    engine_prefix: list[str] | None,
    *,
    which: Callable[[str], str | None],
    environ: Mapping[str, str] | None,
) -> list[str]:
    """Return the engine argv prefix, detecting it when not supplied.

    Detection always routes through :func:`partgraph.util.container.engine_command`
    (ADR-0009), so ``PARTGRAPH_CONTAINER_ENGINE`` is honoured identically to
    ``db up`` / ``db down`` and no engine name is ever hard-coded here.
    """
    if engine_prefix is not None:
        return list(engine_prefix)
    return engine_command(which=which, environ=environ)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_partgraph_instances(
    *,
    engine_prefix: list[str] | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Instance, ...]:
    """Enumerate every container this host runs and return PartGraph's own.

    READ-ONLY. Runs ``<engine> ps --all --format json`` (never ``--filter``:
    that flag is a *regex* on both engines and must never be ownership
    authority) and decides ownership in Python, then runs ``<engine> container
    inspect --format json <id>`` per usable row to resolve the S2 volume test.

    Args:
        engine_prefix: Engine argv prefix; detected via
            :func:`~partgraph.util.container.engine_command` when None.
        which: PATH-lookup callable used for detection; defaults to
            :func:`shutil.which`, resolved at call time.
        environ: Environment mapping used for detection; defaults to
            :data:`os.environ` inside ``engine_command``.

    Returns:
        Only containers classified S1, S2 or S3 — in ``ps`` order. Anything
        else (every cve-graph container, for instance) is absent entirely, never
        present under some other tag.

    Raises:
        subprocess.SubprocessError: If the enumeration call itself exceeds
            :data:`ENUMERATE_TIMEOUT_S` (``TimeoutExpired``) or the child
            process fails in a way the subprocess module reports.
        OSError: If the engine binary cannot be executed at all — e.g. a
            ``FileNotFoundError`` raised in the narrow window between
            ``engine_command()``'s PATH check and the actual ``exec``, or a
            permission error on the binary.

    Neither is caught here, and that is DELIBERATE: an enumeration that never
    happened must never be degraded to an empty tuple, because an empty tuple
    reads as "nothing PartGraph owns is running" — the exact false success this
    module exists to prevent. Every EXPECTED outcome (empty, unparseable,
    oversized, or partially-malformed output; a non-zero ``ps`` exit) is
    degraded to a result inside this function instead; only a failure to run
    the enumeration at all escapes. ``db down`` turns both exception types into
    one clean, path-free error line and a non-zero exit.
    """
    lookup = _resolve_which(which)
    prefix = _resolve_engine_prefix(engine_prefix, which=lookup, environ=environ)

    result = _run_capture(
        [*prefix, "ps", "--all", "--format", "json"], timeout=ENUMERATE_TIMEOUT_S
    )
    if result.returncode != 0:
        _LOGGER.warning(
            "Container enumeration failed (engine exit code %d); no instance "
            "could be classified.",
            result.returncode,
        )
        return ()

    instances = [
        instance
        for instance in (_instance_from_row(prefix, row) for row in _parse_ps_rows(result.stdout))
        if instance is not None
    ]
    return tuple(instances)


def unit_state(*, which: Callable[[str], str | None] | None = None) -> UnitState:
    """Report the state of the quadlet unit :data:`PARTGRAPH_UNIT_NAME`.

    READ-ONLY. Returns ``UnitState(present=False, ...)`` **without invoking any
    subprocess at all** when ``systemctl`` is not on PATH, and likewise when the
    query itself could not be executed — an undeterminable unit is reported as
    absent rather than guessed at, and ``wanted_by_default`` stays None.

    Args:
        which: PATH-lookup callable; defaults to :func:`shutil.which`, resolved
            at call time.
    """
    lookup = _resolve_which(which)
    if lookup(_SYSTEMCTL) is None:
        return UnitState(
            present=False, load_state=None, active_state=None, wanted_by_default=None
        )

    argv = [
        _SYSTEMCTL,
        "--user",
        "show",
        PARTGRAPH_UNIT_NAME,
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=UnitFileState",
        "--property=WantedBy",
    ]
    try:
        result = _run_capture(argv, timeout=SYSTEMCTL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        _LOGGER.warning(
            "Querying the PartGraph systemd user unit timed out or could not be "
            "executed; treating it as absent."
        )
        return UnitState(
            present=False, load_state=None, active_state=None, wanted_by_default=None
        )

    properties = _parse_property_lines(result.stdout)
    load_state = _property_or_none(properties, "LoadState")
    wanted_by = properties.get("WantedBy") or ""
    return UnitState(
        present=load_state is not None and load_state != _LOAD_STATE_NOT_FOUND,
        load_state=load_state,
        active_state=_property_or_none(properties, "ActiveState"),
        wanted_by_default=(_DEFAULT_TARGET in wanted_by.split()) if wanted_by else None,
    )


def _parse_property_lines(stdout: str) -> dict[str, str]:
    """Parse ``systemctl show`` ``Key=Value`` lines into a mapping."""
    properties: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        key, separator, value = raw_line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()
    return properties


def _property_or_none(properties: Mapping[str, str], key: str) -> str | None:
    """Return a systemd property value, or None when absent or empty."""
    value = properties.get(key)
    return value or None


def _stop_unit_if_active(*, which: Callable[[str], str | None], dry_run: bool) -> bool:
    """Phase 1: stop the quadlet unit when it is present AND active.

    Returns True only when a stop was issued and reported success. A failure
    here is ABSORBED (logged, not raised) on purpose: a systemd unit that
    refuses to stop must not prevent the Compose and engine-level sweeps from
    running — they are the phases that actually reach the container.
    """
    state = unit_state(which=which)
    if not state.present or state.active_state != _ACTIVE_STATE_ACTIVE:
        return False
    if dry_run:
        return False

    try:
        result = _run_capture(
            [_SYSTEMCTL, "--user", "stop", PARTGRAPH_UNIT_NAME],
            timeout=SYSTEMCTL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        _LOGGER.warning(
            "Stopping the PartGraph systemd user unit timed out or could not be "
            "executed; continuing with the Compose and engine sweeps."
        )
        return False
    if result.returncode != 0:
        _LOGGER.warning(
            "Stopping the PartGraph systemd user unit failed (exit code %d); "
            "continuing with the Compose and engine sweeps.",
            result.returncode,
        )
        return False
    return True


def _is_stoppable(instance: Instance) -> bool:
    """Return True iff *instance* is PartGraph's own AND may still be running."""
    if instance.owned_by not in _STOPPABLE_OWNERS:
        return False
    return instance.status.strip().casefold() not in _NOT_RUNNING_STATES


def _stop_instances(engine_prefix: list[str], targets: tuple[Instance, ...]) -> set[str]:
    """Phase 3: ``stop`` each target BY CONTAINER ID; return the stopped IDs.

    A per-container failure is logged and the sweep continues: the verification
    re-enumeration is what ultimately decides whether anything survived, so one
    stubborn container never hides another.
    """
    stopped_ids: set[str] = set()
    for instance in targets:
        argv = [*engine_prefix, "stop", "-t", str(STOP_GRACE_SECONDS), instance.id]
        try:
            result = _run_capture(argv, timeout=STOP_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            _LOGGER.warning(
                "Stopping a PartGraph container timed out or could not be executed; "
                "the verification pass will report it as a survivor."
            )
            continue
        if result.returncode != 0:
            _LOGGER.warning(
                "The container engine failed to stop a PartGraph container "
                "(exit code %d).",
                result.returncode,
            )
            continue
        stopped_ids.add(instance.id)
    return stopped_ids


def _still_serving_health(probe_health: Callable[[], Any] | None) -> bool:
    """Return True iff Dgraph's health endpoint still answers after the sweep.

    ``probe_health`` is the injected seam. It defaults to a LAZY import of
    :func:`partgraph.util.health.probe_health`, so this leaf never pulls the
    HTTP stack in merely by being imported, and so ``partgraph.cli`` can thread
    its OWN module-level ``probe_health`` reference through without this module
    importing anything from the CLI.
    """
    probe = probe_health
    if probe is None:
        from partgraph.util.health import (  # noqa: PLC0415 — deliberate lazy import
            probe_health as default_probe,
        )

        probe = default_probe
    return bool(getattr(probe(), "healthy", False))


def stop_all(  # noqa: PLR0913 — one keyword-only seam per injected dependency.
    *,
    engine_prefix: list[str] | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
    compose_down: Callable[[], None],
    probe_health: Callable[[], Any] | None = None,
    dry_run: bool = False,
) -> DownResult:
    """Stop every PartGraph lifecycle owner, in a load-bearing phase order.

    Phases:
      1. Query :func:`unit_state`; if the quadlet unit is present and active,
         ``systemctl --user stop`` it. A failure here is ABSORBED.
      2. Call *compose_down*. Unlike phase 1, an exception it raises propagates
         out of this function completely UNMODIFIED and short-circuits phases 3
         and 4 — a deliberate asymmetry: a failed systemd-unit stop is
         survivable, a failed Compose invocation is not.
      3. Enumerate what survived and ``stop`` every S1/S2 instance BY CONTAINER
         ID (never by name).
      4. Re-enumerate to verify, then probe the health endpoint.

    Args:
        engine_prefix: Engine argv prefix; detected when None.
        which: PATH-lookup callable; defaults to :func:`shutil.which`.
        environ: Environment mapping used for engine detection.
        compose_down: REQUIRED, keyword-only, no default. Every caller must
            decide EXPLICITLY whether and how Compose is invoked, rather than
            silently no-op-ing phase 2 by inheriting a permissive default.
        probe_health: Injected health seam; defaults to a lazy import of
            :func:`partgraph.util.health.probe_health`.
        dry_run: When True, every MUTATING step (the unit stop, *compose_down*,
            the engine stop sweep) is skipped; the READ-ONLY steps — the unit
            query, both enumerations and the health probe — still run, so the
            caller can report exactly what would have been stopped and whether
            the database is answering. Every field of the returned
            :class:`DownResult` is therefore populated by a real observation
            under ``dry_run`` too; none is a placeholder.

    Returns:
        A frozen :class:`DownResult` carrying display names only.
    """
    lookup = _resolve_which(which)
    prefix = _resolve_engine_prefix(engine_prefix, which=lookup, environ=environ)

    unit_stopped = _stop_unit_if_active(which=lookup, dry_run=dry_run)

    if not dry_run:
        compose_down()

    swept = find_partgraph_instances(engine_prefix=prefix, which=lookup, environ=environ)
    port_holders = tuple(
        instance.name for instance in swept if instance.owned_by == _OWNER_PORT_HOLDER
    )
    targets = tuple(instance for instance in swept if _is_stoppable(instance))
    stopped_ids = set() if dry_run else _stop_instances(prefix, targets)

    remaining = find_partgraph_instances(engine_prefix=prefix, which=lookup, environ=environ)
    survivor_ids = {instance.id for instance in remaining if _is_stoppable(instance)}
    survivors = tuple(
        instance.name for instance in remaining if instance.id in survivor_ids
    )
    # Populated from THIS pass only. An inspect failure during the pre-stop
    # sweep that resolves before verification is absorbed, exactly like a
    # phase-1 systemctl failure: only the verification pass decides the verdict.
    undetermined = tuple(
        instance.name
        for instance in remaining
        if instance.owned_by == _OWNER_UNDETERMINED
    )
    stopped = tuple(
        instance.name
        for instance in targets
        if instance.id in stopped_ids and instance.id not in survivor_ids
    )

    return DownResult(
        stopped=stopped,
        skipped_foreign_port_holders=port_holders,
        unit_stopped=unit_stopped,
        survivors=survivors,
        still_serving_health=_still_serving_health(probe_health),
        undetermined=undetermined,
    )
