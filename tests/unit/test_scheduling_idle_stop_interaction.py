"""
Tests: PR-C (feat/db-idle-autostop) — C-13, the interaction between
`partgraph db idle-stop` (this PR) and the ALREADY-SHIPPED scheduled refresh
path (`scripts/partgraph-refresh-all.sh` / `systemd/partgraph-refresh-all.
{service,timer}`, PR-B2's `PARTGRAPH_AUTOSTART=0` forcing).

"Do not leave the interaction implicit — that omission is exactly what
Gate 3b caught in PR-B2." This file pins it explicitly.

OWN RULING (flagged for pushback — the AC asks this to be decided, not
dictated):

1. **The two timers are independently opt-in.** Installing
   `partgraph-db-idle-stop.timer` does not require, and must not be coupled
   to, installing `partgraph-refresh-all.timer`, or vice versa — mirrors
   ADR-0014's own "the repo ships the unit; the operator decides" pattern,
   applied twice rather than once.

2. **`db idle-stop` never autostarts (C-12).** If the database is already
   down when it fires, it is a genuine no-op — `stop_all()`'s own read-only
   enumeration finds nothing and there is nothing to report (already proven
   generically in `tests/unit/test_cli_idle_stop.py`). No special-casing of
   "a refresh is about to run" is needed for THIS half.

3. **A live lease already protects a CONCURRENTLY-RUNNING scheduled
   refresh — reusing the SAME generic mechanism (C-3/C-4), not a special
   case.** `refresh`/`refresh-links` are both paging, lease-holding,
   heartbeating commands (`tests/unit/test_cli_activity_wiring.py`'s own
   C-2 section). While either is mid-run — scheduled or interactive, idle-
   stop cannot tell the difference and must not need to — its lease
   unconditionally blocks `db idle-stop` (C-4), exactly as it blocks any
   other concurrent invocation. `test_a_scheduled_refreshs_lease_blocks_a_
   concurrent_idle_stop_exactly_like_any_other_lease` below proves this
   directly against the leaf, tying the already-generic property explicitly
   to this scenario.

4. **The genuinely NEW, residual risk C-13 asks to be named: a scheduled
   refresh CAN fail if idle-stop stopped an idle database BETWEEN two
   refresh runs.** `partgraph-refresh-all.sh` forces
   `PARTGRAPH_AUTOSTART=0` (PR-B2) specifically so a schedule-triggered run
   never implicitly starts a container. If an operator ALSO installs
   `partgraph-db-idle-stop.timer`, and it legitimately stops an idle
   database between refresh runs, the NEXT scheduled `refresh`/
   `refresh-links` invocation finds the database down and fails — loudly,
   visibly to the scheduler (already `partgraph-refresh-all.sh`'s own,
   pre-existing, pinned contract: a non-zero phase exit propagates as the
   wrapper's own exit code — see `tests/unit/test_scheduling_wrapper.py`).
   This is NOT a bug idle-stop must special-case away — doing so would
   require `db idle-stop` to know about `partgraph-refresh-all.timer`'s own
   schedule, an inappropriate coupling between two independently opt-in
   units. It is a genuine, foreseeable operational trade-off an operator who
   installs BOTH timers accepts, and it must be DOCUMENTED, not left
   implicit — pinned by the doc-consistency checks below.

5. **Neither `scripts/partgraph-refresh-all.sh` nor
   `systemd/partgraph-refresh-all.service` needs to change for this PR.**
   This file does not re-test their own, already-pinned contract
   (`tests/unit/test_scheduling_wrapper.py`,
   `tests/unit/test_scheduling_autostart_disabled.py`) — it only pins the
   NEW cross-reference the idle-stop unit and `docs/scheduling.md` must
   carry once PR-C lands.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest

# This import is expected to raise ModuleNotFoundError until
# src/partgraph/util/activity.py exists — the correct test-first red state.
from partgraph.util.activity import (  # noqa: E402
    REASON_LIVE_LEASE,
    REASON_STALE,
    acquire_lease,
    evaluate_idle,
    touch_activity,
)

SCHEDULING_DOC_REL = "docs/scheduling.md"
IDLE_STOP_SERVICE_REL = "systemd/partgraph-db-idle-stop.service"


def _dt(y, m, d, h=0, mi=0, s=0) -> datetime:  # noqa: PLR0913
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Point 3 — the lease-protects-concurrent-refresh property, tied explicitly
# to this scenario (the underlying mechanism is already generically proven
# in test_activity.py's own C-4 section; this test names the SCENARIO).
# ---------------------------------------------------------------------------


def test_a_scheduled_refreshs_lease_blocks_a_concurrent_idle_stop_exactly_like_any_other_lease(
    tmp_path,
) -> None:
    """Given a SCHEDULED `partgraph refresh` invocation (PID from the real,
    currently-running process — modelling `partgraph-refresh-all.timer`
    having just started `refresh`) is genuinely mid-run and holds a live
    lease, AND — the load-bearing part — its OWN activity stamp is
    deliberately STALE (it has been running long enough, or the scheduler
    fired at an unlucky moment relative to the last recorded activity, that
    the stamp ALONE would already say "stop").
    When `partgraph db idle-stop` fires concurrently (as it might, on its
    own independent schedule).
    Then it does NOT stop the database — the SAME `REASON_LIVE_LEASE`
    decision C-4 already guarantees generically, now demonstrated for the
    concrete scheduled-refresh scenario C-13 names.
    """
    import os

    state_dir = tmp_path / "state"
    acquire_lease(state_dir=state_dir, pid=os.getpid())
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))  # deliberately stale

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
    )
    assert decision.should_stop is False
    assert decision.reason == REASON_LIVE_LEASE


def test_once_the_scheduled_refresh_releases_its_lease_idle_stop_may_stop_normally(
    tmp_path,
) -> None:
    """Given the SAME scenario, but the refresh invocation has since
    finished and released its lease (a normal, successful completion —
    C-3), and its activity stamp is stale.
    When `db idle-stop` fires afterward.
    Then it is free to stop — proving the protection is scoped to WHILE the
    refresh is genuinely running, not a permanent block once one has ever
    run (which would defeat idle-stop's whole purpose after any scheduled
    refresh)."""
    import os

    from partgraph.util.activity import held_lease

    state_dir = tmp_path / "state"
    with held_lease(state_dir=state_dir, pid=os.getpid()):
        touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))
    # lease released on context-manager exit (C-3)

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
    )
    assert decision.should_stop is True
    assert decision.reason == REASON_STALE


# ---------------------------------------------------------------------------
# Point 4/5 — the residual gap must be DOCUMENTED, not left implicit.
# ---------------------------------------------------------------------------


def _scheduling_doc_text(repo_root: pathlib.Path) -> str:
    path = repo_root / SCHEDULING_DOC_REL
    assert path.exists(), f"{SCHEDULING_DOC_REL} is expected to already exist (ADR-0014)."
    return path.read_text(encoding="utf-8")


def test_scheduling_doc_names_the_idle_stop_interaction(repo_root: pathlib.Path) -> None:
    """C-13: Given `docs/scheduling.md` already exists (ADR-0014) and
    already documents the `PARTGRAPH_AUTOSTART=0` scheduling guarantee.
    When its full text is scanned.
    Then it ALSO mentions `idle-stop` (or `idle-autostop`) somewhere — an
    operator reading the scheduling guide must be told that a SECOND,
    independently-installable timer exists which can legitimately stop the
    database BETWEEN scheduled refresh runs, and that a resulting refresh
    failure is expected in that case, not a bug. This is a genuine RED
    failure today (the current doc predates PR-C and says nothing about
    it) — not merely a not-yet-created-file skip.
    """
    text = _scheduling_doc_text(repo_root).lower()
    assert "idle-stop" in text or "idle-autostop" in text or "idle auto-stop" in text, (
        f"{SCHEDULING_DOC_REL} does not mention idle-stop at all. Once PR-C "
        "lands, an operator installing BOTH timers must be told that "
        "idle-stop can legitimately stop the database between scheduled "
        "refresh runs, and that the next refresh failing as a result is "
        "expected (PARTGRAPH_AUTOSTART=0 means it will not restart itself)."
    )


@pytest.fixture(scope="module")
def idle_stop_service_text(repo_root: pathlib.Path) -> str:
    path = repo_root / IDLE_STOP_SERVICE_REL
    if not path.exists():
        pytest.skip(f"{IDLE_STOP_SERVICE_REL} does not exist yet (expected pre-PR-C).")
    return path.read_text(encoding="utf-8")


def test_idle_stop_service_header_names_the_refresh_interaction(
    idle_stop_service_text: str,
) -> None:
    """C-13: Given the idle-stop unit's OWN header comment is where an
    operator installing it would first read about its scope (mirrors
    `partgraph-refresh-all.service`'s own header, which already explains
    the scheduling/autostart interaction from the OTHER side).
    When the comment block before `[Unit]` is scanned.
    Then it mentions 'refresh' — naming the interaction with the scheduled
    refresh path, not merely describing idle-stop in isolation.
    """
    lines = idle_stop_service_text.splitlines()
    unit_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "[Unit]"), None)
    assert unit_idx is not None, f"{IDLE_STOP_SERVICE_REL} has no [Unit] section."
    header = "\n".join(lines[:unit_idx]).lower()
    assert "refresh" in header, (
        f"{IDLE_STOP_SERVICE_REL}'s header comment does not mention the "
        "scheduled refresh path — an operator installing both timers must "
        "be told idle-stop can legitimately stop the database between "
        "refresh runs.\n\nHeader text:\n{header}"
    )
