"""
Tests: PR-B2 (feat/db-lazy-autostart) — `partgraph.util.lifecycle.ensure_running`
(ADR-0022 Section 7, AC B-1..B-5).

ADR-0022 shipped in two PRs. PR-B1 (already landed, `docs/db-lifecycle.md` +
`db doctor`) removed the ONLY thing that used to start the database implicitly
(the quadlet unit's `WantedBy=default.target`). PR-B2 closes the gap that
opens: "the database no longer starts by itself, so a command that needs it
must say so, or start it" (ADR-0022 Section 7). This file specifies the leaf
half of that: a NEW function, `ensure_running()`, added to the ALREADY-landed
leaf module `partgraph.util.lifecycle` (PR-A/PR-B1; see
`tests/unit/test_lifecycle.py` / `tests/unit/test_lifecycle_volume.py`).

Split into its OWN file — never appended to `test_lifecycle.py` — for the
SAME reason `tests/unit/test_lifecycle_volume.py` (PR-B1's `volume_exists()`)
was: `test_lifecycle.py`'s own top-level `from partgraph.util.lifecycle import
(...)` already collects successfully today (PR-A/PR-B1 landed), so appending
an import of a NOT-YET-EXISTING symbol (`ensure_running`) to THAT shared
import list would turn PR-A's own, separately-passing suite red merely
because one more name was appended. Scoping the new import to ONLY this file
isolates the RED-phase collection failure (`ImportError`) to the new
addition.

Pinned contract (NOT YET IMPLEMENTED — collection of THIS file is expected to
ERROR with ImportError until it exists; the correct test-first RED state):

  ``AUTOSTART_READY_TIMEOUT_S: float`` — the named, finite, bounded wall-clock
  budget (ADR-0007 bounded-constant precedent, mirrors
  ``INSPECT_SWEEP_BUDGET_S``/``STOP_TIMEOUT_S``) `ensure_running()` will poll
  Dgraph's health endpoint for AFTER issuing the start command, before giving
  up. Pinned here as "finite, positive, bounded" only — never a specific
  number: this file has no measured evidence for what Dgraph's real
  first-run startup time is on any host (unlike ``STOP_GRACE_SECONDS``, which
  IS backed by a live measurement recorded in ``test_lifecycle.py``), so
  pinning an exact value here would be exactly the kind of unverified,
  invented "measured requirement" CONTRIBUTING.md's discipline forbids.

  ``AUTOSTART_POLL_INTERVAL_S: float`` — the named, finite, bounded delay
  between two health polls. Pinned as "finite, positive, bounded, and
  strictly less than AUTOSTART_READY_TIMEOUT_S" (so more than one poll is
  structurally possible; a poll interval greater than or equal to the whole
  budget would mean at most one poll ever happens, which defeats the point
  of a bounded RETRY loop rather than a bounded single wait).

  ``class AutostartTimeoutError(RuntimeError)`` — raised when the readiness
  poll exhausts ``AUTOSTART_READY_TIMEOUT_S`` without Dgraph ever reporting
  healthy. Mirrors ``ContainerEngineError``'s precedent (an EXCEPTION, not a
  message-free DTO like ``DownResult``/``HealthResult``): its own ``str()``
  IS the complete, human-readable, path-free, SINGLE-LINE message — printed
  verbatim by ``partgraph.cli`` (`f"[red]Error:[/red] {exc}"`, the same
  pattern already used for ``ContainerEngineError`` in `_run_compose`/`down`).
  It contains the formatted budget (``f"{AUTOSTART_READY_TIMEOUT_S:.0f}s"``,
  the SAME ``:.0f`` format `_run_compose`'s own timeout message already uses)
  and the literal substring ``"partgraph db status"`` (B-3's "suggesting
  `partgraph db status`" — the one command that can tell an operator whether
  the wait eventually succeeded after `partgraph` itself gave up on it).

  ``ensure_running(*, probe_health: Callable[[], Any], compose_up: Callable[[],
  None], sleep: Callable[[float], None] | None = None, monotonic:
  Callable[[], float] | None = None) -> None``

  ``probe_health``/``compose_up`` are REQUIRED, keyword-only, and carry NO
  default — mirrors ``stop_all()``'s own ``compose_down`` (Gate 3b finding
  3b-M1 in `test_lifecycle.py`): a caller must decide EXPLICITLY how health is
  checked and how the database is started, rather than silently inheriting a
  permissive default. ``compose_up`` is named to mirror ``compose_down``
  (the symmetric injected seam `db up`'s own `_run_compose(["up", "-d"],
  action="start")` is expected to be threaded through as, exactly like
  `db down` threads its OWN `_run_compose(["down"], ...)` through
  ``stop_all``'s ``compose_down``).

  ``sleep``/``monotonic`` default to ``None`` and are resolved LAZILY, at CALL
  time, to ``time.sleep``/``time.monotonic`` — mirrors this module's own
  ``_resolve_which`` precedent ("Resolved at CALL time (never captured as a
  parameter default) so a test that patches [the underlying stdlib callable]
  globally is honoured"). This is why `partgraph.cli` never needs to pass
  either explicitly in production: only tests inject fakes.

  Behaviour (B-1..B-5):
    1. Call ``probe_health()``. If ``.healthy`` is truthy, RETURN
       immediately — ``compose_up`` is NEVER called and NOTHING is ever
       slept on (B-1: the healthy case costs one HTTP probe and nothing
       else — zero container-engine subprocesses, since ``compose_up`` is
       the ONLY seam that can ever reach one).
    2. Otherwise, call ``compose_up()`` EXACTLY ONCE. Any exception it
       raises is ABSORBED here, never propagated — the readiness poll
       (step 3) is what decides the outcome, not whether the start command
       itself reported success (B-5: "No fabricated lock — health is the
       truth". A concurrent second invocation's ``compose_up`` can
       genuinely fail — e.g. the engine's own "container name already in
       use" — while the database it raced against is, or is about to be,
       healthy; treating that failure as authoritative would report a
       false negative for a command that is about to succeed).
    3. Compute ``deadline = monotonic() + AUTOSTART_READY_TIMEOUT_S`` ONCE,
       right after ``compose_up()`` returns (or its exception was
       absorbed) — never before it, and never per-iteration. Then loop:
       ``sleep(AUTOSTART_POLL_INTERVAL_S)``, then ``probe_health()``; if
       healthy, RETURN; otherwise, if ``monotonic() >= deadline``, raise
       ``AutostartTimeoutError``; otherwise, loop again.

This file's hermetic style mirrors `test_lifecycle.py`/`test_lifecycle_volume.py`
exactly: every seam (`probe_health`, `compose_up`, `sleep`, `monotonic`) is
injected as a plain callable/`MagicMock` — no test in this file sleeps for a
real duration, opens a socket, starts a container, or reads the real wall
clock. The bounded-timeout tests (B-3/B-4) control `monotonic()`'s return
sequence directly, computed as fractions/multiples of the REAL
`AUTOSTART_POLL_INTERVAL_S`/`AUTOSTART_READY_TIMEOUT_S` constants (never a
hardcoded literal), so they stay correct across any future change to either
constant's value and never depend on real elapsed time.
"""

from __future__ import annotations

import inspect
import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Before implementation, this import raises ImportError — the correct
# test-first RED state, since `ensure_running`/`AUTOSTART_READY_TIMEOUT_S`/
# `AUTOSTART_POLL_INTERVAL_S`/`AutostartTimeoutError` do not exist yet.
from partgraph.util.lifecycle import (  # noqa: E402
    AUTOSTART_POLL_INTERVAL_S,
    AUTOSTART_READY_TIMEOUT_S,
    AutostartTimeoutError,
    ensure_running,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_HEALTHY = SimpleNamespace(healthy=True)
_UNHEALTHY = SimpleNamespace(healthy=False)


def _probe_sequence(*results: SimpleNamespace) -> MagicMock:
    """Return a MagicMock probe_health() that yields *results* in order."""
    return MagicMock(side_effect=list(results))


# ---------------------------------------------------------------------------
# CONTRACT — module constants
# ---------------------------------------------------------------------------


def test_autostart_ready_timeout_is_a_finite_positive_bounded_float() -> None:
    """Given AUTOSTART_READY_TIMEOUT_S is the readiness-poll wall-clock budget.
    When the constant is read directly.
    Then it is a finite float strictly greater than zero — never
    None/unbounded (mirrors INSPECT_SWEEP_BUDGET_S/STOP_TIMEOUT_S's own
    ADR-0007 bounded-constant precedent).
    """
    assert isinstance(AUTOSTART_READY_TIMEOUT_S, float)
    assert math.isfinite(AUTOSTART_READY_TIMEOUT_S)
    assert AUTOSTART_READY_TIMEOUT_S > 0


def test_autostart_poll_interval_is_a_finite_positive_bounded_float_smaller_than_the_budget() -> None:
    """Given AUTOSTART_POLL_INTERVAL_S is the delay between two health polls.
    When the constant is read directly.
    Then it is a finite float strictly greater than zero AND strictly less
    than AUTOSTART_READY_TIMEOUT_S — a poll interval at or above the whole
    budget would mean at most one poll could ever happen, defeating the
    point of a bounded RETRY loop.
    """
    assert isinstance(AUTOSTART_POLL_INTERVAL_S, float)
    assert math.isfinite(AUTOSTART_POLL_INTERVAL_S)
    assert AUTOSTART_POLL_INTERVAL_S > 0
    assert AUTOSTART_POLL_INTERVAL_S < AUTOSTART_READY_TIMEOUT_S


def test_autostart_timeout_error_is_a_runtime_error_subclass() -> None:
    """Given AutostartTimeoutError is the leaf's own timeout exception.
    When the class is inspected directly.
    Then it subclasses RuntimeError (mirrors ContainerEngineError's own
    precedent: a leaf-raised, CLI-caught RuntimeError subclass, never a bare
    Exception and never a builtin).
    """
    assert issubclass(AutostartTimeoutError, RuntimeError)


# ---------------------------------------------------------------------------
# CONTRACT — ensure_running()'s own signature
# ---------------------------------------------------------------------------


def test_probe_health_and_compose_up_are_required_keyword_only_with_no_default() -> None:
    """[Mirrors stop_all()'s compose_down, Gate 3b finding 3b-M1] Given a
    caller must decide EXPLICITLY how health is probed and how the database
    is started.
    When ensure_running()'s signature is inspected.
    Then both `probe_health` and `compose_up` are KEYWORD_ONLY and carry NO
    default (inspect.Parameter.empty).
    """
    sig = inspect.signature(ensure_running)
    for name in ("probe_health", "compose_up"):
        param = sig.parameters[name]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name!r} must be keyword-only: {sig}"
        )
        assert param.default is inspect.Parameter.empty, (
            f"{name!r} must carry no default: {sig}"
        )


def test_sleep_and_monotonic_default_to_none_and_are_keyword_only() -> None:
    """Given sleep/monotonic are OPTIONAL injected seams, resolved lazily.
    When ensure_running()'s signature is inspected.
    Then both are keyword-only and default to None (never bound to
    time.sleep/time.monotonic AT DEFINITION TIME, which would defeat a test
    that patches the real time module afterward — mirrors _resolve_which's
    own "resolved at call time" precedent).
    """
    sig = inspect.signature(ensure_running)
    for name in ("sleep", "monotonic"):
        param = sig.parameters[name]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name!r} must be keyword-only: {sig}"
        )
        assert param.default is None, (
            f"{name!r} must default to None (resolved lazily), not a bound "
            f"callable captured at definition time: {sig}"
        )


def test_sleep_and_monotonic_resolve_to_real_time_module_when_not_given() -> None:
    """Given sleep/monotonic are None (the default, as production code will
    call it).
    When ensure_running() is called WITHOUT passing either explicitly, and
    `time.sleep`/`time.monotonic` are patched globally.
    Then the PATCHED `time.sleep`/`time.monotonic` are used — proving
    resolution happens at CALL time via a plain `time.sleep`/`time.monotonic`
    lookup, not a reference captured when the module was first imported.
    """
    probe_health = _probe_sequence(_UNHEALTHY, _HEALTHY)
    compose_up = MagicMock()

    with (
        patch("time.sleep") as mock_sleep,
        patch("time.monotonic", return_value=0.0) as mock_monotonic,
    ):
        ensure_running(probe_health=probe_health, compose_up=compose_up)

    mock_sleep.assert_called_once_with(AUTOSTART_POLL_INTERVAL_S)
    assert mock_monotonic.called


# ---------------------------------------------------------------------------
# B-1 — healthy short-circuit: zero container-engine subprocesses
# ---------------------------------------------------------------------------


def test_b1_already_healthy_never_calls_compose_up_or_sleeps() -> None:
    """B-1: Given probe_health() reports healthy on the FIRST call.
    When ensure_running() is called.
    Then it returns immediately: compose_up() is NEVER called, sleep() is
    NEVER called, and probe_health() is called exactly once — the healthy
    case costs one HTTP probe and nothing else.
    """
    probe_health = MagicMock(return_value=_HEALTHY)
    compose_up = MagicMock()
    sleep = MagicMock()
    monotonic = MagicMock()

    ensure_running(
        probe_health=probe_health, compose_up=compose_up, sleep=sleep, monotonic=monotonic,
    )

    compose_up.assert_not_called()
    sleep.assert_not_called()
    monotonic.assert_not_called()
    probe_health.assert_called_once_with()


# ---------------------------------------------------------------------------
# B-2 — unhealthy: start exactly once, then poll until healthy
# ---------------------------------------------------------------------------


def test_b2_unhealthy_starts_exactly_once_then_polls_until_healthy() -> None:
    """B-2: Given probe_health() reports unhealthy, then unhealthy again on
    the first poll, then healthy on the second poll.
    When ensure_running() is called (with a monotonic clock that never
    exceeds the deadline, isolating THIS scenario from B-3's timeout path).
    Then compose_up() is invoked EXACTLY once, sleep() is called exactly
    twice (once per poll iteration) with AUTOSTART_POLL_INTERVAL_S each
    time, probe_health() is called exactly three times total (the initial
    check + two polls), and the function returns normally (the caller's own
    DB work may now proceed).
    """
    probe_health = _probe_sequence(_UNHEALTHY, _UNHEALTHY, _HEALTHY)
    compose_up = MagicMock()
    sleep = MagicMock()
    # A constant, never-exceeded monotonic reading: deadline = 0.0 + budget
    # (budget > 0), and every check `monotonic() >= deadline` reads
    # `0.0 >= budget`, which is always False. The loop's own STOP condition
    # is therefore driven entirely by probe_health() finally reporting
    # healthy, not by the timeout — isolating this test from B-3's path.
    monotonic = MagicMock(return_value=0.0)

    ensure_running(
        probe_health=probe_health, compose_up=compose_up, sleep=sleep, monotonic=monotonic,
    )

    compose_up.assert_called_once_with()
    assert probe_health.call_count == 3
    assert sleep.call_count == 2
    for call in sleep.call_args_list:
        assert call.args == (AUTOSTART_POLL_INTERVAL_S,), (
            f"every poll sleep must use AUTOSTART_POLL_INTERVAL_S, never a "
            f"hard-coded literal: {call.args!r}"
        )


# ---------------------------------------------------------------------------
# B-3 — bounded readiness wait: named budget, injected clock, never real sleep
# ---------------------------------------------------------------------------


def test_b3_never_becomes_healthy_raises_after_a_bounded_poll_count_never_real_time() -> None:
    """B-3: Given the DB never becomes healthy (probe_health always
    unhealthy), and an injected monotonic() clock scripted to report "not
    yet exceeded" twice and "exceeded" on the third check — computed as
    fractions/multiples of the REAL AUTOSTART_POLL_INTERVAL_S/
    AUTOSTART_READY_TIMEOUT_S constants, never a hard-coded literal, so this
    stays correct across any future change to either constant.
    When ensure_running() is called.
    Then AutostartTimeoutError is raised; sleep() was called EXACTLY three
    times (the poll count is bounded by, and directly attributable to, the
    injected clock — never real elapsed wall time, since `sleep` here is a
    MagicMock that never actually sleeps), each call passing
    AUTOSTART_POLL_INTERVAL_S; and the exception's own message is a single,
    path-free line naming the budget and suggesting `partgraph db status`.
    """
    probe_health = MagicMock(return_value=_UNHEALTHY)
    compose_up = MagicMock()
    sleep = MagicMock()
    monotonic = MagicMock(
        side_effect=[
            0.0,  # deadline = 0.0 + AUTOSTART_READY_TIMEOUT_S
            AUTOSTART_POLL_INTERVAL_S * 0.25,  # check 1: not yet exceeded
            AUTOSTART_POLL_INTERVAL_S * 0.5,  # check 2: not yet exceeded
            AUTOSTART_READY_TIMEOUT_S + 1.0,  # check 3: exceeded
        ]
    )

    with pytest.raises(AutostartTimeoutError) as excinfo:
        ensure_running(
            probe_health=probe_health, compose_up=compose_up, sleep=sleep, monotonic=monotonic,
        )

    assert sleep.call_count == 3, (
        f"expected exactly 3 poll iterations driven by the injected clock, "
        f"got {sleep.call_count}"
    )
    for call in sleep.call_args_list:
        assert call.args == (AUTOSTART_POLL_INTERVAL_S,)

    message = str(excinfo.value)
    assert f"{AUTOSTART_READY_TIMEOUT_S:.0f}s" in message, (
        f"the timeout message must name the budget: {message!r}"
    )
    assert "partgraph db status" in message, (
        f"the timeout message must suggest `partgraph db status`: {message!r}"
    )
    assert "\n" not in message, f"the timeout message must be a single line: {message!r}"
    assert "/" not in message, f"the timeout message must be path-free: {message!r}"


# ---------------------------------------------------------------------------
# B-4 — start command exits non-zero and the DB never recovers: clean failure
# ---------------------------------------------------------------------------


def test_b4_start_failure_that_never_recovers_still_times_out_cleanly() -> None:
    """B-4: Given compose_up() raises (mirrors `_run_compose` re-raising
    typer.Exit when the engine's `up -d` exits non-zero) AND probe_health()
    NEVER reports healthy afterward — a genuine, unrecoverable start
    failure, distinct from B-5's race.
    When ensure_running() is called (with an injected clock that exhausts
    the budget after exactly one poll, so this test stays fast and
    deterministic).
    Then AutostartTimeoutError is raised (the leaf's own clean, catchable
    failure signal — `partgraph.cli` is expected to turn this into "exit
    non-zero with a clear message, no traceback"), compose_up() was called
    EXACTLY once despite raising (never retried), and the DB work never got
    a chance to run (ensure_running() never returned normally).
    """

    def _compose_up_raises() -> None:
        raise RuntimeError("the container engine exited with code 125")

    probe_health = MagicMock(return_value=_UNHEALTHY)
    compose_up = MagicMock(side_effect=_compose_up_raises)
    sleep = MagicMock()
    monotonic = MagicMock(side_effect=[0.0, AUTOSTART_READY_TIMEOUT_S + 1.0])

    with pytest.raises(AutostartTimeoutError):
        ensure_running(
            probe_health=probe_health, compose_up=compose_up, sleep=sleep, monotonic=monotonic,
        )

    compose_up.assert_called_once_with()
    assert sleep.call_count == 1, (
        "the DB is still polled at least once even after a start failure — "
        "only the deadline decides when to give up"
    )


# ---------------------------------------------------------------------------
# B-5 — concurrency honesty: a start failure never blocks a health recovery
# ---------------------------------------------------------------------------


def test_b5_start_failure_absorbed_when_health_recovers_no_fabricated_lock() -> None:
    """B-5: Given TWO `partgraph` invocations raced to start the database,
    and THIS one's compose_up() fails because the container name is
    already in use (the OTHER invocation won the race) — modelled here as
    compose_up() raising — but probe_health() reports unhealthy once more,
    then healthy (the OTHER invocation's start is completing).
    When ensure_running() is called (with a monotonic clock that never
    exceeds the deadline, isolating this from B-3/B-4's timeout path).
    Then it returns NORMALLY — no exception propagates — proving the start
    command's own reported outcome is NEVER authoritative; only the health
    probe is ("No fabricated lock — health is the truth"). compose_up() was
    still invoked exactly once (the attempt itself is real and not
    skipped), and probe_health() was polled until it actually reported
    healthy.
    """

    def _compose_up_raises() -> None:
        raise RuntimeError("container name \"partgraph-dgraph\" is already in use")

    probe_health = _probe_sequence(_UNHEALTHY, _UNHEALTHY, _HEALTHY)
    compose_up = MagicMock(side_effect=_compose_up_raises)
    sleep = MagicMock()
    monotonic = MagicMock(return_value=0.0)  # never exceeds the deadline

    ensure_running(
        probe_health=probe_health, compose_up=compose_up, sleep=sleep, monotonic=monotonic,
    )  # must not raise

    compose_up.assert_called_once_with()
    assert probe_health.call_count == 3
