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

IMPLEMENTED. `src/partgraph/util/activity.py` exists and this file collects
and runs normally now. The paragraph above ("Pinned contract this file
specifies") is a live, present-tense inventory; this one is history, kept
for context rather than because it is still true: this file ORIGINALLY
collected with every test below erroring with `ModuleNotFoundError` (its
own top-level `from partgraph.util.activity import (...)`), mirroring
`tests/unit/test_cli_db_down.py`'s own documented pre-PR-A history for
`partgraph.util.lifecycle` — the correct test-first RED state PR-C started
from, not a claim about where it stands today.

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
    `REASON_STAMP_BOOTSTRAPPED`, `REASON_STAMP_POISON_RECOVERED`,
    `REASON_STAMP_UNRECORDABLE` — plain string tags (mirrors
    `partgraph.util.lifecycle`'s own `_OWNER_NAME_MATCH = "S1"` style),
    naming WHY an `IdleDecision` was reached, so a test can assert the
    reason, not merely the boolean. This inventory is enforced, not merely
    hand-maintained: `test_activity_architecture.py`'s
    `test_reason_tags_docstring_inventory_matches_the_modules_actual_reason_constants`
    diffs the `REASON_*` tokens listed here against
    `partgraph.util.activity.__all__`'s actual set, so it cannot silently
    go stale the way it did once already — `REASON_STAMP_UNRECORDABLE`
    landed and this exact list was not updated to match, and nothing went
    red, because prose is not an assertion.

    `STAMP_FUTURE_POISON_CEILING_MINUTES: float` — see "[Gate 3a BLOCKING]
    the stamp-poisoning ceiling" below.

    `MAX_STAMP_FILE_BYTES` / `MAX_LEASE_FILE_BYTES` — see "[Gate 3a
    SHOULD-FIX] size/shape bounds on stamp and lease reads" below.

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
    `touch_activity(*, state_dir, now=None) -> bool` — True iff the durable
    record is trustworthy once the call returns (a landed write, or a
    write correctly declined under monotonic protection because the stamp
    already on disk is at least as recent); False iff a write was owed and
    did not land. Originally `-> None`, silently discarding the boolean
    `_atomic_write` (below) already computes — see
    `REASON_STAMP_UNRECORDABLE` further down.
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

[Gate 3a BLOCKING] the stamp-poisoning ceiling — a forward clock jump must
not permanently poison the stamp. The ORIGINAL "monotonic-safe" rule below
("never write earlier than the existing stamp") was reviewed and found to
have exactly the failure mode Gate 3a flagged: a SINGLE bad `now()` (a real
NTP/RTC correction, or a caller bug) that lands far in the future gets
written once, and every subsequent, CORRECT `now()` is then permanently
refused by that same rule forever — `evaluate_idle` sees a stamp that reads
as "just active" forever (C-10's own clamp), `db idle-stop` becomes a
silent, permanent no-op, and the idle cost this PR exists to eliminate
returns invisibly. "Silent and permanent" is unacceptable for the same
reason the original 14-hour incident was: silence is exactly how it
persisted.

THE FIX, applied identically at both the ONLY two places a stamp's
plausibility is judged against a clock (`touch_activity`'s own write-time
protection AND `evaluate_idle`'s own C-10 read-time clamp): a stored stamp
that is more than `STAMP_FUTURE_POISON_CEILING_MINUTES` AHEAD of a genuine
`now()` is no longer treated as "legitimately more advanced than this
write" (the ordinary small-clock-skew case C-10 protects) — it is treated
as UNTRUSTWORTHY:
  - `touch_activity`: the monotonic-protection rule no longer applies to an
    untrustworthy existing stamp — the new, correct `now()` overwrites it
    immediately (self-heals on the VERY NEXT legitimate write, not after
    literal wall-clock time catches up to the poisoned value, which for an
    extreme poisoning could be centuries away).
  - `evaluate_idle`: an untrustworthy stamp is treated EXACTLY as if no
    stamp existed at all, routing through the SAME, already-tested C-8
    no-stamp logic — `db_reachable=True` bootstraps a fresh, correct stamp
    immediately (self-healing on `db idle-stop`'s OWN independent schedule,
    without requiring any OTHER DB command to run first) and reports
    `REASON_STAMP_POISON_RECOVERED` — a DISTINCT tag from the ordinary
    first-install `REASON_STAMP_BOOTSTRAPPED`, so the anomaly is
    OBSERVABLE (surfaceable by a future `db doctor`/log line) rather than
    silently indistinguishable from a fresh install; `db_reachable=False`
    reports `REASON_NOTHING_TO_DO` and leaves the poisoned file untouched
    (nothing is running, so there is no urgency, and fabricating a stamp
    for a database that is not up would violate the very property
    `test_no_stamp_and_db_not_reachable_is_nothing_to_do_and_writes_no_stamp`
    below already pins).

`STAMP_FUTURE_POISON_CEILING_MINUTES` is a JUDGEMENT CALL (mirrors
`STOP_GRACE_SECONDS`/`AUTOSTART_READY_TIMEOUT_S`'s own documented
precedent), set to 10.0: it must be (a) far larger than any plausible
ordinary clock skew during normal operation (sub-second to low-single-digit
seconds across cores/hosts, so 10 minutes is enormous headroom against a
false positive that would fight a genuine, small NTP backward correction —
`test_touch_activity_never_regresses_the_stamp_on_a_small_backward_clock_
step_within_the_poison_ceiling` below), and (b) meaningfully smaller than
`DEFAULT_IDLE_TIMEOUT_MINUTES` (30) so the worst-case damage of ANY
poisoning event — "the stamp looks fresh for up to the ceiling" — stays
well under one full idle-timeout cycle, not an unbounded one.

[Gate 3a SHOULD-FIX] the shared stamp's temp-filename is NOT made unique
per-writer, unlike the lease (which IS PID-scoped, above) — disclosed here
with the SAME rigour, as requested, rather than fixed, because the fail
direction was traced and found SAFE: two processes racing on the same
`activity.json.tmp` can, in the worst case, interleave a torn/partial write
that leaves malformed JSON at the final path once `os.replace` completes.
`read_activity_stamp` already degrades a malformed stamp to `None`
(`test_read_activity_stamp_degrades_to_none_never_raises_on_malformed_
content` below, unconditionally, not only for this scenario), which routes
`evaluate_idle` through the SAME no-stamp/C-8 bootstrap-or-nothing-to-do
branch a torn write's honest sibling — no stamp at all — already goes
through. The worst outcome of a torn shared-stamp write is therefore
IDENTICAL to a fresh install's own first observation, never a false "must
not stop" nor a false "must stop" — unlike the lease, where a lost update
could have hidden a genuinely LIVE process, a torn STAMP write cannot hide
a live lease (the two files are independent), so the stakes are lower here
and a shared temp name is an accepted, analysed trade-off rather than an
oversight.

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
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import psutil
import pytest

# This import is expected to raise ModuleNotFoundError until
# src/partgraph/util/activity.py exists — the correct test-first red state.
from partgraph.util.activity import (  # noqa: E402
    DEFAULT_IDLE_TIMEOUT_MINUTES,
    MAX_LEASE_FILE_BYTES,
    MAX_STAMP_FILE_BYTES,
    REASON_DISABLED,
    REASON_FRESH_STAMP,
    REASON_LIVE_LEASE,
    REASON_NOTHING_TO_DO,
    REASON_STALE,
    REASON_STAMP_BOOTSTRAPPED,
    REASON_STAMP_POISON_RECOVERED,
    REASON_UNDETERMINED_LEASE,
    STAMP_FUTURE_POISON_CEILING_MINUTES,
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


def _dt(  # noqa: PLR0913, PLR0917 — a fixed-instant builder needs one arg per field.
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


def _fake_psutil(by_pid: dict[int, _FakeProcess] | None = None) -> _FakePsutilModule:
    """[Gate 3b defect 1 fix] Takes *by_pid* POSITIONALLY, never via `**`
    unpacking: `_fake_psutil({4242: proc})` is rejected by CPython at the
    call site itself ("keywords must be strings"), before any implementation
    code ever runs — an integer dict key can never be a keyword argument
    name. `_FakePsutilModule` already accepts a plain mapping, so passing it
    positionally is both the fix and the simpler call shape.
    """
    return _FakePsutilModule(by_pid or {})


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


def test_touch_activity_never_regresses_the_stamp_on_a_small_backward_clock_step_within_the_poison_ceiling(
    tmp_path,
) -> None:
    """C-1 "monotonic-safe": Given a stamp already recorded at T2, and a
    SMALL backward step — well WITHIN `STAMP_FUTURE_POISON_CEILING_MINUTES`
    (2 minutes, against a 10-minute ceiling) — a real hazard: NTP stepping
    the wall clock backward mid-run, or two heartbeats completing out of
    order across cores.
    When `touch_activity` is called again with `now` returning the earlier
    instant T1.
    Then the stamp on disk is UNCHANGED — still T2 — never regressed to T1.
    A regression here would make a database that WAS recently confirmed
    active suddenly look OLDER (more idle) than it already was recorded as,
    which is exactly the wrong direction for a control that guards against
    stopping something in use. [Gate 3a BLOCKING fix] This is deliberately
    a SMALL gap now, not the 1-hour gap an earlier draft used — see
    `test_touch_activity_self_heals_a_stamp_poisoned_implausibly_far_into_
    the_future` immediately below for what happens BEYOND the ceiling,
    where protecting the existing stamp forever is exactly the failure mode
    Gate 3a flagged.
    """
    state_dir = tmp_path / "state"
    t2 = _dt(2026, 7, 28, 12, 0, 0)
    t1 = t2 - timedelta(minutes=2)
    assert t1 < t2
    assert (t2 - t1) < timedelta(minutes=STAMP_FUTURE_POISON_CEILING_MINUTES)

    touch_activity(state_dir=state_dir, now=lambda: t2)
    touch_activity(state_dir=state_dir, now=lambda: t1)

    assert read_activity_stamp(state_dir) == t2, (
        "a SMALL backward clock step must not regress the stamp"
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


# ---------------------------------------------------------------------------
# [Gate 3a BLOCKING] the stamp-poisoning ceiling and its recoverability —
# see the module docstring's own full explanation.
# ---------------------------------------------------------------------------


def test_stamp_future_poison_ceiling_is_a_finite_positive_minutes_value_below_the_default_timeout() -> None:
    """[Gate 3a BLOCKING] Given the ceiling is a JUDGEMENT CALL (module
    docstring), not a measured requirement.
    When the constant is read directly.
    Then it is finite, positive, and STRICTLY LESS than
    `DEFAULT_IDLE_TIMEOUT_MINUTES` — the whole point is bounding a
    poisoning's worst-case damage to well under one full idle cycle, which
    a ceiling AT OR ABOVE the default timeout could not do."""
    assert math.isfinite(STAMP_FUTURE_POISON_CEILING_MINUTES)
    assert STAMP_FUTURE_POISON_CEILING_MINUTES > 0
    assert STAMP_FUTURE_POISON_CEILING_MINUTES < DEFAULT_IDLE_TIMEOUT_MINUTES


def test_touch_activity_self_heals_a_stamp_poisoned_implausibly_far_into_the_future(
    tmp_path,
) -> None:
    """[Gate 3a BLOCKING — THE property test]: Given a stamp poisoned two
    days into the future relative to the honest wall clock (modelling a
    real RTC/NTP fault, or a caller passing a wrong `now` once).
    When `touch_activity` is called again with a CORRECT, ordinary `now()`.
    Then the poisoned stamp is OVERWRITTEN with the correct value — self-
    healing on the very next legitimate write, rather than being protected
    forever by the monotonic rule (which would otherwise silently and
    permanently disable this whole feature — the exact failure Gate 3a
    flagged)."""
    state_dir = tmp_path / "state"
    honest_now = _dt(2026, 7, 28, 12, 0, 0)
    poisoned = honest_now + timedelta(days=2)
    assert (poisoned - honest_now) > timedelta(minutes=STAMP_FUTURE_POISON_CEILING_MINUTES)

    touch_activity(state_dir=state_dir, now=lambda: poisoned)
    assert read_activity_stamp(state_dir) == poisoned  # sanity: the poison landed

    touch_activity(state_dir=state_dir, now=lambda: honest_now)
    assert read_activity_stamp(state_dir) == honest_now, (
        "a stamp poisoned beyond the ceiling must self-heal on the next "
        "legitimate write, never be protected forever"
    )


def test_touch_activity_poison_ceiling_boundary_exactly_at_ceiling_still_protected(
    tmp_path,
) -> None:
    """Pins the boundary operator precisely: a gap of EXACTLY the ceiling is
    still protected (not yet poisoned) — the ceiling is exceeded strictly,
    giving a small extra safety margin before declaring poison."""
    state_dir = tmp_path / "state"
    honest_now = _dt(2026, 7, 28, 12, 0, 0)
    at_ceiling = honest_now + timedelta(minutes=STAMP_FUTURE_POISON_CEILING_MINUTES)

    touch_activity(state_dir=state_dir, now=lambda: at_ceiling)
    touch_activity(state_dir=state_dir, now=lambda: honest_now)

    assert read_activity_stamp(state_dir) == at_ceiling, (
        "a gap of exactly the ceiling must still be protected, not treated as poisoned"
    )


def test_touch_activity_poison_ceiling_boundary_just_over_ceiling_self_heals(
    tmp_path,
) -> None:
    """Mirrors the boundary test above from the other side: a gap ONE
    MINUTE beyond the ceiling is treated as poisoned and self-heals."""
    state_dir = tmp_path / "state"
    honest_now = _dt(2026, 7, 28, 12, 0, 0)
    just_over = honest_now + timedelta(minutes=STAMP_FUTURE_POISON_CEILING_MINUTES + 1)

    touch_activity(state_dir=state_dir, now=lambda: just_over)
    touch_activity(state_dir=state_dir, now=lambda: honest_now)

    assert read_activity_stamp(state_dir) == honest_now, (
        "a gap just beyond the ceiling must be treated as poisoned and self-heal"
    )


def test_touch_activity_warns_once_and_never_raises_when_rename_fails(
    tmp_path, monkeypatch, caplog
) -> None:
    """C-14: Given the state dir cannot actually be written (the final
    `os.replace` fails — e.g. a full disk, or a read-only mount).
    When `touch_activity` is called TWICE in a row (modelling a paginated
    command's per-page heartbeat, C-2, hammering an unwritable state dir for
    a whole run).
    Then NEITHER call raises — the DB command this stamp is a side effect of
    must never crash because its own bookkeeping failed — exactly ONE
    warning is logged for the whole pair, not two (a multi-hour run must not
    flood the log with an identical warning every single page) — and [Gate
    3a SHOULD-FIX] the state dir is left CLEAN afterward: no `.tmp` debris
    file survives a failed `os.replace`, even though the temp file's own
    `write_text` genuinely succeeded before the rename failed.
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
    leftover = list(state_dir.glob("*.tmp")) if state_dir.exists() else []
    assert leftover == [], (
        f"a failed os.replace must not leave a .tmp debris file behind: {leftover!r}"
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


# ---------------------------------------------------------------------------
# `touch_activity`'s return-value contract (honesty fix — landed).
# `_atomic_write`'s own docstring already promised "Return True iff it
# landed" (see its definition above), but `touch_activity` discarded that
# boolean and returned `None` unconditionally — the outcome was not merely
# ignored, it was UNOBSERVABLE one frame up. Before this section was added,
# no test anywhere in this suite asserted `touch_activity(...)`'s return
# value (confirmed by inspection: `grep -n "touch_activity(" tests/` showed
# every call site as a bare statement); the ONLY place `-> None` had
# appeared was this file's own module-docstring "Pinned contract" prose near
# the top (now updated to `-> bool` alongside this fix), which was
# documentation, never an executable assertion. Widening the return type to
# `bool` therefore weakened no existing pin, and every real caller
# (`src/partgraph/cli.py`'s nine-plus `touch_activity(state_dir=...)` call
# sites) already discards the return value as a bare expression statement,
# so this was additive, not breaking.
# ---------------------------------------------------------------------------


def test_touch_activity_returns_true_when_the_write_lands(tmp_path) -> None:
    """Given an ordinary, writable state dir.
    When `touch_activity` is called.
    Then it returns True — the write genuinely landed, threading
    `_atomic_write`'s own "Return True iff it landed" promise all the way up
    to `touch_activity`'s caller, its only consumer inside this module."""
    state_dir = tmp_path / "state"
    assert touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 7, 28)) is True


def test_touch_activity_returns_false_when_os_replace_fails(tmp_path, monkeypatch) -> None:
    """Given the final `os.replace` step of the atomic write fails (a full
    disk, a read-only mount, or — the scenario this whole fix exists for — a
    state dir that turned root-owned or read-only after a stray `sudo`).
    When `touch_activity` is called.
    Then it does not raise (the existing C-14 contract, already proven above
    by `test_touch_activity_warns_once_and_never_raises_when_rename_fails`)
    AND it returns False — not None, not True — so a caller can finally tell
    "I tried and it did not land" apart from "it landed", which
    `_stamp_decision` (exercised further below) needs in order to stop
    asserting a write that never happened."""
    state_dir = tmp_path / "state"
    monkeypatch.setattr(
        os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated: disk full"))
    )
    assert touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 7, 28)) is False


def test_touch_activity_returns_false_when_the_state_dir_cannot_be_created(
    tmp_path, monkeypatch
) -> None:
    """Mirrors the test above for the OTHER OSError site inside
    `_atomic_write` (`directory.mkdir(...)` failing, rather than
    `os.replace`) — both failure sites must report the same False, not only
    one of the two."""
    state_dir = tmp_path / "state"
    monkeypatch.setattr(
        pathlib.Path,
        "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("simulated: permission denied")),
    )
    assert touch_activity(state_dir=state_dir, now=lambda: _dt(2026, 7, 28)) is False


def test_touch_activity_returns_true_when_a_write_is_correctly_skipped_by_monotonic_protection(
    tmp_path,
) -> None:
    """Given a stamp already recorded at T2, and a call with an OLDER `now`
    T1 well within the poison ceiling — the "protected, no write needed"
    branch (a SUCCESS, not a failure: the durable state on disk is already
    correct and current).
    When `touch_activity` is called.
    Then it still returns True — "landed" is read as "the durable record is
    fine after this call returns", never "a write literally executed just
    now". A naive implementation that returns False whenever
    `_atomic_write` itself was never invoked would wrongly report the
    monotonic-protection guard — an intentional, correct no-op — as an
    unrecordable failure, which would be the wrong direction one layer up
    for every OTHER `touch_activity` call site in `partgraph.cli` that this
    change also touches."""
    state_dir = tmp_path / "state"
    t2 = _dt(2026, 7, 28, 12, 0, 0)
    t1 = t2 - timedelta(minutes=2)
    touch_activity(state_dir=state_dir, now=lambda: t2)

    assert touch_activity(state_dir=state_dir, now=lambda: t1) is True
    assert read_activity_stamp(state_dir) == t2, "sanity: the protection itself still held"


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
        psutil_module=_fake_psutil({4242: _FakeProcess(4242, create_time=100.0)}),
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
        psutil_module=_fake_psutil({111: _FakeProcess(111, create_time=1.0)}),
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
    fake = _fake_psutil({os.getpid(): _FakeProcess(os.getpid(), create_time=1.0)})
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
    fake = _fake_psutil({os.getpid(): _FakeProcess(os.getpid(), create_time=1.0)})

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
        {
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
        psutil_module=_fake_psutil({555: _FakeProcess(555, create_time=1000.0)}),
    )
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    recycled = _fake_psutil({555: _FakeProcess(555, create_time=5000.0)})
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
        psutil_module=_fake_psutil({777: _FakeProcess(777, create_time=1.0)}),
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
        psutil_module=_fake_psutil({888: _FakeProcess(888, create_time=1.0)}),
    )
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    zombie = _fake_psutil(
        {888: _FakeProcess(888, create_time=None, error=psutil.ZombieProcess(888))}
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
        psutil_module=_fake_psutil({999: _FakeProcess(999, create_time=1.0)}),
    )
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    denied = _fake_psutil(
        {999: _FakeProcess(999, create_time=None, error=psutil.AccessDenied(999))}
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
    fake = _fake_psutil({222: _FakeProcess(222, create_time=2.0)})
    acquire_lease(
        state_dir=state_dir, pid=111, now=lambda: _dt(2026, 1, 1),
        psutil_module=_fake_psutil({111: _FakeProcess(111, create_time=1.0)}),
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
        psutil_module=_fake_psutil({os.getpid(): _FakeProcess(os.getpid(), create_time=1.0)}),
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
# C-10 — clock skew: a stamp MODERATELY in the future (within the poison
# ceiling) is just-active, never idle. [Gate 3a BLOCKING fix] A stamp
# IMPLAUSIBLY far in the future (beyond the ceiling) is a DIFFERENT case —
# see the poisoning section below — never silently protected forever.
# ---------------------------------------------------------------------------


def test_future_stamp_within_the_poison_ceiling_is_treated_as_just_active_never_idle(
    tmp_path,
) -> None:
    """C-10: Given the activity stamp is (per ordinary clock skew) 2 minutes
    in the FUTURE relative to `now` — well WITHIN
    `STAMP_FUTURE_POISON_CEILING_MINUTES` (10).
    When `evaluate_idle` runs with a much shorter idle timeout.
    Then it is treated as FRESH (age clamped to zero, never negative), never
    as stale — the opposite of the correct direction would let a clock-skewed
    stamp look ancient and trigger a stop. [Gate 3a BLOCKING fix] Deliberately
    a SMALL skew now, not the 1-hour skew an earlier draft used — see
    `test_future_stamp_beyond_the_poison_ceiling_is_untrustworthy_and_self_
    heals_via_bootstrap` immediately below for what happens beyond the
    ceiling."""
    state_dir = tmp_path / "state"
    now_value = _dt(2026, 7, 28, 12, 0, 0)
    future_stamp = now_value + timedelta(minutes=2)
    assert (future_stamp - now_value) < timedelta(minutes=STAMP_FUTURE_POISON_CEILING_MINUTES)
    touch_activity(state_dir=state_dir, now=lambda: future_stamp)

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=5.0,
        db_reachable=True,
        now=lambda: now_value,
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_FRESH_STAMP)


# ---------------------------------------------------------------------------
# [Gate 3a BLOCKING] a stamp poisoned BEYOND the ceiling, read back by
# evaluate_idle: treated as untrustworthy, routed through the SAME no-stamp
# (C-8) logic, self-healing on idle-stop's OWN independent schedule — see
# the module docstring's full explanation.
# ---------------------------------------------------------------------------


def test_future_stamp_beyond_the_poison_ceiling_is_untrustworthy_and_self_heals_via_bootstrap(
    tmp_path,
) -> None:
    """[Gate 3a BLOCKING — THE property test]: Given a stamp poisoned an
    hour into the future (beyond the 10-minute ceiling) and the database IS
    currently reachable.
    When `evaluate_idle` runs.
    Then it does NOT silently treat the poisoned stamp as "just active"
    forever: it is treated as untrustworthy, routed through the SAME C-8
    no-stamp logic, and — because the database is reachable — a FRESH,
    correct stamp is bootstrapped immediately (self-healing on THIS
    `db idle-stop` invocation's own schedule, without needing any other DB
    command to run first). The decision is `REASON_STAMP_POISON_RECOVERED`
    — a DISTINCT tag from an ordinary first-install bootstrap, so the
    anomaly is OBSERVABLE, never silently indistinguishable from a fresh
    install. `should_stop` is False (this call heals; it does not stop)."""
    state_dir = tmp_path / "state"
    now_value = _dt(2026, 7, 28, 12, 0, 0)
    poisoned = now_value + timedelta(hours=1)
    assert (poisoned - now_value) > timedelta(minutes=STAMP_FUTURE_POISON_CEILING_MINUTES)
    touch_activity(state_dir=state_dir, now=lambda: poisoned)

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: now_value,
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_STAMP_POISON_RECOVERED)
    assert read_activity_stamp(state_dir) == now_value, (
        "the poisoned stamp must be overwritten with a fresh, correct one immediately"
    )


def test_future_stamp_beyond_the_ceiling_and_db_not_reachable_leaves_it_untouched(
    tmp_path,
) -> None:
    """Given the SAME poisoned-beyond-ceiling stamp, but the database is NOT
    currently reachable (nothing is running).
    When `evaluate_idle` runs.
    Then `REASON_NOTHING_TO_DO` (mirrors the ordinary no-stamp/not-reachable
    case exactly) and the poisoned file is left ON DISK, untouched — there
    is no urgency (nothing is running to protect or stop), and fabricating
    a stamp for a database that is not up would contradict
    `test_no_stamp_and_db_not_reachable_is_nothing_to_do_and_writes_no_stamp`'s
    own property."""
    state_dir = tmp_path / "state"
    now_value = _dt(2026, 7, 28, 12, 0, 0)
    poisoned = now_value + timedelta(hours=1)
    touch_activity(state_dir=state_dir, now=lambda: poisoned)

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=False,
        now=lambda: now_value,
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_NOTHING_TO_DO)
    assert read_activity_stamp(state_dir) == poisoned, (
        "an unreachable database's poisoned stamp must be left untouched, not silently healed"
    )


def test_extremely_future_stamp_does_not_crash_and_self_heals(tmp_path) -> None:
    """C-10: An EXTREME clock-skew stamp (decades in the future) must not
    raise (e.g. no OverflowError from a giant timedelta) — and, exactly like
    the more modest hour-long poisoning above, is treated as untrustworthy
    and self-heals rather than being silently protected forever."""
    state_dir = tmp_path / "state"
    now_value = _dt(2026, 7, 28)
    touch_activity(state_dir=state_dir, now=lambda: _dt(2100, 1, 1))
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: now_value,
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_STAMP_POISON_RECOVERED)
    assert read_activity_stamp(state_dir) == now_value


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


# ---------------------------------------------------------------------------
# REASON_STAMP_UNRECORDABLE (honesty fix — landed). `_stamp_decision` used to
# call `touch_activity(state_dir=directory, now=lambda: moment)` and return
# REASON_STAMP_BOOTSTRAPPED / REASON_STAMP_POISON_RECOVERED
# UNCONDITIONALLY, discarding the boolean pinned in the section above — so on
# a read-only or root-owned `data/state`, every `db idle-stop` run claimed a
# stamp was written when NONE ever landed, and the database was never
# stopped: permanently, silently. `REASON_STAMP_UNRECORDABLE` now exists in
# `src/partgraph/util/activity.py`; it is still imported LOCALLY inside each
# test below (never at this file's module level) rather than added to this
# file's own top-level import list, so that a future regression to this one
# symbol errors only these tests at run time, not the ~120 other tests this
# same file collects.
#
# Every test below makes the write GENUINELY fail (`os.replace` raises,
# exactly like the leaf-level failure injection just above — never a mocked
# call-count or a hand-typed proxy string) and then asserts both which
# REASON comes back AND what is actually left on disk — the property, not a
# proxy. An implementation that returns REASON_STAMP_UNRECORDABLE
# UNCONDITIONALLY (ignoring whether the write actually failed) is caught by
# the EXISTING, unmodified tests above —
# `test_no_stamp_and_db_reachable_bootstraps_a_stamp_and_does_not_stop_yet`,
# `test_no_stamp_bootstrap_protects_for_a_full_budget_window_then_goes_stale`,
# `test_future_stamp_beyond_the_poison_ceiling_is_untrustworthy_and_self_
# heals_via_bootstrap`, `test_extremely_future_stamp_does_not_crash_and_
# self_heals` — all of which pin the OLD reasons against a genuinely
# writable tmp_path, so that direction of the mutation is already covered
# and is not duplicated here.
# ---------------------------------------------------------------------------


def test_bootstrap_write_failure_reports_unrecordable_not_bootstrapped_and_writes_no_stamp(
    tmp_path, monkeypatch, caplog
) -> None:
    """Given a fresh install (no stamp, no lease) with the database
    REACHABLE right now — ordinarily the bootstrap case — but the state dir
    is genuinely unwritable (`os.replace` raises).
    When `evaluate_idle` runs.
    Then `should_stop` stays False (the safe direction is unchanged) BUT
    `reason` is `REASON_STAMP_UNRECORDABLE` — NEVER `REASON_STAMP_
    BOOTSTRAPPED`, which would assert a write that did not happen — and (the
    actual property, not merely a different string) NO stamp file exists on
    disk afterward, proving the reported reason matches what is actually
    durable. A WARNING is logged exactly once, proving the failure is routed
    through the SAME swallow-and-warn path `_atomic_write` already uses, not
    a new, separately-untested error path.
    """
    from partgraph.util.activity import REASON_STAMP_UNRECORDABLE  # noqa: PLC0415

    state_dir = tmp_path / "state"
    monkeypatch.setattr(
        os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated: read-only"))
    )

    with caplog.at_level("WARNING"):
        decision = evaluate_idle(
            state_dir=state_dir,
            idle_timeout_minutes=30.0,
            db_reachable=True,
            now=lambda: _dt(2026, 7, 28, 9, 0, 0),
            psutil_module=_fake_psutil(),
        )

    assert decision.should_stop is False
    assert decision.reason == REASON_STAMP_UNRECORDABLE, (
        f"a write that genuinely did not land must never be reported as "
        f"REASON_STAMP_BOOTSTRAPPED: got {decision.reason!r}"
    )
    assert not activity_stamp_path(state_dir).exists(), (
        "no stamp file may exist on disk when the reported reason admits the write failed"
    )
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_poison_recovery_write_failure_reports_unrecordable_and_leaves_the_poisoned_stamp_untouched(
    tmp_path, monkeypatch
) -> None:
    """Given a stamp poisoned beyond the ceiling — ordinarily the self-heal/
    poison-recovery case — with the database reachable, but the write
    genuinely fails.
    When `evaluate_idle` runs.
    Then `reason` is `REASON_STAMP_UNRECORDABLE` — NEVER `REASON_STAMP_
    POISON_RECOVERED`, which would claim a self-heal that never happened —
    and the ORIGINAL poisoned stamp is left completely UNCHANGED on disk (a
    failed `os.replace` never touches the real target, only the temp file it
    wrote to first), proving the failed self-heal attempt neither corrupted
    nor partially overwrote the existing record either.
    """
    from partgraph.util.activity import REASON_STAMP_UNRECORDABLE  # noqa: PLC0415

    state_dir = tmp_path / "state"
    now_value = _dt(2026, 7, 28, 12, 0, 0)
    poisoned = now_value + timedelta(hours=1)
    touch_activity(state_dir=state_dir, now=lambda: poisoned)  # lands: still writable here

    monkeypatch.setattr(
        os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated: read-only"))
    )
    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: now_value,
        psutil_module=_fake_psutil(),
    )

    assert decision.should_stop is False
    assert decision.reason == REASON_STAMP_UNRECORDABLE, (
        f"a failed self-heal write must never be reported as "
        f"REASON_STAMP_POISON_RECOVERED: got {decision.reason!r}"
    )
    assert read_activity_stamp(state_dir) == poisoned, (
        "the original poisoned stamp must survive a failed self-heal attempt untouched"
    )


def test_stamp_unrecordable_reason_constant_is_a_new_distinct_plain_string() -> None:
    """Given the module's existing REASON_* tags are all distinct, path-free
    plain strings (module docstring's own stated invariant: "every value is
    safe to print verbatim: no path, no separator, no operator data").
    When `REASON_STAMP_UNRECORDABLE` is read.
    Then it is a `str`, contains no `/`, and collides with none of the seven
    reason tags this file already imports — including `REASON_NOTHING_TO_DO`
    (reusing it would conflate "the database is not reachable" with "the
    database IS up but the record could not be written", exactly the
    state-collapse this module's own docstring forbids for leases, now
    pinned here for the stamp too) — proving the new tag is genuinely
    additive, never an accidental alias for an existing one."""
    from partgraph.util.activity import REASON_STAMP_UNRECORDABLE  # noqa: PLC0415

    existing = {
        REASON_DISABLED, REASON_LIVE_LEASE, REASON_UNDETERMINED_LEASE,
        REASON_FRESH_STAMP, REASON_STALE, REASON_NOTHING_TO_DO,
        REASON_STAMP_BOOTSTRAPPED, REASON_STAMP_POISON_RECOVERED,
    }
    assert isinstance(REASON_STAMP_UNRECORDABLE, str)
    assert "/" not in REASON_STAMP_UNRECORDABLE
    assert REASON_STAMP_UNRECORDABLE not in existing


# ---------------------------------------------------------------------------
# [Gate 5 finding 2] The two-state invariant, pinned as a BLACK-BOX
# regression test, not merely argued in a comment. `_stamp_decision`'s own
# comment (`src/partgraph/util/activity.py`) records WHY `touch_activity`'s
# "declined a needless write under monotonic protection -> True" success
# case is structurally unreachable from its one call site: that branch runs
# ONLY when the freshly-read stamp is absent or poisoned, and a fresh read
# of an already-correct (non-poisoned) stamp is neither. That reasoning was
# confirmed by inspection but asserted nowhere — precisely the "prose only"
# criticism this commit's own earlier finding made of the docstring it
# fixed. These two tests patch the module's OWN `touch_activity` name with
# a fail-fast fake and prove it is NEVER CALLED AT ALL when an existing
# stamp is already fresh or already stale (both are "already correct":
# present and non-poisoned) — so a future change that widens the gating to
# also call `touch_activity` in either of those cases trips this
# immediately, rather than only being caught if someone happens to reread
# the comment.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing_stamp_age_minutes", "expected_reason"),
    [
        pytest.param(5.0, REASON_FRESH_STAMP, id="already_fresh"),
        pytest.param(120.0, REASON_STALE, id="already_stale"),
    ],
)
def test_stamp_decision_never_calls_touch_activity_when_an_existing_stamp_is_already_correct(
    tmp_path, monkeypatch, existing_stamp_age_minutes: float, expected_reason: str
) -> None:
    """[Gate 5 finding 2 — the regression test] Given a stamp already on
    disk that is neither absent nor poisoned — either FRESH (well within
    the timeout) or STALE (well past it), the two ways an "already correct"
    stamp reaches `_stamp_decision`.
    When `evaluate_idle` runs, with `partgraph.util.activity.touch_activity`
    itself replaced by a fake that raises `AssertionError` if ever called.
    Then the decision is unaffected (still the ordinary fresh/stale reason)
    AND the fake is never invoked — proving, black-box, that widening
    `_stamp_decision`'s gating to call `touch_activity` for an
    already-correct stamp would be caught here, not left to a comment."""
    import partgraph.util.activity as activity_module

    state_dir = tmp_path / "state"
    t0 = _dt(2026, 7, 28, 9, 0, 0)
    touch_activity(state_dir=state_dir, now=lambda: t0)  # a real, landed, non-poisoned stamp

    def _forbid_touch_activity(**kwargs):
        raise AssertionError(
            "touch_activity must never be called by _stamp_decision when an "
            "existing stamp is already correct (fresh or stale, never absent "
            "or poisoned) — this is exactly the gating widening Gate 5 asked "
            "to be regression-tested"
        )

    monkeypatch.setattr(activity_module, "touch_activity", _forbid_touch_activity)

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: t0 + timedelta(minutes=existing_stamp_age_minutes),
        psutil_module=_fake_psutil(),
    )
    assert decision == IdleDecision(should_stop=(expected_reason == REASON_STALE), reason=expected_reason)


# ---------------------------------------------------------------------------
# [Gate 5 finding 3] "Not sticky" — pinned hermetically, not only by hand
# with a real `chmod 0500` (as both the implementer's and the reviewer's own
# commit messages record). A call-counter over `os.replace` fails exactly
# ONCE, then behaves normally, across TWO SEQUENTIAL `evaluate_idle` calls
# sharing the SAME on-disk state — mirroring
# `test_no_stamp_bootstrap_protects_for_a_full_budget_window_then_goes_stale`'s
# own "two real, sequential calls sharing on-disk state" pattern, the actual
# property rather than a single-call proxy.
# ---------------------------------------------------------------------------


def test_unrecordable_state_self_heals_to_bootstrapped_once_the_write_succeeds_again(
    tmp_path, monkeypatch
) -> None:
    """[Gate 5 finding 3 — hermetic, not manual-only] Given a fresh install
    (no stamp) where the FIRST `evaluate_idle` call's underlying write
    genuinely fails (`os.replace` raises), and the SECOND call's write
    succeeds — modelling the state directory becoming writable again (an
    operator fixing a permission, or a full disk being freed), driven by an
    in-process call-counter, never a real filesystem permission change.
    When both calls run in sequence against the same `tmp_path` state dir.
    Then the FIRST reports `REASON_STAMP_UNRECORDABLE` and leaves no stamp
    on disk; the SECOND reports `REASON_STAMP_BOOTSTRAPPED` and a real
    stamp now exists — proving the unrecordable state reflects only THIS
    call's own write attempt, never a remembered failure from a previous
    one: it is not sticky.
    """
    from partgraph.util.activity import REASON_STAMP_UNRECORDABLE  # noqa: PLC0415

    state_dir = tmp_path / "state"
    real_replace = os.replace
    call_count = {"n": 0}

    def _fails_once_then_succeeds(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated: read-only (first attempt only)")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "replace", _fails_once_then_succeeds)

    first = evaluate_idle(
        state_dir=state_dir, idle_timeout_minutes=30.0, db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 9, 0, 0), psutil_module=_fake_psutil(),
    )
    assert first.reason == REASON_STAMP_UNRECORDABLE
    assert not activity_stamp_path(state_dir).exists()

    second_moment = _dt(2026, 7, 28, 9, 1, 0)
    second = evaluate_idle(
        state_dir=state_dir, idle_timeout_minutes=30.0, db_reachable=True,
        now=lambda: second_moment, psutil_module=_fake_psutil(),
    )
    assert second == IdleDecision(should_stop=False, reason=REASON_STAMP_BOOTSTRAPPED), (
        "the state must self-heal to an honest bootstrap the moment the write "
        "starts landing again — it must never stay stuck reporting unrecordable "
        "once the underlying failure is gone"
    )
    assert read_activity_stamp(state_dir) == second_moment
    assert call_count["n"] == 2, "sanity: both evaluate_idle calls must have attempted the write"


# ---------------------------------------------------------------------------
# [Gate 3a SHOULD-FIX] size/shape bounds on stamp and lease reads — mirrors
# `partgraph.util.lifecycle.MAX_PS_OUTPUT_BYTES`'s own bounded-constant
# precedent ("ps output is bounded ... before it is decoded"), applied here
# to a much smaller expected payload (a fixed-size, single-marker file, not
# a whole-host container enumeration). This matters MORE than it looks: the
# monotonic-write rule means `touch_activity` reads the EXISTING stamp
# before writing, so this read path fires on all nine DB-touching commands
# on every call, not merely the opt-in `db idle-stop` timer — an oversized
# file, a directory where a file is expected, or a symlink to a FIFO/device
# must never hang or exhaust memory during perfectly ordinary use.
# ---------------------------------------------------------------------------

_SYMLINK_HANG_SUBPROCESS_TIMEOUT_S = 8.0


def test_max_stamp_and_lease_file_bytes_are_finite_positive_bounds() -> None:
    assert isinstance(MAX_STAMP_FILE_BYTES, int)
    assert isinstance(MAX_LEASE_FILE_BYTES, int)
    assert 0 < MAX_STAMP_FILE_BYTES < 10 * 1024 * 1024
    assert 0 < MAX_LEASE_FILE_BYTES < 10 * 1024 * 1024


def test_read_activity_stamp_treats_an_oversized_but_otherwise_valid_file_as_unreadable(
    tmp_path,
) -> None:
    """[Gate 3a SHOULD-FIX] Given a stamp file whose JSON content IS
    otherwise well-formed and valid, but whose total byte size EXCEEDS
    `MAX_STAMP_FILE_BYTES` (padded with an oversized filler value, isolating
    the SIZE bound from the "malformed JSON" degradation already proven
    elsewhere).
    When `read_activity_stamp` is called.
    Then it returns None — the bound is checked before/regardless of
    whether the content would otherwise parse, mirroring
    `MAX_PS_OUTPUT_BYTES`'s own "discarded without being decoded"
    discipline."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    padding = "A" * (MAX_STAMP_FILE_BYTES + 1024)
    payload = json.dumps({"last_active_utc": "2026-01-01T00:00:00Z", "_pad": padding})
    assert len(payload.encode("utf-8")) > MAX_STAMP_FILE_BYTES
    activity_stamp_path(state_dir).write_text(payload, encoding="utf-8")

    assert read_activity_stamp(state_dir) is None


def test_read_lease_treats_an_oversized_but_otherwise_valid_file_as_unreadable(tmp_path) -> None:
    """[Gate 3a SHOULD-FIX] Mirrors the stamp test above, for the lease."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    padding = "A" * (MAX_LEASE_FILE_BYTES + 1024)
    payload = json.dumps(
        {"pid": 123, "create_time": 1.0, "acquired_utc": "2026-01-01T00:00:00Z", "_pad": padding}
    )
    assert len(payload.encode("utf-8")) > MAX_LEASE_FILE_BYTES
    target = lease_path(state_dir, pid=123)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")

    assert read_lease(state_dir, pid=123) is None


def test_touch_activity_treats_an_oversized_existing_stamp_as_unreadable_and_overwrites_it(
    tmp_path,
) -> None:
    """[Gate 3a SHOULD-FIX] Given the EXISTING on-disk stamp is oversized
    (per the size bound above) — this is the path that fires on EVERY
    DB-touching command's write, not only the opt-in timer's reads.
    When `touch_activity` is called.
    Then it does not hang or crash: the oversized existing stamp is treated
    as unreadable (equivalent to no existing stamp), so the monotonic
    comparison is skipped and the new value is written normally."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    padding = "A" * (MAX_STAMP_FILE_BYTES + 1024)
    payload = json.dumps({"last_active_utc": "2026-01-01T00:00:00Z", "_pad": padding})
    activity_stamp_path(state_dir).write_text(payload, encoding="utf-8")

    new_value = _dt(2026, 7, 28, 12, 0, 0)
    touch_activity(state_dir=state_dir, now=lambda: new_value)
    assert read_activity_stamp(state_dir) == new_value


@pytest.mark.parametrize(
    "target_kind", ["stamp", "lease"],
)
def test_read_degrades_to_none_when_the_expected_path_is_actually_a_directory(
    tmp_path, target_kind: str,
) -> None:
    """[Gate 3a SHOULD-FIX] Given the path the reader expects to be a plain
    file is actually a DIRECTORY (a plausible operator mistake, or a leftover
    from an unrelated tool).
    When the corresponding read function is called.
    Then it returns None (never raises `IsADirectoryError`)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    if target_kind == "stamp":
        activity_stamp_path(state_dir).mkdir(parents=True)
        assert read_activity_stamp(state_dir) is None
    else:
        lease_path(state_dir, pid=123).mkdir(parents=True)
        assert read_lease(state_dir, pid=123) is None


def _assert_read_call_does_not_hang(script_body: str) -> str:
    """Run *script_body* in a SEPARATE subprocess with a hard timeout, so a
    genuinely-hanging implementation fails this test deterministically
    (a `TimeoutExpired` -> `pytest.fail`) rather than hanging the whole
    suite. Returns the subprocess's stripped stdout on success."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script_body],
            capture_output=True, text=True,
            timeout=_SYMLINK_HANG_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"the read hung for more than {_SYMLINK_HANG_SUBPROCESS_TIMEOUT_S}s — it "
            "must check the file's type (e.g. stat().st_mode) before ever "
            "opening it, never block reading a FIFO/device with no writer."
        )
    assert result.returncode == 0, (
        f"subprocess failed unexpectedly:\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    return result.stdout.strip()


def test_read_activity_stamp_on_a_symlink_to_a_fifo_degrades_promptly_not_hangs(
    tmp_path,
) -> None:
    """[Gate 3a SHOULD-FIX] Given the stamp path is a symlink to a REAL
    FIFO with no writer — a plain `open()`/`Path.read_text()` on such a FIFO
    blocks INDEFINITELY at the OS level, since nothing will ever write to
    it.
    When `read_activity_stamp` is called against it, in a subprocess with
    its own hard wall-clock bound (so a genuinely-hanging implementation
    fails this test deterministically instead of hanging the suite).
    Then it returns `None` promptly."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is not available on this platform.")
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    fifo_path = tmp_path / "real_fifo"
    os.mkfifo(fifo_path)
    stamp_path = activity_stamp_path(state_dir)
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.symlink_to(fifo_path)

    script = (
        "from partgraph.util.activity import read_activity_stamp\n"
        f"print(read_activity_stamp({str(state_dir)!r}))\n"
    )
    stdout = _assert_read_call_does_not_hang(script)
    assert stdout == "None", stdout


def test_read_lease_on_a_symlink_to_a_fifo_degrades_promptly_not_hangs(tmp_path) -> None:
    """[Gate 3a SHOULD-FIX] Mirrors the stamp test above, for the lease."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is not available on this platform.")
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    fifo_path = tmp_path / "real_fifo"
    os.mkfifo(fifo_path)
    lease_target = lease_path(state_dir, pid=4242)
    lease_target.parent.mkdir(parents=True, exist_ok=True)
    lease_target.symlink_to(fifo_path)

    script = (
        "from partgraph.util.activity import read_lease\n"
        f"print(read_lease({str(state_dir)!r}, pid=4242))\n"
    )
    stdout = _assert_read_call_does_not_hang(script)
    assert stdout == "None", stdout
