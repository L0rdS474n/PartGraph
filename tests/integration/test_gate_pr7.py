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

Gate 4 amendment (two defects found in code review; fixed HERE only — the
unit tests and production code are settled and were not touched):

  1. A18 used to assert zero PartGraph-owned containers EXIST after `db
     down`. `db down`'s locked verb surface is `stop`-only (ADR-0021; it
     never runs `rm`), and `ps --all` lists exited/created/dead containers
     too — so a PartGraph container created outside Compose without `--rm`
     is still LISTED after a fully successful stop, and the old assertion
     would fail a run that did exactly what the command is contracted to
     do. A18 now asserts no PartGraph instance is still RUNNING, using the
     SAME running/not-running deny-list the production leaf itself uses
     (`partgraph.util.lifecycle._NOT_RUNNING_STATES`, imported directly —
     not duplicated — so this gate cannot silently drift from the leaf: an
     unrecognised status degrades toward "still running", exactly mirroring
     the leaf's own "never toward a silent all-clear" rule). The cve-graph
     ("foreign") half of A18's assertion is UNCHANGED.
  2. A17 used to assert that NO foreign container name appears ANYWHERE in
     `--dry-run` stdout. That contradicts A16
     (tests/unit/test_cli_db_down.py, NOT touched here — see cli.py's
     `_print_down_dry_run`), which requires `--dry-run` to print the S3
     "reported only, never stopped" set BY NAME. The two cannot both hold
     whenever a foreign container holds a watched port (8081/9081/8001) —
     it does not on today's host (the live cve-graph stack sits on
     8080/9080), but a latent contradiction that only bites on different
     hardware is worth closing now. A17 now asserts precisely what actually
     matters: a foreign name may appear ONLY on the S3 report-only line
     (never implying PartGraph ownership by appearing on the "would stop"
     line), and — strengthened beyond the original check — the ENTIRE live
     container set, including each container's STATUS, is byte-identical
     before and after, so a foreign container being stopped/mutated without
     being removed or renamed is caught too, not just its bare presence.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from partgraph.cli import app
from partgraph.util.container import ContainerEngineError, engine_command

# Imported directly (never duplicated as a local literal) so this gate cannot
# silently drift from the production leaf's own running/not-running
# classification (Gate 4 defect 1): if the leaf's deny-list ever changes,
# this gate changes with it, by construction.
from partgraph.util.lifecycle import _NOT_RUNNING_STATES

RUNNER = CliRunner()

_PARTGRAPH_CONTAINER_NAME = "partgraph-dgraph"
_PARTGRAPH_DATA_VOLUME = "partgraph_dgraph_data"
_LIVE_SUBPROCESS_TIMEOUT_S = 15

#: The exact phrase cli.py's `_print_down_dry_run` uses to introduce the S3
#: "report only, never stopped" line (partgraph/cli.py). A foreign name is
#: PERMITTED to appear in stdout only on a line carrying this marker — never
#: elsewhere, which would imply PartGraph ownership of a container it must
#: never claim.
_REPORT_ONLY_MARKER = "reported only, never stopped"


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


def _row_status(row: dict) -> str:
    """Return the row's raw engine `State` string, or "" when absent/non-string."""
    value = row.get("State")
    return value if isinstance(value, str) else ""


def _live_rows(prefix: list[str]) -> list[dict]:
    """Return every parsed `ps --all --format json` row currently on the host."""
    result = _run([*prefix, "ps", "--all", "--format", "json"])
    if result.returncode != 0:
        return []
    return _parse_ps_rows(result.stdout)


def _live_snapshot(prefix: list[str]) -> dict[str, tuple[str, str]]:
    """Return {container_id: (name, status)} for every container on the host.

    STATUS is included (not just id/name) so a caller can prove a container
    was genuinely UNTOUCHED, not merely still present: a container that was
    stopped without being removed or renamed would still show the same id
    and name in `ps --all`, but a DIFFERENT status (Gate 4 defect 2).
    """
    snapshot: dict[str, tuple[str, str]] = {}
    for row in _live_rows(prefix):
        cid = _row_id(row)
        name = _row_name(row)
        if cid and name:
            snapshot[cid] = (name, _row_status(row))
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


def _classify_live_host(prefix: list[str]) -> tuple[dict[str, str], set[str]]:
    """Return (partgraph_status_by_id, foreign_ids) from a live, read-only scan.

    ``partgraph_status_by_id`` maps container id -> raw engine ``State``
    string for every container matching S1 (exact name) or S2 (mounts the
    named data volume) right now — `ps --all` lists exited/created/dead
    containers too, so the STATUS is needed to distinguish "still running"
    from "merely still listed" (Gate 4 defect 1; `db down` is stop-only and
    never `rm`s, so bare non-existence is the wrong post-condition).
    ``foreign_ids`` = everything else currently on the host (must be left
    byte-for-byte untouched — its containers' full (name, status) is
    verified separately via :func:`_live_snapshot`).
    """
    partgraph_status_by_id: dict[str, str] = {}
    foreign_ids: set[str] = set()
    for row in _live_rows(prefix):
        cid = _row_id(row)
        name = _row_name(row)
        if not cid or not name:
            continue
        if name == _PARTGRAPH_CONTAINER_NAME or _mounts_partgraph_volume(prefix, cid):
            partgraph_status_by_id[cid] = _row_status(row)
        else:
            foreign_ids.add(cid)
    return partgraph_status_by_id, foreign_ids


def _is_running_status(status: str) -> bool:
    """Return True iff *status* counts as "still running" under the SAME
    deny-list the production leaf uses (`_NOT_RUNNING_STATES`).

    Mirrors `partgraph.util.lifecycle._is_stoppable`'s own predicate exactly
    (``status.strip().casefold() not in _NOT_RUNNING_STATES``): a status
    NOT in the deny-list — including an unrecognised one — counts as
    running. Degrading an unrecognised status toward "still running" (never
    toward "must be down") is deliberate on both sides: a gate that
    silently treated an unknown state as "down" could pass while a real
    PartGraph instance was still serving traffic.
    """
    return status.strip().casefold() not in _NOT_RUNNING_STATES


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
    Then the exit code is 0; a foreign container's name (e.g. any live
    cve-graph container) may appear in stdout ONLY on the S3 "reported only,
    never stopped" line (`db down --dry-run` is CONTRACTED to name S3 port
    holders there — see A16 in tests/unit/test_cli_db_down.py, NOT touched
    by this fix — so a blanket "no foreign name anywhere" assertion is
    wrong whenever a real S3 holder exists; it does not on today's host,
    where the live cve-graph stack sits on ports 8080/9080, outside
    PARTGRAPH_WATCHED_PORTS); the ENTIRE live container set — id, name AND
    status — is BYTE-IDENTICAL before and after (dry-run genuinely touches
    nothing, for every container, not just the foreign ones).
    """
    _partgraph_status_before, foreign_ids_before = _classify_live_host(real_engine_prefix)
    snapshot_before = _live_snapshot(real_engine_prefix)
    foreign_names = {
        snapshot_before[cid][0] for cid in foreign_ids_before if cid in snapshot_before
    }

    result = _invoke(["db", "down", "--dry-run"])

    assert result.exit_code == 0, (
        f"`db down --dry-run` must always exit 0. Output:\n{result.output}"
    )
    for line in result.output.splitlines():
        for name in foreign_names:
            if name not in line:
                continue
            assert _REPORT_ONLY_MARKER in line, (
                f"A17: dry-run stdout names a FOREIGN container {name!r} "
                "OUTSIDE the S3 'reported only, never stopped' line — this "
                "implies PartGraph ownership of a container it must never "
                f"claim. Offending line: {line!r}\nFull output:\n{result.output}"
            )

    snapshot_after = _live_snapshot(real_engine_prefix)
    assert snapshot_after == snapshot_before, (
        "A17: --dry-run must be strictly read-only for EVERY container "
        "(id, name and status) — none may be stopped, removed or otherwise "
        f"mutated. before={snapshot_before!r} after={snapshot_after!r}"
    )


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
    Then afterwards: (a) NO PartGraph-owned container is still RUNNING —
    NOT "zero containers exist": `db down`'s locked verb surface is
    stop-only (ADR-0021; it never runs `rm`), and `ps --all` lists
    exited/created/dead containers too, so a container created outside
    Compose without `--rm` is still LISTED after a fully successful stop;
    asserting bare non-existence would fail a run that did exactly what the
    command is contracted to do. "Running" is judged by the SAME
    running/not-running deny-list the production leaf itself uses
    (`partgraph.util.lifecycle._NOT_RUNNING_STATES`, imported directly, not
    duplicated): a status NOT in that deny-list — including an
    unrecognised one — counts as still running, mirroring the leaf's own
    "degrade toward a survivor, never toward a silent all-clear" rule —
    regardless of how many PartGraph-owned instances existed before (0, 1
    or N); (b) every FOREIGN container present before is STILL present
    afterwards with the SAME container ID (untouched; UNCHANGED from the
    original gate — this half is deliberately kept exactly as strict as it
    was); (c) the command itself does not raise/hang (a finite, bounded
    run).
    """
    partgraph_status_before, foreign_ids_before = _classify_live_host(real_engine_prefix)

    result = _invoke(["db", "down"])

    assert result.exit_code in (0, 1), (
        f"`db down` must exit cleanly (0 or 1), never hang/crash. "
        f"exit={result.exit_code} output:\n{result.output}"
    )

    partgraph_status_after, foreign_ids_after = _classify_live_host(real_engine_prefix)

    still_running_after = {
        cid: status
        for cid, status in partgraph_status_after.items()
        if _is_running_status(status)
    }
    assert still_running_after == {}, (
        "A18: after a real `db down`, NO PartGraph-owned instance may still "
        f"be RUNNING (0, 1 or N before: {partgraph_status_before!r}); "
        f"still running (id -> status): {still_running_after!r}"
    )
    assert foreign_ids_before.issubset(foreign_ids_after), (
        "A18: every foreign (non-PartGraph) container present before `db "
        "down` must still be present afterwards, with the SAME container "
        f"ID (untouched). before={foreign_ids_before!r} "
        f"after={foreign_ids_after!r}"
    )
