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
