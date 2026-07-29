"""
Tests: real-installed-psutil behaviour pins for the `(pid, create_time)`
anti-recycling primitives `partgraph.util.activity._lease_status` composes
(ADR-0023, "psutil becomes load-bearing... psutil's own documented
technique").

Distinct from `tests/unit/test_activity.py`'s existing coverage: that file's
`_FakePsutilModule`/`_FakeProcess` seam reuses the REAL psutil exception
CLASSES (`psutil.NoSuchProcess`, `psutil.ZombieProcess`, `psutil.AccessDenied`
— imported once at module scope there) so a test proves the implementation's
`except` clauses catch the SAME classes real psutil raises, but it never
calls the REAL `psutil.Process(pid).create_time()` against a REAL OS
process — every `create_time()` value in that file is a hand-supplied float
on a `_FakeProcess`. This file exercises exactly that gap: the raw,
underlying primitives (not `_lease_status`'s decision logic, already covered
there) against the actually-installed library and real, disposable
subprocesses.

Every claim below was directly executed against this repository's installed
psutil (7.2.2, confirmed via `importlib.metadata.version("psutil")`) before
being written as an assertion — see the accompanying pyproject-pin analysis
for the psutil changelog evidence (the single `7.1.0` release's
create_time()/NTP fixes — #2526, #2541, #2570, #2578, all inside that one
section, not spread across 7.1.1-7.1.3, which was an earlier misreading
corrected in `test_pyproject_dependency_pins.py`; and 8.0.0's documented
breaking changes) that this behavioural pin complements. A version bound
protects AGAINST a regression; this file proves the CURRENTLY installed
version has the property the bound exists to preserve.

Hermetic despite touching real processes: every process this file creates is
spawned and reaped by the test itself under a bounded wait, mirroring
`test_activity.py`'s own `_assert_read_call_does_not_hang` discipline — a
hanging child fails the test, never the suite. No network, no container, no
`PARTGRAPH_*` environment variable is read or needed; this file never
imports `partgraph.cli`.

NOT attempted here, disclosed rather than silently skipped: manufacturing a
REAL zombie process (fork + parent never wait()s) to exercise
`psutil.ZombieProcess` end-to-end. That scenario is already covered
behaviourally in `test_activity.py`
(`test_zombie_process_is_treated_as_dead_via_the_real_psutil_exception_hierarchy`,
via the fake seam raising a REAL `psutil.ZombieProcess` instance) and the
`issubclass` relationship the code's single `except` clause actually depends
on is proven directly below without needing a live zombie. A real fork-based
zombie recipe (documented in psutil's own FAQ) adds process-forking risk
inside a pytest worker for no additional coverage this repository's code
depends on, so it is left out rather than added for its own sake.

NOT attempted here either: forcing a REAL PID-recycling event (the same PID
number reused by a genuinely different process) — this is an OS scheduling
decision this test cannot force deterministically, and `test_activity.py`
already covers the recycled-PID DECISION path with an injected fake
(`test_recycled_pid_is_not_mistaken_for_a_live_lease`). What IS proven here
is the primitive that decision depends on: create_time() is stable for a
live process and raises NoSuchProcess for a confirmed-dead one.

EXTENDED for ADR-0025 § 1's "Open risk" (the clock-step defect fixed on
branch `fix/lease-identity-survives-clock-step`): two further tests measure
`create_time() - boot_time()`, the fix's own candidate replacement quantity,
against the real kernel and the real library — supporting evidence for that
fix's design questions, kept here because it is the SAME kind of claim this
file already makes ("measured on the installed library", not quoted from an
ADR). The clock-step DEFECT itself, and the migration contract the fix's
persisted-format change creates, are pinned separately in
`tests/unit/test_lease_survives_clock_step.py`, not here — this file stays
scoped to primitives that are ALREADY true today, never to a not-yet-landed
fix's own behaviour.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time

import psutil
import pytest

from partgraph.util.activity import _CREATE_TIME_TOLERANCE_S

#: Bounded wait for every subprocess this file spawns or reaps — a hanging
#: child must fail the test deterministically, never hang the suite.
_SUBPROCESS_WAIT_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# The exception hierarchy `_lease_status`'s single `except` clause relies on
# ---------------------------------------------------------------------------


def test_zombie_process_is_a_subclass_of_no_such_process() -> None:
    """Given the REAL, installed psutil library.
    When psutil.ZombieProcess and psutil.NoSuchProcess are compared.
    Then ZombieProcess IS a subclass of NoSuchProcess — confirmed directly
    (`issubclass`, not assumed from documentation or memory) against the
    installed interpreter. This is the exact relationship
    `_lease_status`'s docstring claims ("a clean NoSuchProcess...
    (ZombieProcess subclasses it...)") and the property that lets its single
    `except no_such_process:` clause also catch a zombie without a second,
    explicit except arm — a narrower except that matched only a literal
    NoSuchProcess instance would silently stop catching zombies if this
    relationship were ever inverted.
    """
    assert issubclass(psutil.ZombieProcess, psutil.NoSuchProcess)


# ---------------------------------------------------------------------------
# create_time() against a REAL, still-running process
# ---------------------------------------------------------------------------


def test_create_time_of_a_live_process_is_stable_across_repeated_real_reads() -> None:
    """Given a real, currently-running child process (spawned by this test
    and deliberately left running across two reads, not yet reaped).
    When create_time() is read via the REAL installed psutil twice, with a
    real, elapsed wall-clock gap between the two reads (not a bare
    back-to-back double call).
    Then both reads return the SAME value, within activity.py's own
    `_CREATE_TIME_TOLERANCE_S` — the property `_lease_status`'s
    `math.isclose(..., abs_tol=_CREATE_TIME_TOLERANCE_S)` comparison
    depends on to never misclassify a still-alive process as a recycled PID
    under ordinary operation (no system clock change involved here; the
    pre-7.1.0 create_time()/NTP-update bug this repository's psutil floor
    guards against is exercised only under an actual clock step, which this
    test does not attempt to simulate).
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    try:
        first = float(psutil.Process(child.pid).create_time())
        # A real, elapsed gap — not a bare back-to-back call — between the reads.
        time.sleep(0.3)
        second = float(psutil.Process(child.pid).create_time())
    finally:
        child.terminate()
        child.wait(timeout=_SUBPROCESS_WAIT_TIMEOUT_S)

    assert first == pytest.approx(second, abs=_CREATE_TIME_TOLERANCE_S), (
        f"create_time() drifted between two reads of the SAME live process "
        f"({first} vs {second}, diff={abs(first - second)}), beyond the tolerance "
        f"({_CREATE_TIME_TOLERANCE_S}) _lease_status relies on to treat this as one identity."
    )


def test_create_time_of_a_live_process_is_a_finite_float() -> None:
    """Given a real, currently-running process — this test's own pytest
    worker (`os.getpid()`), guaranteed alive for the assertion's duration.
    When create_time() is read via the real psutil and cast with float(),
    exactly as `_create_time_of`/`_lease_status` both do.
    Then the result is a finite float — never NaN, never +/-inf — matching
    the invariant `_read_lease_file`'s own `math.isfinite` guard enforces on
    a value read back from disk. The value is produced here by the real
    library, not hand-constructed, so this pins the library's own contract,
    not merely the reader's defensive check against it.
    """
    value = float(psutil.Process(os.getpid()).create_time())
    assert math.isfinite(value), f"create_time() returned a non-finite float: {value!r}"


# ---------------------------------------------------------------------------
# create_time() against a REAL, confirmed-dead process
# ---------------------------------------------------------------------------


def test_create_time_of_a_terminated_and_reaped_process_raises_no_such_process() -> None:
    """Given a real child process that has ALREADY exited and been reaped
    (`Popen.wait()` returned) — the confirmed-dead case `_lease_status`
    positively confirms before ever cleaning a lease file, not a guess at
    some currently-unused PID number.
    When `psutil.Process(that_pid).create_time()` is called against the REAL
    installed library — the exact expression `_lease_status` evaluates
    inside its own try block.
    Then it raises psutil.NoSuchProcess — proven here without the fake seam
    standing in for it. (Directly observed before being written as an
    assertion: `psutil.NoSuchProcess(pid=<n>, msg='process PID not found')`
    against psutil 7.2.2 on this repository's Linux host.)
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=_SUBPROCESS_WAIT_TIMEOUT_S)  # exits AND is reaped
    dead_pid = child.pid

    with pytest.raises(psutil.NoSuchProcess):
        psutil.Process(dead_pid).create_time()


# ---------------------------------------------------------------------------
# `create_time() - boot_time()` — the seconds-since-boot quantity
# ADR-0025 § 1's "Open risk" names as the fix's own candidate ("persist and
# compare create_time() - boot_time(), the seconds-since-boot form _ident
# itself uses"). These two tests measure, against the REAL installed library
# and the REAL kernel (never assumed from the ADR's own prose), whether that
# reconstruction is numerically sound enough to replace the epoch comparison
# `tests/unit/test_lease_survives_clock_step.py` proves is broken — i.e.
# whether `_CREATE_TIME_TOLERANCE_S` (1e-3) still fits once the quantity being
# compared changes (this fix's design question 3). Both are GREEN today: they
# pin an ALREADY-true property of the installed library and kernel, not the
# fix itself — the fix's own RED tests live in
# `test_lease_survives_clock_step.py`.
# ---------------------------------------------------------------------------


def test_create_time_minus_boot_time_matches_the_kernel_starttime_field_directly() -> None:
    """[decode-before-hypothesise] Given the REAL, currently-running pytest
    worker process, and Linux's own `/proc/<pid>/stat` field 22 (`starttime`
    — "the number of clock ticks since the system booted until the process
    was created", per `proc(5)`) read directly, independent of psutil
    entirely, and converted to seconds via `os.sysconf("SC_CLK_TCK")`.
    When `psutil.Process(pid).create_time() - psutil.boot_time()` (both
    PUBLIC calls — the exact expression ADR-0025 § 1 sketches as the fix) is
    computed for the SAME process.
    Then the two values agree within `_CREATE_TIME_TOLERANCE_S` — proving the
    public-API reconstruction recovers the kernel's own ground-truth
    boot-relative start time, not merely psutil's private `_ident`/`_ctime`
    (a DIFFERENT, unverified source this test deliberately does not consult).
    Linux-only (the `/proc/<pid>/stat` format is a Linux kernel contract);
    skips cleanly on any other platform.
    """
    if sys.platform != "linux":
        pytest.skip("/proc/<pid>/stat is a Linux-specific kernel interface.")

    pid = os.getpid()
    create_time = psutil.Process(pid).create_time()
    boot_time = psutil.boot_time()
    reconstructed_since_boot = create_time - boot_time

    clock_ticks_per_second = os.sysconf("SC_CLK_TCK")
    with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
        raw_stat = handle.read()
    # `comm` (field 2) is parenthesised and may itself contain ')' or spaces,
    # so split on the LAST ')' — the standard, kernel-documented technique —
    # rather than a naive whitespace split from the start of the line.
    fields_after_comm = raw_stat.rpartition(")")[2].split()
    starttime_ticks = int(fields_after_comm[22 - 3])  # field 22, 1-indexed overall
    kernel_since_boot = starttime_ticks / clock_ticks_per_second

    assert reconstructed_since_boot == pytest.approx(
        kernel_since_boot, abs=_CREATE_TIME_TOLERANCE_S
    ), (
        f"create_time() - boot_time() ({reconstructed_since_boot!r}) must match "
        f"the kernel's own starttime field ({kernel_since_boot!r}) within "
        f"_CREATE_TIME_TOLERANCE_S ({_CREATE_TIME_TOLERANCE_S}) for the "
        f"public-API reconstruction to be a safe drop-in for the epoch form"
    )


def test_create_time_minus_boot_time_is_stable_across_repeated_real_reads_with_an_elapsed_gap() -> (
    None
):
    """Given a real, currently-running child process (spawned by this test,
    deliberately left running across two reads).
    When `create_time() - boot_time()` (both PUBLIC calls) is computed TWICE,
    each via a FRESH `psutil.Process` construction (never the same cached
    object — mirroring `_lease_status`'s own per-call construction), with a
    real, elapsed wall-clock gap between the two reads.
    Then both reads agree within `_CREATE_TIME_TOLERANCE_S` — mirroring
    `test_create_time_of_a_live_process_is_stable_across_repeated_real_reads`
    above for the RECONSTRUCTED since-boot quantity: proving it is at least
    as stable, absent an actual clock step, as the epoch form already proven
    stable there — the numerical precondition for design question 1's ruling
    that no widening of `_CREATE_TIME_TOLERANCE_S` is needed for the fix.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    try:
        first = float(psutil.Process(child.pid).create_time()) - float(psutil.boot_time())
        time.sleep(0.3)
        second = float(psutil.Process(child.pid).create_time()) - float(psutil.boot_time())
    finally:
        child.terminate()
        child.wait(timeout=_SUBPROCESS_WAIT_TIMEOUT_S)

    assert first == pytest.approx(second, abs=_CREATE_TIME_TOLERANCE_S), (
        f"create_time() - boot_time() drifted between two reads of the SAME "
        f"live process, absent any clock step ({first} vs {second}, "
        f"diff={abs(first - second)}), beyond _CREATE_TIME_TOLERANCE_S "
        f"({_CREATE_TIME_TOLERANCE_S})"
    )
