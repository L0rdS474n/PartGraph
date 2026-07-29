"""
Tests: a real defect in MERGED code — `db idle-stop`'s lease-liveness check
(`partgraph.util.activity._lease_status`/`evaluate_idle`, ADR-0023) misreads a
genuinely LIVE process as DEAD after an ordinary system clock step, and lets
`db idle-stop` stop the database underneath running work. ADR-0025's "Open
risk: a clock step can still make a live lease read as dead" section
(`docs/decisions/ADR-0025-runtime-dependency-version-bounds.md`, § 1) names
this as an open gap in merged code, not a hypothetical, and its acceptance
criterion is: "A behavioural test that steps a simulated clock and asserts the
lease still reads live." `tests/unit/test_psutil_process_identity_real.py`'s
own module docstring already discloses the gap it leaves open: the pre-clock-
step comparison hazard "is exercised only under an actual clock step, which
this test does not attempt to simulate." This file is that simulation.

THE MECHANISM (measured on this host, installed psutil 7.2.2 — confirmed via
`importlib.metadata.version("psutil")` — before being written as an assertion
below; `test_a_forward_clock_step_moves_a_fresh_create_time_read_by_the_step_
size_past_the_tolerance` decodes it directly rather than merely asserting it):
`acquire_lease` persists `psutil.Process(pid).create_time()` in EPOCH form —
`monotonic_start + boot_time()`, per the installed `psutil._pslinux.py`'s own
source (read directly, quoted in ADR-0025 § 1). `_lease_status` later re-reads
`create_time()` and compares the two floats with `math.isclose(...,
abs_tol=_CREATE_TIME_TOLERANCE_S)` (`_CREATE_TIME_TOLERANCE_S = 1e-3`,
`src/partgraph/util/activity.py`). `boot_time()` is re-derived from
`/proc/stat`'s `btime` line on every call, so a clock step of Delta seconds
moves the SECOND reading by Delta while the recorded one stays put. Any step
larger than a millisecond -- every real NTP correction, every manual `date`
set, a resume from a drifted suspend -- makes the comparison miss, and the
mismatch branch returns DEAD: `db idle-stop` then stops a database while the
process holding the lease is still genuinely working.

HOW THE STEP IS SIMULATED, HERMETICALLY (no root, no real clock change, no
container -- this branch's own CONSTRAINTS). `psutil.boot_time()` (the public
entry point, `psutil/__init__.py`, read directly) is a thin wrapper:
`return _psplatform.boot_time()`. `_psplatform` IS the platform backend
module (`psutil._psplatform is psutil._pslinux` on Linux -- confirmed below,
not assumed, by `test_psutil_boot_time_public_wrapper_delegates_to_the_
platform_backend_module_this_file_patches`), and the platform backend's own
`Process.create_time()` resolves the SAME module-level name `boot_time()` at
call time too (`psutil._pslinux.py`, read directly: `return self._ctime +
boot_time()`). So `_step_boot_time` below monkeypatches
`psutil._psplatform.boot_time` -- never `create_time()` itself, anywhere in
this file -- which moves BOTH what `psutil.boot_time()` publicly reports AND
what a FRESH `psutil.Process(pid).create_time()` computes internally, exactly
as a real `/proc/stat` `btime` change would. This is the distinction the
brief that produced this file draws explicitly: "A test that patches
create_time to return a different number proves arithmetic; a test that moves
what boot_time() returns and shows the real comparison breaking proves the
defect." Every RED test below does the second thing.

WHY THIS PROVES THE DEFECT AND NOT JUST THE SIMULATION TECHNIQUE: Section 1
below is a self-test/positive-control proving `_step_boot_time` has real
teeth (moves the real, installed library's own live computation by the exact
injected delta) BEFORE any test relies on it to demonstrate the defect --
mirroring `test_activity_architecture.py`'s own "prove the scanner has real
teeth... before trusting it against the real tree" discipline.

FILE SPLIT: kept separate from `tests/unit/test_activity.py` (which already
holds ~120 `evaluate_idle`/lease tests against an INJECTED fake psutil) and
from `tests/unit/test_psutil_process_identity_real.py` (real-psutil primitive
pins, extended by two new tests there for the numeric soundness of the fix's
own candidate quantity -- see that file) because this file's own subject is
neither: it is the DEFECT itself -- real psutil, a real boot-time anchor
genuinely moved, and the real, installed comparison breaking -- plus the
migration contract the fix's own persisted-format change creates. Mirrors
this repository's own precedent for splitting a distinct concern into its own
file rather than growing one file indefinitely (`test_activity_architecture.py`
was split from `test_activity.py` the same way).

CURRENT STATUS OF THIS FILE: every test in Sections 2 and 4 is RED against
`main` @ 5932a20 (verified: each was run standalone against the current,
unmodified `src/partgraph/util/activity.py` before being committed here, and
each failed with the exact wrong `IdleDecision` documented in its own
docstring -- never assumed, never fabricated). Section 1 (the simulation
technique's own positive control) and Section 3 (the mechanism, decoded) are
GREEN today: they describe the ALREADY-true behaviour of the installed
library and the ALREADY-true defect mechanism, not the fix. Section 4's
migration tests are RED for a DIFFERENT reason than Section 2's: today's code
has no format distinction at all, so today's own on-disk payload is read
exactly as today's code already reads it (LIVE or STALE, per case) -- never
UNDETERMINED, which is what the fix + migration must produce instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import psutil
import pytest

from partgraph.util.activity import (
    _CREATE_TIME_TOLERANCE_S,
    IdleDecision,
    REASON_LIVE_LEASE,
    REASON_STALE,
    REASON_UNDETERMINED_LEASE,
    acquire_lease,
    evaluate_idle,
    lease_path,
    touch_activity,
)

#: Bounded wait for every subprocess this file spawns or reaps -- a hanging
#: child fails the test deterministically, mirroring
#: `test_psutil_process_identity_real.py`'s own discipline.
_SUBPROCESS_WAIT_TIMEOUT_S = 10.0

#: A step comfortably larger than any real NTP correction (which are
#: sub-second to low-single-digit seconds in practice, per `test_activity.py`'s
#: own `STAMP_FUTURE_POISON_CEILING_MINUTES` commentary) but still an entirely
#: ordinary one -- an hour is well within "someone fixed the RTC" or "resume
#: from a suspend whose clock had drifted", not an extreme edge case.
_ORDINARY_FORWARD_CLOCK_STEP_S = 3600.0


def _dt(  # noqa: PLR0913, PLR0917 -- a fixed-instant builder needs one arg per field.
    year, month, day, hour=0, minute=0, second=0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _step_boot_time(monkeypatch: pytest.MonkeyPatch, delta_seconds: float) -> None:
    """Simulate a system clock step of *delta_seconds* by moving the ONE
    real source both `psutil.boot_time()` (public) and the installed
    library's own internal `Process.create_time()` computation resolve
    through at call time -- see the module docstring's "HOW THE STEP IS
    SIMULATED" section for the measured justification.

    Deliberately narrow: this patches `psutil._psplatform.boot_time` alone.
    `psutil.Process.create_time()` itself is NEVER patched anywhere in this
    file -- the real, installed arithmetic runs unmodified on every call;
    only the boot-time anchor it reads is moved, through the exact code path
    a genuine `/proc/stat` `btime` change would move it through. Restored
    automatically at the end of the test via *monkeypatch*'s own teardown, so
    a failing assertion mid-test can never leak a patched `boot_time()` into
    a later, unrelated test.
    """
    if not hasattr(psutil, "_psplatform") or not hasattr(psutil._psplatform, "boot_time"):
        pytest.skip(
            "this installed psutil build exposes no _psplatform.boot_time to "
            "patch; the hermetic clock-step simulation this file depends on "
            "is unavailable here (expected only on a non-Linux platform)."
        )
    real_boot_time = psutil._psplatform.boot_time

    def _stepped_boot_time() -> float:
        return real_boot_time() + delta_seconds

    monkeypatch.setattr(psutil._psplatform, "boot_time", _stepped_boot_time)


# ---------------------------------------------------------------------------
# Section 1 -- positive control: `_step_boot_time` genuinely moves the real,
# installed library's own live computation, proven BEFORE any RED test below
# relies on it (mirrors test_activity_architecture.py's own "prove the
# scanner has real teeth... before trusting it" discipline).
# ---------------------------------------------------------------------------


def test_psutil_boot_time_public_wrapper_delegates_to_the_platform_backend_module_this_file_patches() -> (
    None
):
    """Given the REAL, installed psutil package.
    When `psutil._psplatform` (the platform backend module the public
    `psutil.boot_time()` wrapper delegates to -- `psutil/__init__.py`'s own
    `def boot_time(): return _psplatform.boot_time()`, read directly) is
    inspected.
    Then it exposes a `boot_time` attribute, and on this Linux host it IS
    `psutil._pslinux` -- confirmed directly (`is`, not name equality), not
    assumed from the module's own docstring. This is the exact object
    `_step_boot_time` patches; if this ever stopped holding, every RED test
    in Section 2/4 would be patching the wrong target and would (correctly)
    stop reproducing the defect, which is why it is pinned here independently
    rather than only asserted in prose.
    """
    if sys.platform != "linux":
        pytest.skip("this pin is specific to the Linux backend this repository targets.")
    assert hasattr(psutil, "_psplatform")
    assert hasattr(psutil._psplatform, "boot_time")
    import psutil._pslinux as pslinux  # noqa: PLC0415

    assert psutil._psplatform is pslinux, (
        "psutil._psplatform is expected to BE psutil._pslinux on this platform "
        "(identity, not merely equal behaviour) -- if this changed, "
        "_step_boot_time would silently patch the wrong module."
    )


def test_step_boot_time_moves_both_the_public_wrapper_and_a_fresh_create_time_read_by_the_same_delta(
    monkeypatch,
) -> None:
    """Given the REAL, currently-running pytest worker process (`os.getpid()`,
    guaranteed alive for the assertion's duration) and its `create_time()`
    read via a FRESH `psutil.Process` BEFORE any patch is applied.
    When `_step_boot_time` moves `psutil._psplatform.boot_time` forward by a
    known delta, and BOTH `psutil.boot_time()` (public) and a NEW
    `psutil.Process(pid).create_time()` (a second, independent construction,
    not the cached object from before the patch) are read again.
    Then EACH moves by exactly the injected delta (within float-rounding
    noise, `abs_tol=1e-6` -- far tighter than `_CREATE_TIME_TOLERANCE_S`
    itself, since this is proving the SIMULATION's fidelity, not the
    production tolerance) -- proving the helper genuinely reproduces what a
    real `/proc/stat` `btime` change does to BOTH quantities `_lease_status`
    depends on, not merely to one of them in isolation.
    """
    pid = os.getpid()
    boot_time_before = psutil.boot_time()
    create_time_before = psutil.Process(pid).create_time()

    delta = 1234.5
    _step_boot_time(monkeypatch, delta)

    boot_time_after = psutil.boot_time()
    create_time_after = psutil.Process(pid).create_time()  # fresh Process object

    assert boot_time_after == pytest.approx(boot_time_before + delta, abs=1e-6), (
        "the public psutil.boot_time() wrapper must reflect the simulated step"
    )
    assert create_time_after == pytest.approx(create_time_before + delta, abs=1e-6), (
        "a FRESH psutil.Process(pid).create_time() must reflect the simulated "
        "step too -- this is the property that makes the simulation faithful "
        "to a real clock step, not merely a patch of one isolated call"
    )


# ---------------------------------------------------------------------------
# Section 2 -- THE acceptance criterion named in ADR-0025 § 1 and in this
# fix's own brief: "A test that steps a simulated clock and asserts the lease
# still reads live." Every test below is RED against the current, unmodified
# `partgraph.util.activity` (verified by running each standalone before it
# was written here; every failure showed exactly the wrong IdleDecision named
# in its own docstring).
# ---------------------------------------------------------------------------


def test_evaluate_idle_still_reads_a_live_lease_as_live_after_a_simulated_forward_clock_step(
    tmp_path, monkeypatch
) -> None:
    """[THE acceptance criterion -- ADR-0025 § 1's "Open risk"] Given a REAL
    lease acquired for the CURRENT, genuinely-alive pytest worker process
    (`acquire_lease` with no injected psutil fake -- the real, installed
    library) AND an activity stamp already decades stale (so a fallthrough
    to the stamp, were the lease wrongly cleared, would demand a stop --
    mirrors `test_activity.py`'s own
    `test_live_lease_blocks_stop_even_with_a_very_stale_stamp`).
    When an ordinary forward system clock step (`_ORDINARY_FORWARD_CLOCK_STEP_S`
    -- one hour, e.g. an NTP correction or a manual `date` fix) occurs BETWEEN
    the lease being acquired and `evaluate_idle` running, simulated via
    `_step_boot_time` (the real comparison breaking, not a patched
    `create_time()`).
    Then the decision MUST still be `should_stop=False,
    reason=REASON_LIVE_LEASE` -- the process is genuinely alive throughout;
    the ONLY thing that moved is the wall clock. On the CURRENT, unmodified
    implementation this fails: the lease is misread as a recycled PID (a
    "confirmed dead" mismatch), `should_stop` comes back True with
    `reason=REASON_STALE`, and the lease file is deleted -- the exact
    "`db idle-stop` stops a database underneath running work" failure
    ADR-0025 documents as an open gap in merged code.
    """
    state_dir = tmp_path / "state"
    pid = os.getpid()
    acquire_lease(state_dir=state_dir, pid=pid, now=lambda: _dt(2026, 7, 28))
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))  # decades stale

    _step_boot_time(monkeypatch, _ORDINARY_FORWARD_CLOCK_STEP_S)

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 0, 5, 0),
        psutil_module=None,  # the real, installed psutil -- never a fake here
    )

    assert decision == IdleDecision(should_stop=False, reason=REASON_LIVE_LEASE), (
        f"a genuinely LIVE process must not be reported DEAD merely because the "
        f"wall clock stepped forward by {_ORDINARY_FORWARD_CLOCK_STEP_S}s: "
        f"got {decision!r} instead"
    )
    assert lease_path(state_dir, pid=pid).exists(), (
        "a LIVE lease must never be removed -- the current defective code "
        "deletes it here, believing the PID was recycled"
    )


def test_evaluate_idle_still_reads_a_live_lease_as_live_after_a_simulated_backward_clock_step(
    tmp_path, monkeypatch
) -> None:
    """Mirrors the test above for the OTHER direction of an NTP correction --
    a BACKWARD step (the clock was briefly fast and gets corrected back). The
    defect is symmetric: `math.isclose` fails identically whichever way the
    two readings diverge, so a backward step is just as capable of falsely
    declaring a live owner dead as a forward one. Given the same real, live
    lease and stale stamp as above.
    When the wall clock steps BACKWARD by `_ORDINARY_FORWARD_CLOCK_STEP_S`.
    Then the decision must still be `should_stop=False, reason=REASON_LIVE_LEASE`.
    """
    state_dir = tmp_path / "state"
    pid = os.getpid()
    acquire_lease(state_dir=state_dir, pid=pid, now=lambda: _dt(2026, 7, 28))
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    _step_boot_time(monkeypatch, -_ORDINARY_FORWARD_CLOCK_STEP_S)

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 0, 5, 0),
        psutil_module=None,
    )

    assert decision == IdleDecision(should_stop=False, reason=REASON_LIVE_LEASE), (
        f"a genuinely LIVE process must not be reported DEAD merely because the "
        f"wall clock stepped BACKWARD by {_ORDINARY_FORWARD_CLOCK_STEP_S}s: "
        f"got {decision!r} instead"
    )
    assert lease_path(state_dir, pid=pid).exists()


def test_evaluate_idle_still_reads_a_live_lease_of_a_spawned_child_process_as_live_after_a_clock_step(
    tmp_path, monkeypatch
) -> None:
    """Mirrors the primary acceptance test above, but for a genuinely
    SEPARATE, disposable child process (spawned and reaped by this test
    itself under a bounded wait -- mirroring
    `test_psutil_process_identity_real.py`'s own subprocess discipline)
    rather than the pytest worker's own PID -- proving the defect (and the
    fix's acceptance criterion) is not an artifact of some special-case
    behaviour of the test's own process, but a property of `evaluate_idle`
    against any real, live PID.
    Given a real child process, kept alive (`sleep`) across the whole
    assertion, with a real lease acquired for its PID.
    When the SAME simulated forward clock step occurs.
    Then the decision is still LIVE, and the still-running child is reaped
    cleanly afterward regardless of the assertion's outcome.
    """
    state_dir = tmp_path / "state"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        acquire_lease(state_dir=state_dir, pid=child.pid, now=lambda: _dt(2026, 7, 28))
        touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

        _step_boot_time(monkeypatch, _ORDINARY_FORWARD_CLOCK_STEP_S)

        decision = evaluate_idle(
            state_dir=state_dir,
            idle_timeout_minutes=30.0,
            db_reachable=True,
            now=lambda: _dt(2026, 7, 28, 0, 5, 0),
            psutil_module=None,
        )
    finally:
        child.terminate()
        child.wait(timeout=_SUBPROCESS_WAIT_TIMEOUT_S)

    assert decision == IdleDecision(should_stop=False, reason=REASON_LIVE_LEASE), (
        f"a genuinely live CHILD process's lease must not be reported DEAD "
        f"after a simulated clock step: got {decision!r} instead"
    )


def test_a_clock_step_within_the_existing_tolerance_still_reads_live_control(
    tmp_path, monkeypatch
) -> None:
    """[Negative control for the three RED tests above] Given the SAME real,
    live lease and stale stamp, but a step SMALLER than
    `_CREATE_TIME_TOLERANCE_S` itself (a tenth of the tolerance -- far below
    any real clock correction, chosen only to stay under the existing
    comparison's own margin).
    When `evaluate_idle` runs.
    Then the decision is LIVE on the CURRENT, unmodified implementation too
    -- proving the RED tests above fail specifically BECAUSE the step
    exceeds the tolerance, not because `_step_boot_time` or this file's own
    harness is broken in some way that would fail EVERY test regardless of
    step size.
    """
    state_dir = tmp_path / "state"
    pid = os.getpid()
    acquire_lease(state_dir=state_dir, pid=pid, now=lambda: _dt(2026, 7, 28))
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    _step_boot_time(monkeypatch, _CREATE_TIME_TOLERANCE_S / 10.0)

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 0, 5, 0),
        psutil_module=None,
    )
    assert decision == IdleDecision(should_stop=False, reason=REASON_LIVE_LEASE)


# ---------------------------------------------------------------------------
# Section 3 -- decode-before-hypothesise: the exact mechanism, measured and
# asserted directly against the real, installed library (currently GREEN --
# this documents the ALREADY-true defect mechanism, not the fix).
# ---------------------------------------------------------------------------


def test_a_forward_clock_step_moves_a_fresh_create_time_read_by_the_step_size_past_the_tolerance(
    monkeypatch,
) -> None:
    """[decode-before-hypothesise] Given the REAL, currently-running pytest
    worker process and its `create_time()` read via a FRESH `psutil.Process`
    BEFORE any step.
    When `_step_boot_time` moves the boot-time anchor forward by
    `_ORDINARY_FORWARD_CLOCK_STEP_S`, and `create_time()` is read again via a
    SECOND, independent `psutil.Process` construction (mirroring exactly what
    `_lease_status` itself does on every call: a fresh `Process(pid)` per
    check, never a cached one).
    Then the second reading differs from the first by exactly the injected
    step (within float-rounding noise) AND that difference is several orders
    of magnitude LARGER than `_CREATE_TIME_TOLERANCE_S` -- the concrete,
    decoded proof that `math.isclose(..., abs_tol=_CREATE_TIME_TOLERANCE_S)`
    -- the exact comparison `_lease_status` performs -- MUST reject this pair
    as a match, i.e. must call this the DEAD branch, even though nothing
    about the process itself changed.
    """
    pid = os.getpid()
    create_time_before = psutil.Process(pid).create_time()

    _step_boot_time(monkeypatch, _ORDINARY_FORWARD_CLOCK_STEP_S)

    create_time_after = psutil.Process(pid).create_time()  # fresh Process, as _lease_status does
    delta = create_time_after - create_time_before

    assert delta == pytest.approx(_ORDINARY_FORWARD_CLOCK_STEP_S, abs=1e-6), (
        f"expected create_time() to move by exactly the simulated step "
        f"({_ORDINARY_FORWARD_CLOCK_STEP_S}s), measured delta={delta!r}"
    )
    assert abs(delta) > _CREATE_TIME_TOLERANCE_S * 1000, (
        f"the measured drift ({delta!r}s) must be many orders of magnitude "
        f"above _CREATE_TIME_TOLERANCE_S ({_CREATE_TIME_TOLERANCE_S}s) for "
        f"this to be the actual failure mechanism, not a near-miss"
    )


# ---------------------------------------------------------------------------
# Section 4 -- Design question 2, "the sharp edge": migration. A lease file
# on disk in TODAY's format (epoch-form `create_time`, no format marker) must
# be read as UNDETERMINED once the fix + its migration land -- never silently
# reinterpreted as either LIVE (a coincidentally-matching stale value) or
# DEAD (the ~1.7-billion-second mismatch the brief that produced this file
# names explicitly). Both tests below construct TODAY's exact on-disk payload
# via the REAL, unmodified `acquire_lease` / a byte-for-byte equivalent raw
# write -- never a hand-guessed future schema -- so they stay meaningful
# regardless of which mechanism (a new key, a version field) the eventual fix
# chooses, as long as that mechanism keeps reading a payload with NO marker
# as pre-migration. RED against the current implementation for a DIFFERENT
# reason than Section 2: today's code has no format concept at all, so it
# reads its own current payload exactly as it already does -- LIVE or STALE,
# never UNDETERMINED (verified below for both cases before being pinned).
# ---------------------------------------------------------------------------


def test_todays_on_disk_lease_format_for_a_live_matching_pid_is_read_as_undetermined_once_migrated(
    tmp_path,
) -> None:
    """Given a lease file written by TODAY's own, unmodified `acquire_lease`
    for the CURRENT, genuinely-alive pytest worker process -- i.e. exactly
    the payload a pre-upgrade PartGraph install would have left on disk,
    including a `create_time` that NUMERICALLY MATCHES what real psutil
    reports for this PID right now (no clock step at all in this test -- the
    strongest form of the pin: the migration must reject this record on
    FORMAT grounds alone, not because the numbers happen to disagree).
    When `evaluate_idle` runs against it (no injected fake -- the real
    psutil).
    Then the decision must be `should_stop=False,
    reason=REASON_UNDETERMINED_LEASE` -- never `REASON_LIVE_LEASE`, even
    though a numeric comparison alone would call this pair a match. On the
    CURRENT, unmodified implementation (which has no format concept at all)
    this payload reads as LIVE, not UNDETERMINED -- proven standalone before
    this test was written, and the reason this is RED today.
    """
    state_dir = tmp_path / "state"
    pid = os.getpid()
    acquire_lease(state_dir=state_dir, pid=pid, now=lambda: _dt(2026, 7, 28))
    todays_payload = lease_path(state_dir, pid=pid).read_text(encoding="utf-8")
    assert json.loads(todays_payload)["pid"] == pid  # sanity: a genuine, parseable payload
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28, 0, 5, 0),
        psutil_module=None,
    )

    assert decision == IdleDecision(should_stop=False, reason=REASON_UNDETERMINED_LEASE), (
        f"a lease written in TODAY's on-disk format must be treated as "
        f"UNDETERMINED once the fix's migration lands -- never re-trusted as "
        f"LIVE by coincidence: got {decision!r} instead"
    )
    assert lease_path(state_dir, pid=pid).exists(), (
        "an UNDETERMINED lease must be left on disk, never deleted -- it was "
        "never confirmed dead"
    )


def test_todays_on_disk_lease_format_for_a_confirmed_dead_pid_is_read_as_undetermined_once_migrated(
    tmp_path,
) -> None:
    """Given a lease file in TODAY's exact on-disk format (raw JSON,
    byte-for-byte matching what `acquire_lease` currently writes:
    `{"pid": ..., "create_time": ..., "acquired_utc": ...}`) naming a PID
    that has ALREADY exited and been reaped -- the confirmed-dead case, where
    the CURRENT code's `NoSuchProcess` branch would clean it without ever
    reaching the create_time comparison at all.
    When `evaluate_idle` runs against it.
    Then the decision must STILL be `should_stop=False,
    reason=REASON_UNDETERMINED_LEASE`, and the file must be LEFT ON DISK --
    the brief that produced this file states the rule unconditionally ("An
    old-format lease must read as UNDETERMINED... pin that an old-format
    lease is never read as dead"), so the migration must not even attempt a
    liveness determination for a pre-migration record, regardless of whether
    that determination would separately have come out DEAD via a clean
    `NoSuchProcess`. On the CURRENT, unmodified implementation this payload
    is read as STALE (should_stop=True) and the file is deleted -- proven
    standalone before this test was written, and the reason this is RED
    today.
    """
    state_dir = tmp_path / "state"
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=_SUBPROCESS_WAIT_TIMEOUT_S)  # exits AND is reaped: confirmed gone
    dead_pid = child.pid

    todays_format_payload = json.dumps(
        {"pid": dead_pid, "create_time": 12345.0, "acquired_utc": "2026-01-01T00:00:00+00:00"}
    )
    target = lease_path(state_dir, pid=dead_pid)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(todays_format_payload, encoding="utf-8")
    touch_activity(state_dir=state_dir, now=lambda: _dt(2000, 1, 1))

    decision = evaluate_idle(
        state_dir=state_dir,
        idle_timeout_minutes=30.0,
        db_reachable=True,
        now=lambda: _dt(2026, 7, 28),
        psutil_module=None,
    )

    assert decision == IdleDecision(should_stop=False, reason=REASON_UNDETERMINED_LEASE), (
        f"a pre-migration lease must never be silently cleaned as DEAD, even "
        f"when its recorded PID has genuinely exited: got {decision!r} instead"
    )
    assert target.exists(), "an UNDETERMINED lease must be left on disk, never deleted"


# ---------------------------------------------------------------------------
# Sanity: this file's own tolerance value stays what ADR-0025 and this file's
# module docstring both cite -- so a future, unrelated edit to
# _CREATE_TIME_TOLERANCE_S cannot silently invalidate the "several orders of
# magnitude" claim in Section 3 without this file's own suite noticing.
# ---------------------------------------------------------------------------


def test_create_time_tolerance_constant_matches_the_value_this_files_evidence_is_measured_against() -> (
    None
):
    assert _CREATE_TIME_TOLERANCE_S == 1e-3
