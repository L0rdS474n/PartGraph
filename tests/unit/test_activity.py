"""
Tests: PR-C (feat/db-idle-autostop) — `partgraph.util.activity` (ADR-0022,
"idle auto-stop", out of scope for PR-B1/PR-B2, now this PR's own subject).

WHY A HOST-SIDE TIMER, NOT AN IN-PROCESS ONE (read this before assuming an
in-process timer was overlooked). `partgraph` is a one-shot CLI: every
invocation runs its command and exits. Nothing survives the exit to observe
idleness later — there is no daemon, no event loop, no background thread that
could wake up 30 minutes after the last command finished. The only thing that
CAN act after `partgraph` itself has already exited is something OUTSIDE the
process: a host-side `systemd --user` timer that periodically invokes
`partgraph db idle-stop` as its own, separate, one-shot command — exactly the
opt-in pattern ADR-0014 already established for `partgraph-refresh-all.timer`
(the repo ships the unit; the repo never enables it; the operator installs
it). `partgraph.util.activity` supplies the STATE two different one-shot
invocations need to cooperate through disk (an activity "last touched" stamp,
and a "someone is working right now" lease) and the pure DECISION
(`evaluate_idle`) a later `db idle-stop` invocation uses to decide, from that
state alone, whether it is safe to hand off to
`partgraph.util.lifecycle.stop_all()`. This module ships no timer of its own
and starts nothing; see `systemd/partgraph-db-idle-stop.{service,timer}`
(C-11) for the actual host-side mechanism, and `tests/unit/
test_systemd_idle_stop_units.py` for its own contract.

LEAF DISCIPLINE (mechanically enforced in `tests/unit/
test_activity_architecture.py`, not here): `partgraph.util.activity` is
stdlib + `psutil` only. It must know NOTHING about containers, Compose, or
the container engine — `partgraph.util.lifecycle` knows nothing about
activity in return; the two are combined only inside `partgraph.cli`'s
`db idle-stop` command (`tests/unit/test_cli_idle_stop.py`). It must never
import `partgraph.cli`, `partgraph.util.lifecycle`, `partgraph.util.container`,
or any embed/query/load module.

NOT YET IMPLEMENTED. `src/partgraph/util/activity.py` does not exist yet —
this whole file is expected to ERROR at COLLECTION with ModuleNotFoundError,
mirroring `tests/unit/test_cli_db_down.py`'s own documented pre-PR-A history
for `partgraph.util.lifecycle`. This is the correct test-first RED state.

Pinned contract this file specifies:

  Module-level constants:
    `DEFAULT_IDLE_TIMEOUT_MINUTES: float = 30.0` — the documented default
    (C-6). Env-var parsing of `PARTGRAPH_IDLE_TIMEOUT_MINUTES` itself is
    DELIBERATELY NOT this leaf's job — it lives in `partgraph.cli`
    (`tests/unit/test_cli_idle_stop.py`'s own `_idle_timeout_minutes()`
    section), mirroring `_autostart_enabled()`'s own precedent ("Parsing
    lives HERE [the CLI] and not in the leaf on purpose... this is the CLI's
    policy, so the CLI owns it" — `src/partgraph/cli.py`). This leaf only
    ever receives an already-parsed `idle_timeout_minutes: float`.

    `REASON_DISABLED`, `REASON_LIVE_LEASE`, `REASON_UNDETERMINED_LEASE`,
    `REASON_FRESH_STAMP`, `REASON_STALE`, `REASON_NOTHING_TO_DO`,
    `REASON_STAMP_BOOTSTRAPPED` — plain string tags (mirrors
    `partgraph.util.lifecycle`'s own `_OWNER_NAME_MATCH = "S1"` style),
    naming WHY an `IdleDecision` was reached, so a test can assert the reason,
    not merely the boolean.

  DTOs (frozen dataclasses, mirroring `Instance`/`UnitState`/`DownResult`):
    `Lease(pid: int, create_time: float, acquired_utc: str)`
    `IdleDecision(should_stop: bool, reason: str)`

  State-file mechanics (C-1, C-3, C-14):
    `default_state_dir() -> Path` — the SAME `data/state` directory the
    existing normalize/load checkpoints already use (verified directly
    against `partgraph.cli.NORMALIZE_CHECKPOINT_PATH`/`LOAD_CHECKPOINT_PATH`
    below, not re-derived independently).
    `activity_stamp_path(state_dir) -> Path`
    `lease_path(state_dir, pid=None) -> Path` — PID-scoped (own reasoning
    below, "two concurrent leases").
    `lease_paths(state_dir) -> tuple[Path, ...]` — every currently-present
    lease file, regardless of liveness.
    `touch_activity(*, state_dir, now=None) -> None`
    `read_activity_stamp(state_dir) -> datetime | None`
    `acquire_lease(*, state_dir, pid=None, now=None, psutil_module=None) -> None`
    `release_lease(*, state_dir, pid=None) -> None`
    `held_lease(*, state_dir, pid=None, now=None, psutil_module=None)` —
    context manager: acquire on enter, release in a `finally` on exit,
    including when the wrapped body raises (C-3).
    `read_lease(state_dir, pid=None) -> Lease | None`

  Decision (C-4..C-10):
    `evaluate_idle(*, state_dir, idle_timeout_minutes, db_reachable,
    now=None, psutil_module=None) -> IdleDecision`

OWN RULING — a per-process (PID-scoped) lease file, not one shared file
(beyond what any single AC states verbatim, flagged here for the parent
agent/reviewer to push back on if they disagree). C-3 says "a DB-touching
command records A lease"; a single shared `activity_lease.json` would let a
SECOND concurrently-running `partgraph` invocation's `acquire_lease`
silently clobber the FIRST's still-live lease — and if the second finishes
and releases first, `db idle-stop` would then see "no lease at all" while
the FIRST invocation is still genuinely doing real work. Scoping the lease
filename by PID (`lease_path` below) and having `evaluate_idle` scan ALL
present lease files removes that hazard for a negligible cost (an extra
glob). Pinned explicitly by
`test_two_concurrent_leases_one_dead_one_live_still_blocks_and_cleans_only_
the_dead_one` below.

OWN RULING — a malformed lease file is UNDETERMINED, not DEAD (also flagged
for pushback). C-5 says "a dead lease is ignored and cleaned"; that
sentence describes a lease that WAS read successfully and whose recorded
PID is confirmed, via `psutil`, to be gone. A lease file that cannot even be
parsed (corrupt JSON, a missing required field) is a DIFFERENT, ambiguous
case with no AC coverage. Given this codebase's own established asymmetry
throughout `partgraph.util.lifecycle` ("I could not tell" must never be
recorded as "I checked, and it is not ours" — the `UNKNOWN` tag), the same
direction is applied here: an unparseable lease degrades to
`REASON_UNDETERMINED_LEASE` (blocks the stop, exactly like a live one) and
is left ON DISK untouched — never silently deleted, and never silently
treated as safe to stop past.

OWN RULING — an unreadable `psutil.AccessDenied`/other unexpected error
while checking a WELL-FORMED lease is ALSO undetermined, not dead — same
reasoning, distinct from the clean `NoSuchProcess`/`ZombieProcess` case
(confirmed, via a real interpreter, to be a subclass of `NoSuchProcess` in
the installed psutil — see `test_zombie_process_is_treated_as_dead_via_the_
real_psutil_exception_hierarchy` below) which alone means "confirmed gone".
"""

from __future__ import annotations

import json
import math
import os
import pathlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import psutil
import pytest

# This import is expected to raise ModuleNotFoundError until
# src/partgraph/util/activity.py exists — the correct test-first red state.
from partgraph.util.activity import (  # noqa: E402
    DEFAULT_IDLE_TIMEOUT_MINUTES,
    REASON_DISABLED,
    REASON_FRESH_STAMP,
    REASON_LIVE_LEASE,
    REASON_NOTHING_TO_DO,
    REASON_STALE,
    REASON_STAMP_BOOTSTRAPPED,
    REASON_UNDETERMINED_LEASE,
    IdleDecision,
    Lease,
    acquire_lease,
    activity_stamp_path,
    default_state_dir,
    evaluate_idle,
    held_lease,
    lease_path,
    lease_paths,
    read_activity_stamp,
    read_lease,
    release_lease,
    touch_activity,
)

# ---------------------------------------------------------------------------
# Fixture builders (local to this file — CONTRIBUTING.md's "Test fixtures
# stay local to their file" policy).
# ---------------------------------------------------------------------------


def _dt(  # noqa: PLR0913 — a fixed-instant builder needs one arg per field.
    year, month, day, hour=0, minute=0, second=0, microsecond=0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)


class _FakeProcess:
    """A minimal stand-in for `psutil.Process`, injected via `psutil_module`."""

    def __init__(self, pid: int, *, create_time: float | None, error: Exception | None = None):
        self._pid = pid
        self._create_time = create_time
        self._error = error

    def create_time(self) -> float:
        if self._error is not None:
            raise self._error
        return self._create_time


class _FakePsutilModule:
    """A minimal stand-in for the whole `psutil` module, sharing the REAL
    exception classes (imported from the actually-installed `psutil`) so a
    test proves the implementation catches the SAME classes real psutil
    raises, not a look-alike hierarchy invented for the test.
    """

    NoSuchProcess = psutil.NoSuchProcess
    ZombieProcess = psutil.ZombieProcess
    AccessDenied = psutil.AccessDenied

    def __init__(self, processes: dict[int, _FakeProcess]):
        self._processes = processes

    def Process(self, pid: int):
        if pid not in self._processes:
            raise psutil.NoSuchProcess(pid)
        return self._processes[pid]


def _fake_psutil(**by_pid: _FakeProcess) -> _FakePsutilModule:
    return _FakePsutilModule(by_pid)


# ---------------------------------------------------------------------------
# C-1 mechanism — state dir resolution matches the EXISTING checkpoint
# directory (verified against the real, existing cli.py constants, not
# re-derived independently).
# ---------------------------------------------------------------------------


def test_default_state_dir_is_the_same_directory_the_existing_checkpoints_use(
    repo_root: pathlib.Path,
) -> None:
    """C-1: Given `src/partgraph/cli.py` already resolves its normalize/load
    checkpoints to `<repo_root>/data/state/...` (`NORMALIZE_CHECKPOINT_PATH`,
    `LOAD_CHECKPOINT_PATH` — read directly off disk, not assumed).
    When `default_state_dir()` is called.
    Then it returns EXACTLY that same directory — verified against the real,
    already-existing `partgraph.cli` module attributes, not a fresh,
    independently-derived guess that could silently drift from them.
    """
    from partgraph.cli import LOAD_CHECKPOINT_PATH, NORMALIZE_CHECKPOINT_PATH

    assert default_state_dir() == NORMALIZE_CHECKPOINT_PATH.parent
    assert default_state_dir() == LOAD_CHECKPOINT_PATH.parent
    assert default_state_dir() == repo_root / "data" / "state"
    assert default_state_dir().is_absolute()


# ---------------------------------------------------------------------------
# C-1 / C-14 — the activity stamp: atomic write, no absolute path in its
# content, monotonic-safe (never regresses), warn-once-never-crash.
# ---------------------------------------------------------------------------


def test_touch_activity_creates_state_dir_and_a_single_stamp_file(tmp_path) -> None:
    """C-1: Given a state dir that does not exist yet.
    When `touch_activity` is called.
    Then the state dir is created, exactly one stamp file exists afterward
    (no leftover `.tmp` file from the atomic write survives), and
    `read_activity_stamp` returns the instant that was written.
    """
    state_dir = tmp_path / "state"
    moment = _dt(2026, 7, 28, 12, 0, 0)

    touch_activity(state_dir=state_dir, now=lambda: moment)

    assert state_dir.is_dir()
    leftover_tmp = list(state_dir.glob("*.tmp"))
    assert leftover_tmp == [], f"a temp file survived the atomic write: {leftover_tmp!r}"
    assert activity_stamp_path(state_dir).is_file()
    assert read_activity_stamp(state_dir) == moment


def test_touch_activity_stamp_content_contains_no_absolute_path(tmp_path) -> None:
    """C-1: Given `state_dir` is itself a deep, real absolute path (as every
    `tmp_path` genuinely is).
    When `touch_activity` writes the stamp and its raw bytes are read back.
    Then the state dir's own absolute path string never appears anywhere in
    the file's content, and no forward-slash-bearing value is present — the
    property C-1 actually asks for, not a proxy like "the file has a
    'path' key" (it must not have ANY path-shaped value, named or not).
    """
    state_dir = tmp_path / "some" / "deep" / "state" / "dir"
    touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 1, 1))

    raw = activity_stamp_path(state_dir).read_bytes().decode("utf-8")
    assert str(state_dir) not in raw
    assert str(tmp_path) not in raw
    payload = json.loads(raw)
    for value in payload.values():
        if isinstance(value, str):
            assert "/" not in value, f"a path-shaped value leaked into the stamp: {value!r}"


def test_touch_activity_uses_a_temp_file_and_os_replace(tmp_path, monkeypatch) -> None:
    """C-1: Given `os.replace` is the last step of every other atomic write
    already in this repo (`normalize/run.py`, `load/loader.py`'s own
    checkpoints — "temp file + os.replace so a crash mid-write cannot
    corrupt the marker").
    When `touch_activity` writes the stamp.
    Then `os.replace` is called exactly once, with a SOURCE path ending in
    `.tmp` and a DESTINATION equal to `activity_stamp_path(state_dir)`.
    """
    state_dir = tmp_path / "state"
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy_replace)
    touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 1, 1))

    assert len(calls) == 1, calls
    src, dst = calls[0]
    assert src.endswith(".tmp"), src
    assert dst == str(activity_stamp_path(state_dir))


def test_touch_activity_never_regresses_the_stamp_on_a_backward_clock_step(tmp_path) -> None:
    """C-1 "monotonic-safe": Given a stamp already recorded at T2.
    When `touch_activity` is called again with `now` returning an EARLIER
    instant T1 (a real hazard: NTP stepping the wall clock backward mid-run,
    or two heartbeats completing out of order across cores).
    Then the stamp on disk is UNCHANGED — still T2 — never regressed to T1.
    A regression here would make a database that WAS recently confirmed
    active suddenly look OLDER (more idle) than it already was recorded as,
    which is exactly the wrong direction for a control that guards against
    stopping something in use.
    """
    state_dir = tmp_path / "state"
    t2 = _dt(2026, 7, 28, 12, 0, 0)
    t1 = _dt(2026, 7, 28, 11, 0, 0)
    assert t1 < t2

    touch_activity(state_dir=state_dir, now=lambda: t2)
    touch_activity(state_dir=state_dir, now=lambda: t1)

    assert read_activity_stamp(state_dir) == t2, (
        "touch_activity must never regress an existing, newer stamp to an "
        "older 'now' value"
    )


def test_touch_activity_advances_normally_when_now_is_later(tmp_path) -> None:
    """Given a stamp already recorded at T1.
    When `touch_activity` is called again with a LATER `now` T2.
    Then the stamp on disk advances to T2 — the ordinary, non-skew case.
    """
    state_dir = tmp_path / "state"
    t1 = _dt(2026, 7, 28, 11, 0, 0)
    t2 = _dt(2026, 7, 28, 12, 0, 0)

    touch_activity(state_dir=state_dir, now=lambda: t1)
    touch_activity(state_dir=state_dir, now=lambda: t2)

    assert read_activity_stamp(state_dir) == t2


def test_touch_activity_warns_once_and_never_raises_when_rename_fails(
    tmp_path, monkeypatch, caplog
) -> None:
    """C-14: Given the state dir cannot actually be written (the final
    `os.replace` fails — e.g. a full disk, or a read-only mount).
    When `touch_activity` is called TWICE in a row (modelling a paginated
    command's per-page heartbeat, C-2, hammering an unwritable state dir for
    a whole run).
    Then NEITHER call raises — the DB command this stamp is a side effect of
    must never crash because its own bookkeeping failed — and exactly ONE
    warning is logged for the whole pair, not two: a multi-hour run must not
    flood the log with an identical warning every single page.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setattr(
        os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated: disk full"))
    )

    with caplog.at_level("WARNING"):
        touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 1, 1))
        touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 1, 1, 0, 1))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, (
        f"expected exactly one warning across two failed touch_activity calls "
        f"against the SAME unwritable state dir, got {len(warnings)}: {warnings!r}"
    )


def test_touch_activity_warns_when_state_dir_cannot_be_created(
    tmp_path, monkeypatch, caplog
) -> None:
    """C-14: Given the state dir's own parent creation fails (e.g. a
    permissions error, or the path collides with an existing plain file).
    When `touch_activity` is called.
    Then it does not raise, and a warning is logged.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setattr(
        pathlib.Path,
        "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("simulated: permission denied")),
    )

    with caplog.at_level("WARNING"):
        touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 1, 1))

    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_touch_activity_warn_once_scope_is_per_state_dir_not_global(
    tmp_path, monkeypatch, caplog
) -> None:
    """C-14: Given TWO different, independently-unwritable state dirs.
    When `touch_activity` fails against each once.
    Then EACH gets its own warning — "warn once" suppresses a FLOOD against
    the SAME target across repeated calls (C-2's heartbeat case), it must
    not globally suppress a genuinely different failure elsewhere.
    """
    monkeypatch.setattr(
        os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated"))
    )
    with caplog.at_level("WARNING"):
        touch_activity(state_dir=tmp_path / "state_a", now=lambda: _dt(2026, 1, 1))
        touch_activity(state_dir=tmp_path / "state_b", now=lambda: _dt(2026, 1, 1))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2, warnings


def test_read_activity_stamp_returns_none_for_missing_file(tmp_path) -> None:
    """Given no stamp file exists.
    When `read_activity_stamp` is called.
    Then it returns None (never raises)."""
    assert read_activity_stamp(tmp_path / "state") is None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json at all", id="not_json"),
        pytest.param("{}", id="empty_object"),
        pytest.param('{"last_active_utc": 12345}', id="wrong_type"),
        pytest.param('{"last_active_utc": "not a timestamp"}', id="unparseable_timestamp"),
        pytest.param("[]", id="json_array_not_object"),
    ],
)
def test_read_activity_stamp_degrades_to_none_never_raises_on_malformed_content(
    tmp_path, raw: str
) -> None:
    """Given a stamp file that exists but is malformed in some way.
    When `read_activity_stamp` is called.
    Then it returns None rather than raising."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    activity_stamp_path(state_dir).write_text(raw, encoding="utf-8")
    assert read_activity_stamp(state_dir) is None


def test_activity_stamp_round_trips_sub_second_precision(tmp_path) -> None:
    """Given a moment carrying microsecond precision.
    When it is written then read back.
    Then the round trip preserves it exactly — a heartbeat-per-page write can
    legitimately happen faster than one second apart during a fast test run,
    and second-only precision would make two closely-spaced heartbeats
    compare equal by accident."""
    state_dir = tmp_path / "state"
    moment = _dt(2026, 7, 28, 12, 0, 0, 123456)
    touch_activity(state_dir=state_dir, now=lambda: moment)
    assert read_activity_stamp(state_dir) == moment


# ---------------------------------------------------------------------------
# C-3 — the lease: acquire/release, atomic, no absolute path, PID-scoped,
# released even on error paths.
# ---------------------------------------------------------------------------


def test_acquire_lease_records_the_calling_processs_real_pid_and_create_time(tmp_path) -> None:
    """C-3: Given the REAL, installed psutil (no injected fake) and the
    CURRENT pytest process's own PID.
    When `acquire_lease` is called for that PID with no injected seam.
    Then the persisted lease's `pid` and `create_time` match what real
    psutil itself reports for this very process — proving genuine
    integration with the real library, not only against an injected fake.
    """
    state_dir = tmp_path / "state"
    pid = os.getpid()
    acquire_lease(state_dir=state_dir, pid=pid)

    lease = read_lease(state_dir, pid=pid)
    assert lease is not None
    assert lease.pid == pid
    assert lease.create_time == pytest.approx(psutil.Process(pid).create_time(), abs=1.0)


def test_lease_content_contains_no_absolute_path(tmp_path) -> None:
    """C-1's "no absolute path" property, extended to the lease file."""
    state_dir = tmp_path / "a" / "deep" / "state" / "dir"
    acquire_lease(
        state_dir=state_dir,
        pid=4242,
        now=lambda: _dt(2026, 1, 1),
        psutil_module=_fake_psutil(**{4242: _FakeProcess(4242, create_time=100.0)}),
    )
    raw = lease_path(state_dir, pid=4242).read_bytes().decode("utf-8")
    assert str(state_dir) not in raw
    assert str(tmp_path) not in raw


def test_release_lease_removes_the_file_and_is_idempotent(tmp_path) -> None:
    """C-3: Given a held lease.
    When `release_lease` is called, then called AGAIN on the same (now
    absent) file.
    Then the file is gone after the first call, and the second call does not
    raise."""
    state_dir = tmp_path / "state"
    acquire_lease(
        state_dir=state_dir, pid=111, now=lambda: _dt(2026, 1, 1),
        psutil_module=_fake_psutil(**{111: _FakeProcess(111, create_time=1.0)}),
    )
    assert lease_path(state_dir, pid=111).exists()

    release_lease(state_dir=state_dir, pid=111)
    assert not lease_path(state_dir, pid=111).exists()

    release_lease(state_dir=state_dir, pid=111)  # must not raise


def test_release_lease_on_a_state_dir_with_no_lease_at_all_does_not_raise(tmp_path) -> None:
    """Given a state dir that was never used for a lease at all.
    When `release_lease` is called.
    Then it does not raise."""
    release_lease(state_dir=tmp_path / "state", pid=999)


def test_held_lease_releases_on_normal_exit(tmp_path) -> None:
    """C-3: Given the `held_lease` context manager.
    When the `with` block completes normally.
    Then the lease file is gone afterward."""
    state_dir = tmp_path / "state"
    fake = _fake_psutil(**{os.getpid(): _FakeProcess(os.getpid(), create_time=1.0)})
    with held_lease(state_dir=state_dir, now=lambda: _dt(2026, 1, 1), psutil_module=fake):
        assert lease_path(state_dir).exists()
    assert not lease_path(state_dir).exists()


def test_held_lease_releases_even_when_the_body_raises(tmp_path) -> None:
    """C-3: "clears it on exit, INCLUDING ON ERROR PATHS" — the actual
    property, not a proxy. Given a DB-touching command's own work raises.
    When it raises INSIDE a `with held_lease(...):` block.
    Then the original exception still propagates unmodified AND the lease
    file is gone afterward — `finally`, not merely "on the happy path"."""
    state_dir = tmp_path / "state"
    fake = _fake_psutil(**{os.getpid(): _FakeProcess(os.getpid(), create_time=1.0)})

    class _BoomError(RuntimeError):
        pass

    with (
        pytest.raises(_BoomError),
        held_lease(state_dir=state_dir, now=lambda: _dt(2026, 1, 1), psutil_module=fake),
    ):
        assert lease_path(state_dir).exists()
        raise _BoomError("simulated DB command failure")

    assert not lease_path(state_dir).exists(), (
        "the lease must be released even though the wrapped body raised"
    )


def test_lease_path_is_scoped_per_pid(tmp_path) -> None:
    """OWN RULING (see module docstring): Given two DIFFERENT PIDs.
    When each acquires its own lease.
    Then they land at two DIFFERENT files — a second concurrent invocation's
    lease must never overwrite a first, still-live one."""
    state_dir = tmp_path / "state"
    fake = _fake_psutil(
        **{
            111: _FakeProcess(111, create_time=1.0),
            222: _FakeProcess(222, create_time=2.0),
        }
    )
    acquire_lease(state_dir=state_dir, pid=111, now=lambda: _dt(2026, 1, 1), psutil_module=fake)
    acquire_lease(state_dir=state_dir, pid=222, now=lambda: _dt(2026, 1, 1), psutil_module=fake)

    assert lease_path(state_dir, pid=111) != lease_path(state_dir, pid=222)
    assert lease_path(state_dir, pid=111).exists()
    assert lease_path(state_dir, pid=222).exists()
    assert set(lease_paths(state_dir)) == {
        lease_path(state_dir, pid=111),
        lease_path(state_dir, pid=222),
    }


# ---------------------------------------------------------------------------
# DTOs are frozen (mirrors lifecycle.py's own Instance/UnitState/DownResult).
# ---------------------------------------------------------------------------


def test_lease_and_idle_decision_dataclasses_are_frozen() -> None:
    lease = Lease(pid=1, create_time=1.0, acquired_utc="2026-01-01T00:00:00Z")
    with pytest.raises(FrozenInstanceError):
        lease.pid = 2  # type: ignore[misc]

    decision = IdleDecision(should_stop=False, reason=REASON_FRESH_STAMP)
    with pytest.raises(FrozenInstanceError):
        decision.should_stop = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# C-4 — a live lease blocks the stop, UNCONDITIONALLY, even with a stale
# stamp. THE property test: proves the DECISION, not merely "a file exists".
# ---------------------------------------------------------------------------


def test_live_lease_blocks_stop_even_with_a_very_stale_stamp(tmp_path) -> None:
    """C-4: Given a lease naming the REAL, currently-running pytest process
    (genuinely alive, real psutil, no injected fake) AND an activity stamp
    that is WAY older than the idle timeout (would, on its own, demand a
    stop).
    When `evaluate_idle` runs.
    Then `should_stop` is False, `reason == REASON_LIVE_LEASE` — the live
    lease wins UNCONDITIONALLY over the stale stamp, exactly as C-4 requires
    ("even if the stamp is stale").
    """
    state_dir = tmp_path / "state"
    pid = os.getpid()
    acquire_lease(state_dir=state_dir, pid=pid)
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))  # decades stale

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 12, 0, 0),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_LIVE_LEASE)


def test_recycled_pid_is_not_mistaken_for_a_live_lease(tmp_path) -> None:
    """C-4: "pin how identity is established — a recycled PID must not be
    mistaken for a live lease". Given a lease recorded for PID 555 at
    create_time=1000.0, and NOW a DIFFERENT process happens to hold PID 555,
    with a DIFFERENT create_time (5000.0) — the recycled-PID scenario.
    When `evaluate_idle` runs against a stale stamp.
    Then the lease is treated as DEAD (not live): `should_stop` is True,
    `reason == REASON_STALE`, and the stale lease file is CLEANED (removed).
    A naive `psutil.pid_exists(pid)`-only check would wrongly call this
    lease live forever.
    """
    state_dir = tmp_path / "state"
    acquire_lease(
        state_dir=state_dir, pid=555, now=lambda: _dt(2026, 1, 1),
        psutil_module=_fake_psutil(**{555: _FakeProcess(555, create_time=1000.0)}),
    )
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    recycled = _fake_psutil(**{555: _FakeProcess(555, create_time=5000.0)})
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=recycled,
    )
    assert decision == IdleDecision(should_stop=True, reason=REASON_STALE)
    assert not lease_path(state_dir, pid=555).exists(), "a recycled-PID lease must be cleaned"


def test_dead_lease_no_such_process_is_cleaned_and_falls_through_to_stamp(tmp_path) -> None:
    """C-5: Given a well-formed lease whose PID no longer exists at all.
    When `evaluate_idle` runs against a stale stamp.
    Then the decision falls through to the (stale) stamp: `should_stop` is
    True, `reason == REASON_STALE`, and the dead lease file is removed."""
    state_dir = tmp_path / "state"
    acquire_lease(
        state_dir=state_dir, pid=777, now=lambda: _dt(2026, 1, 1),
        psutil_module=_fake_psutil(**{777: _FakeProcess(777, create_time=1.0)}),
    )
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    dead = _fake_psutil()  # 777 not present -> psutil.Process(777) raises NoSuchProcess
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=dead,
    )
    assert decision == IdleDecision(should_stop=True, reason=REASON_STALE)
    assert not lease_path(state_dir, pid=777).exists()


def test_zombie_process_is_treated_as_dead_via_the_real_psutil_exception_hierarchy(
    tmp_path,
) -> None:
    """C-5 [decode-before-hypothesise]: confirmed directly against the
    REAL, installed psutil interpreter that `psutil.ZombieProcess` IS a
    subclass of `psutil.NoSuchProcess` (not assumed from memory). Given a
    lease whose process lookup raises the REAL `psutil.ZombieProcess`.
    When `evaluate_idle` runs against a stale stamp.
    Then it is treated exactly like NoSuchProcess: DEAD, cleaned, falls
    through to the stale stamp — proving the implementation's except clause
    genuinely catches this real exception type (a narrower except that only
    matched a literal `NoSuchProcess` instance would NOT catch this)."""
    state_dir = tmp_path / "state"
    acquire_lease(
        state_dir=state_dir, pid=888, now=lambda: _dt(2026, 1, 1),
        psutil_module=_fake_psutil(**{888: _FakeProcess(888, create_time=1.0)}),
    )
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    zombie = _fake_psutil(
        **{888: _FakeProcess(888, create_time=None, error=psutil.ZombieProcess(888))}
    )
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=zombie,
    )
    assert decision == IdleDecision(should_stop=True, reason=REASON_STALE)
    assert not lease_path(state_dir, pid=888).exists()


def test_access_denied_is_undetermined_not_dead_and_the_lease_is_kept(tmp_path) -> None:
    """OWN RULING (module docstring): Given a well-formed lease whose PID
    exists, but reading its create_time raises the REAL
    `psutil.AccessDenied` (distinct from a clean NoSuchProcess — we KNOW a
    process holds that PID, we just cannot verify it is the SAME one).
    When `evaluate_idle` runs against a stale stamp.
    Then it does NOT fall through to stale: `should_stop` is False,
    `reason == REASON_UNDETERMINED_LEASE`, and the lease file is left ON
    DISK — never deleted, since it was never confirmed dead. This is the
    discriminating case a broad `except Exception` catch-all around
    NoSuchProcess/ZombieProcess alone would get WRONG only if it conflated
    "confirmed gone" with "cannot tell" — this test would catch that."""
    state_dir = tmp_path / "state"
    acquire_lease(
        state_dir=state_dir, pid=999, now=lambda: _dt(2026, 1, 1),
        psutil_module=_fake_psutil(**{999: _FakeProcess(999, create_time=1.0)}),
    )
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    denied = _fake_psutil(
        **{999: _FakeProcess(999, create_time=None, error=psutil.AccessDenied(999))}
    )
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=denied,
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_UNDETERMINED_LEASE)
    assert lease_path(state_dir, pid=999).exists(), (
        "an undetermined lease must never be deleted — it was never confirmed dead"
    )


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json", id="not_json"),
        pytest.param('{"pid": "not-an-int"}', id="wrong_pid_type"),
        pytest.param('{"pid": 123}', id="missing_create_time"),
        pytest.param('{"pid": 123, "create_time": "nope"}', id="wrong_create_time_type"),
        pytest.param('{"pid": -1, "create_time": 1.0}', id="negative_pid"),
    ],
)
def test_malformed_lease_file_is_undetermined_and_left_untouched(tmp_path, raw: str) -> None:
    """OWN RULING: Given a lease file that exists but cannot be parsed as a
    well-formed lease.
    When `evaluate_idle` runs against a stale stamp.
    Then the decision is `should_stop=False, reason=REASON_UNDETERMINED_LEASE`
    (never silently treated as "no lease" nor as "confirmed dead"), and the
    malformed file is left on disk untouched."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    lease_path(state_dir, pid=1).parent.mkdir(parents=True, exist_ok=True)
    lease_path(state_dir, pid=1).write_text(raw, encoding="utf-8")
    # Rename to a plausible lease filename regardless of the (possibly
    # malformed) pid inside — evaluate_idle discovers lease files by GLOB,
    # not by trusting their own content for the filename.
    malformed_path = state_dir / "activity_lease.1.json"
    malformed_path.write_text(raw, encoding="utf-8")
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_UNDETERMINED_LEASE)
    assert malformed_path.exists()


def test_two_concurrent_leases_one_dead_one_live_still_blocks_and_cleans_only_the_dead_one(
    tmp_path,
) -> None:
    """OWN RULING (module docstring, PID-scoped leases): Given TWO leases
    from two different invocations: PID 111 (long dead) and PID 222 (alive,
    matching create_time).
    When `evaluate_idle` runs against a stale stamp.
    Then the decision still blocks the stop (`REASON_LIVE_LEASE`) because of
    222 — 111 being dead must not let the stop through — AND only 111's
    lease file is cleaned; 222's stays exactly because it is still live."""
    state_dir = tmp_path / "state"
    fake = _fake_psutil(**{222: _FakeProcess(222, create_time=2.0)})
    acquire_lease(
        state_dir=state_dir, pid=111, now=lambda: _dt(2026, 1, 1),
        psutil_module=_fake_psutil(**{111: _FakeProcess(111, create_time=1.0)}),
    )
    acquire_lease(state_dir=state_dir, pid=222, now=lambda: _dt(2026, 1, 1), psutil_module=fake)
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=fake,  # 111 not present in `fake` -> NoSuchProcess
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_LIVE_LEASE)
    assert not lease_path(state_dir, pid=111).exists(), "the dead lease must be cleaned"
    assert lease_path(state_dir, pid=222).exists(), "the live lease must survive"


def test_no_lease_file_at_all_falls_through_cleanly(tmp_path) -> None:
    """Given no lease file exists at all (the ordinary case for most runs).
    When `evaluate_idle` runs against a stale stamp.
    Then the decision falls through to the stamp: should_stop True,
    reason=stale — proving "absent" and "confirmed dead" reach the same
    outcome without a spurious warning or crash either way."""
    state_dir = tmp_path / "state"
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=True, reason=REASON_STALE)


# ---------------------------------------------------------------------------
# C-9 — the escape hatch: idle_timeout_minutes <= 0 is a total no-op, and
# proven as a PROPERTY (zero psutil interaction), not merely "returns False".
# ---------------------------------------------------------------------------


class _ForbidPsutil:
    NoSuchProcess = psutil.NoSuchProcess
    ZombieProcess = psutil.ZombieProcess
    AccessDenied = psutil.AccessDenied

    def Process(self, pid):  # noqa: N802 — mirrors psutil's own method name
        raise AssertionError(
            f"psutil.Process({pid!r}) must never be called while idle-stop is disabled"
        )


@pytest.mark.parametrize("timeout", [0.0, -1.0, -0.001, -1_000_000.0])
def test_zero_or_negative_timeout_disables_with_zero_further_io(tmp_path, timeout: float) -> None:
    """C-9: Given `idle_timeout_minutes <= 0` (the parsed escape hatch) AND a
    lease/stamp scenario that would OTHERWISE demand a stop (a live lease
    AND a stale stamp both present).
    When `evaluate_idle` runs.
    Then it returns `should_stop=False, reason=REASON_DISABLED` — the
    escape hatch overrides everything else, even a scenario that would
    otherwise be unambiguous — and it never even calls `psutil.Process`
    (proven via a fake that raises `AssertionError` if it is ever invoked),
    proving the "no-op" property, not merely the observable outcome.
    """
    state_dir = tmp_path / "state"
    acquire_lease(
        state_dir=state_dir, pid=os.getpid(), now=lambda: _dt(2026, 1, 1),
        psutil_module=_fake_psutil(**{os.getpid(): _FakeProcess(os.getpid(), create_time=1.0)}),
    )
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=timeout,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=_ForbidPsutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_DISABLED)


# ---------------------------------------------------------------------------
# C-6 / C-7 — fresh vs. stale stamp, including the exact boundary.
# ---------------------------------------------------------------------------


def test_fresh_stamp_well_within_timeout_does_not_stop(tmp_path) -> None:
    state_dir = tmp_path / "state"
    touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 7, 28, 12, 0, 0))
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 12, 5, 0),  # 5 minutes old
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_FRESH_STAMP)


def test_stamp_age_just_under_the_timeout_boundary_is_still_fresh(tmp_path) -> None:
    state_dir = tmp_path / "state"
    touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 7, 28, 12, 0, 0))
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 12, 0, 0) + timedelta(minutes=30, seconds=-1),
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_FRESH_STAMP)


def test_stamp_age_exactly_at_the_timeout_boundary_is_stale(tmp_path) -> None:
    """Pins the boundary operator precisely: "younger than the timeout" means
    stale is >= the timeout, not > — an age exactly equal to the configured
    budget must already be treated as stale, matching C-6's own wording
    ("Younger than ... -> stop nothing")."""
    state_dir = tmp_path / "state"
    touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 7, 28, 12, 0, 0))
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 12, 0, 0) + timedelta(minutes=30),
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=True, reason=REASON_STALE)


def test_stale_stamp_well_past_the_timeout_stops(tmp_path) -> None:
    state_dir = tmp_path / "state"
    touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 7, 28, 12, 0, 0))
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 14, 0, 0),  # 2 hours old
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=True, reason=REASON_STALE)


def test_default_idle_timeout_minutes_constant_is_thirty() -> None:
    """C-6: "default 30" — pinned as a real constant, not a magic literal
    scattered across callers."""
    assert DEFAULT_IDLE_TIMEOUT_MINUTES == 30.0
    assert isinstance(DEFAULT_IDLE_TIMEOUT_MINUTES, float)
    assert math.isfinite(DEFAULT_IDLE_TIMEOUT_MINUTES)
    assert DEFAULT_IDLE_TIMEOUT_MINUTES > 0


# ---------------------------------------------------------------------------
# C-10 — clock skew: a stamp in the future is just-active, never idle. Also
# proves no crash on an extreme skew.
# ---------------------------------------------------------------------------


def test_future_stamp_is_treated_as_just_active_never_idle(tmp_path) -> None:
    """C-10: Given the activity stamp is (per clock skew) an hour in the
    FUTURE relative to `now`.
    When `evaluate_idle` runs with a much shorter idle timeout.
    Then it is treated as FRESH (age clamped to zero, never negative), never
    as stale — the opposite of the correct direction would let a clock-skewed
    stamp look ancient and trigger a stop."""
    state_dir = tmp_path / "state"
    now_value = _dt(2026, 7, 28, 12, 0, 0)
    future_stamp = now_value + timedelta(hours=1)
    touch_activity(state_dir=state_dir, now=lambda: future_stamp)

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=5.0,
        db_reachable=True,
        now=lambda: now_value,
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_FRESH_STAMP)


def test_extremely_future_stamp_does_not_crash(tmp_path) -> None:
    """C-10: An extreme clock-skew stamp (decades in the future) must not
    raise (e.g. no OverflowError from a giant timedelta)."""
    state_dir = tmp_path / "state"
    touch_activity(state_dir=state_dir, now=lambda: _dt(2100, 1, 1))
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=_fake_psutil(),
    )
    assert decision.should_stop is False


# ---------------------------------------------------------------------------
# C-8 — no stamp at all: bootstrap on first observed reachability, protect
# for a FULL budget window, THEN stale. Proven as a two-call sequence
# sharing real on-disk state — the actual property, not a single-call proxy.
# ---------------------------------------------------------------------------


def test_no_stamp_and_db_not_reachable_is_nothing_to_do_and_writes_no_stamp(tmp_path) -> None:
    """C-8: Given a fresh install — no stamp, no lease, and the database is
    NOT currently reachable (nothing running at all).
    When `evaluate_idle` runs.
    Then `should_stop=False, reason=REASON_NOTHING_TO_DO` — there is nothing
    to protect and nothing to stop — and (property) no stamp file is
    fabricated for a database that was never even up."""
    state_dir = tmp_path / "state"
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=False,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_NOTHING_TO_DO)
    assert not activity_stamp_path(state_dir).exists()


def test_no_stamp_and_db_reachable_bootstraps_a_stamp_and_does_not_stop_yet(tmp_path) -> None:
    """C-8: Given no stamp exists, but the caller reports the database
    reachable RIGHT NOW (e.g. an operator ran `partgraph db up` by hand, or
    the quadlet unit started it — `partgraph` never touched it before).
    When `evaluate_idle` runs.
    Then it does NOT stop (`REASON_STAMP_BOOTSTRAPPED`) and a real activity
    stamp is written, anchored at `now` — this observation itself becomes
    the "first seen" instant the operator's own suggested grace window
    counts from."""
    state_dir = tmp_path / "state"
    t0 = _dt(2026, 7, 28, 9, 0, 0)
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: t0,
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_STAMP_BOOTSTRAPPED)
    assert read_activity_stamp(state_dir) == t0


def test_no_stamp_bootstrap_protects_for_a_full_budget_window_then_goes_stale(tmp_path) -> None:
    """C-8 — THE property test (two real, sequential calls sharing on-disk
    state, not a single-call proxy). Given call #1 bootstraps at T0 (no
    stamp, db_reachable=True, as above).
    When call #2 runs at T0 + (timeout - 1 minute) — still WITHIN the full
    budget window since the first observation.
    Then it is still protected (fresh, from the bootstrapped stamp).
    When call #3 runs at T0 + timeout — the FULL budget window has now
    elapsed since PartGraph first observed the database reachable.
    Then it is stale and `should_stop=True` — proving the operator's own
    suggested rule end-to-end, not merely that a stamp got written."""
    state_dir = tmp_path / "state"
    t0 = _dt(2026, 7, 28, 9, 0, 0)
    timeout_minutes = 30.0

    call1 = evaluate_idle(
        state_dir=state_dir, idle_timeout_minutes=timeout_minutes, db_reachable=True,
        now=lambda: t0, psutil_module=_fake_psutil(),
    )
    assert call1.reason == REASON_STAMP_BOOTSTRAPPED

    within_window = t0 + timedelta(minutes=timeout_minutes - 1)
    call2 = evaluate_idle(
        state_dir=state_dir, idle_timeout_minutes=timeout_minutes, db_reachable=True,
        now=lambda: within_window, psutil_module=_fake_psutil(),
    )
    assert call2 == IdleDecision(should_stop=False, reason=REASON_FRESH_STAMP), (
        "must still be protected before the full budget window elapses"
    )

    window_elapsed = t0 + timedelta(minutes=timeout_minutes)
    call3 = evaluate_idle(
        state_dir=state_dir, idle_timeout_minutes=timeout_minutes, db_reachable=True,
        now=lambda: window_elapsed, psutil_module=_fake_psutil(),
    )
    assert call3 == IdleDecision(should_stop=True, reason=REASON_STALE), (
        "must go stale once a FULL budget window has elapsed since the first observation"
    )
