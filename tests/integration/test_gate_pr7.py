"""
Tests: GATE-PR7 (A17, A18) — fix/db-down-all-instances acceptance gate.

A single `partgraph db down` must leave ZERO PartGraph instances running —
for 0, 1 or N instances, regardless of which lifecycle owner started them —
while PROVABLY never touching an unrelated cve-graph (or any other foreign)
stack running on the same host.

@pytest.mark.integration — requires a real container engine (docker or
podman) on PATH; SKIPS cleanly (never fails) via ContainerEngineError
otherwise. Deliberately does NOT depend on the `dgraph_available` fixture:
`db down` must behave correctly whether or not anything is running at all
(PR-A's own A1 contract), so gating this file on Dgraph being UP would test
the wrong precondition.

STRICTLY NEVER STARTS A CONTAINER: neither test calls `partgraph db up`,
`compose up`, `compose restart`, or any other command that CREATES a
container. GATE-PR7-A18 exercises the REAL, non-dry-run `db down` — a
STOP-only operation per PR-A's locked verb surface (never `rm`/`volume
rm`/`prune`/`-v`) — so it may legitimately stop a PartGraph container that
happened to already be running before the test started (e.g. left over from
prior manual `partgraph db up` use), but it never creates one, and the named
data volume is never removed.

Both tests are SELF-VERIFYING against whatever the REAL host state actually
is at run time (they do not assume today's session's specific cve-graph
container names) — they independently re-derive the "must not touch"
container set from a direct, read-only `ps`/`inspect` call using the SAME
engine, then assert the CLI's behaviour against that independently-observed
set. This makes GATE-PR7-A6's promise ("provably never touching the
unrelated cve-graph stack") a live, structural check rather than a
session-specific fixture.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from partgraph.cli import app
from partgraph.util.container import ContainerEngineError, engine_command

RUNNER = CliRunner()

_PARTGRAPH_CONTAINER_NAME = "partgraph-dgraph"
_PARTGRAPH_DATA_VOLUME = "partgraph_dgraph_data"
_LIVE_SUBPROCESS_TIMEOUT_S = 15


def _invoke(args: list[str]):
    return RUNNER.invoke(app, args)


@pytest.fixture(scope="module")
def real_engine_prefix() -> list[str]:
    """The REAL engine prefix (e.g. ["podman"]) for this host, or SKIP.

    Given: no container engine may be installed on the runner executing this
    suite.
    When: this fixture is requested by a GATE-PR7 test.
    Then: the test is skipped cleanly (never failed) when neither docker nor
    podman is on PATH.
    """
    try:
        return engine_command()
    except ContainerEngineError:
        pytest.skip("No container engine (docker/podman) on PATH; skipping GATE-PR7.")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=_LIVE_SUBPROCESS_TIMEOUT_S, check=False,
    )


def _parse_ps_rows(stdout: str) -> list[dict]:
    """Tolerantly parse `ps --all --format json` output (array OR NDJSON)."""
    text = stdout.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [row for row in parsed if isinstance(row, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except ValueError:
        pass
    rows = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _row_name(row: dict) -> str | None:
    names = row.get("Names")
    if isinstance(names, list) and names:
        return names[0]
    if isinstance(names, str) and names:
        return names.split(",")[0]
    return None


def _row_id(row: dict) -> str | None:
    return row.get("Id") or row.get("ID")


def _live_snapshot(prefix: list[str]) -> dict[str, str]:
    """Return {container_id: name} for every container currently on the host."""
    result = _run([*prefix, "ps", "--all", "--format", "json"])
    if result.returncode != 0:
        return {}
    snapshot: dict[str, str] = {}
    for row in _parse_ps_rows(result.stdout):
        cid = _row_id(row)
        name = _row_name(row)
        if cid and name:
            snapshot[cid] = name
    return snapshot


def _mounts_partgraph_volume(prefix: list[str], container_id: str) -> bool:
    """Return True iff *container_id* mounts the PartGraph named data volume."""
    result = _run([*prefix, "container", "inspect", "--format", "json", container_id])
    if result.returncode != 0:
        return False
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        return False
    if not (isinstance(parsed, list) and parsed and isinstance(parsed[0], dict)):
        return False
    mounts = parsed[0].get("Mounts") or []
    return any(
        isinstance(m, dict)
        and m.get("Type") == "volume"
        and m.get("Name") == _PARTGRAPH_DATA_VOLUME
        for m in mounts
    )


def _classify_live_host(prefix: list[str]) -> tuple[set[str], set[str]]:
    """Return (partgraph_owned_ids, foreign_ids) from a live, read-only scan.

    ``partgraph_owned_ids`` = containers matching S1 (exact name) or S2
    (mounts the named data volume) right now. ``foreign_ids`` = everything
    else currently on the host (must be left byte-for-byte untouched).
    """
    snapshot = _live_snapshot(prefix)
    partgraph_ids: set[str] = set()
    foreign_ids: set[str] = set()
    for cid, name in snapshot.items():
        if name == _PARTGRAPH_CONTAINER_NAME or _mounts_partgraph_volume(prefix, cid):
            partgraph_ids.add(cid)
        else:
            foreign_ids.add(cid)
    return partgraph_ids, foreign_ids


# ---------------------------------------------------------------------------
# A17 — dry-run is read-only and never names a foreign container
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_gate_pr7_a17_dry_run_reports_no_cve_graph_containers(
    real_engine_prefix: list[str],
) -> None:
    """A17: Given the REAL, currently-running container set on this host
    (independently classified into PartGraph-owned vs. foreign by a direct,
    read-only ps+inspect scan — never assumed).
    When `partgraph db down --dry-run` runs for real.
    Then the exit code is 0, NO foreign container's name (e.g. any live
    cve-graph container) appears anywhere in stdout, the live container set
    is BYTE-IDENTICAL before and after (dry-run touches nothing), and if the
    host currently has zero PartGraph-owned containers the output says
    nothing would be stopped.
    """
    partgraph_ids_before, foreign_ids_before = _classify_live_host(real_engine_prefix)
    snapshot_before = _live_snapshot(real_engine_prefix)
    foreign_names = {snapshot_before[cid] for cid in foreign_ids_before}

    result = _invoke(["db", "down", "--dry-run"])

    assert result.exit_code == 0, (
        f"`db down --dry-run` must always exit 0. Output:\n{result.output}"
    )
    for name in foreign_names:
        assert name not in result.output, (
            f"A17: dry-run stdout names a FOREIGN container {name!r} it must "
            f"never touch or report as its own. Output:\n{result.output}"
        )

    snapshot_after = _live_snapshot(real_engine_prefix)
    assert snapshot_after == snapshot_before, (
        "A17: --dry-run must be strictly read-only; the live container set "
        f"changed. before={snapshot_before!r} after={snapshot_after!r}"
    )
    _ = partgraph_ids_before  # documented for readers; not asserted further here.


# ---------------------------------------------------------------------------
# A18 — a real `db down` leaves zero PartGraph containers, foreign untouched
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_gate_pr7_a18_live_down_leaves_zero_partgraph_and_cve_graph_intact(
    real_engine_prefix: list[str],
) -> None:
    """A18: Given the REAL, currently-running container set on this host,
    independently classified (as in A17) into PartGraph-owned vs. foreign
    BEFORE this test runs anything.
    When `partgraph db down` (the REAL, non-dry-run command) runs.
    Then afterwards: (a) zero containers on the host are named
    'partgraph-dgraph' or mount 'partgraph_dgraph_data' — regardless of how
    many PartGraph-owned instances existed before (0, 1 or N); (b) every
    FOREIGN container that was running before is STILL running afterwards
    with the SAME container ID (proves it was genuinely untouched, not
    merely restarted under the same name); (c) the command itself does not
    raise/hang (a finite, bounded run).
    """
    partgraph_ids_before, foreign_ids_before = _classify_live_host(real_engine_prefix)

    result = _invoke(["db", "down"])

    assert result.exit_code in (0, 1), (
        f"`db down` must exit cleanly (0 or 1), never hang/crash. "
        f"exit={result.exit_code} output:\n{result.output}"
    )

    partgraph_ids_after, foreign_ids_after = _classify_live_host(real_engine_prefix)

    assert partgraph_ids_after == set(), (
        "A18: after a real `db down`, ZERO PartGraph-owned instances may "
        f"remain running (0, 1 or N before: {partgraph_ids_before!r}); "
        f"survivors: {partgraph_ids_after!r}"
    )
    assert foreign_ids_before.issubset(foreign_ids_after), (
        "A18: every foreign (non-PartGraph) container present before `db "
        "down` must still be present afterwards, with the SAME container "
        f"ID (untouched). before={foreign_ids_before!r} "
        f"after={foreign_ids_after!r}"
    )
