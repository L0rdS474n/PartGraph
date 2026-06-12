"""
Tests: AC-AD-1..9 — partgraph.util.resources

Specifies the behaviour of the adaptive resource controller used by embed_write
to regulate batch sizing and pacing based on system load.

Module under test: partgraph.util.resources
  - SystemSnapshot(cpu_count, load_avg_1m, ram_available_fraction)
  - ResourceController.regulate(prev_batch_size, snapshot) -> RegulationDirective
  - RegulationDirective(next_batch_size, pause_seconds)
  - get_system_reader() -> Callable[[], SystemSnapshot]

Design decisions pinned by PR4 plan:
  - Controller is RELATIVE (uses % thresholds, never absolute cpu/RAM numbers).
  - cpu_count 4 vs 64 with same load RATIO -> same directive class (proves no
    absolute core constants).
  - Never grow above max_batch; never shrink below min_batch (>=1).
  - pause_seconds <= max_pause.
  - deterministic: same input -> same output.
  - wait_until_healthy: uses injected sleep (NOT time.sleep), bounded retries.
  - psutil missing: get_system_reader falls back gracefully (cpu_count/getloadavg).
  - cpu_count=1: no div-by-zero; conservative under load.

NOTE: Collection will ERROR on import of partgraph.util.resources because that
module does not exist yet. That is the correct red state before PR4 implementation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from partgraph.util.resources import (  # noqa: F401
    RegulationDirective,
    ResourceController,
    SystemSnapshot,
    get_system_reader,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap(
    *,
    cpu_count: int = 4,
    load_avg_1m: float = 0.5,
    ram_available_fraction: float = 0.4,
) -> SystemSnapshot:
    return SystemSnapshot(
        cpu_count=cpu_count,
        load_avg_1m=load_avg_1m,
        ram_available_fraction=ram_available_fraction,
    )


def _default_controller(**kwargs) -> ResourceController:
    """Create a ResourceController with sensible defaults for unit tests."""
    defaults = {
        "min_batch": 1,
        "max_batch": 256,
        "max_pause": 30.0,
    }
    defaults.update(kwargs)
    return ResourceController(**defaults)


# ===========================================================================
# AC-AD-1: RAM critically low -> pause and/or shrink, never grow
# ===========================================================================

def test_ac_ad_1_low_ram_pause_or_shrink_never_grow() -> None:
    """AC-AD-1: Given ram_available_fraction=0.05 (critically low RAM).
    When regulate(prev_batch_size=64, snapshot) is called.
    Then:
    - next_batch_size <= prev_batch_size (never grow under low RAM).
    - Either pause_seconds > 0 OR next_batch_size < prev_batch_size (or both).
    """
    controller = _default_controller()
    snapshot = _snap(ram_available_fraction=0.05, load_avg_1m=0.1, cpu_count=4)
    directive = controller.regulate(64, snapshot)

    assert directive.next_batch_size <= 64, (
        f"AC-AD-1: next_batch_size must not grow under low RAM. "
        f"Got next_batch_size={directive.next_batch_size} (was 64)."
    )
    assert directive.pause_seconds > 0 or directive.next_batch_size < 64, (
        f"AC-AD-1: under low RAM, must pause > 0 and/or shrink batch. "
        f"Got pause={directive.pause_seconds}, next_batch={directive.next_batch_size}."
    )


# ===========================================================================
# AC-AD-2: High CPU load -> pause/shrink, never grow
# ===========================================================================

def test_ac_ad_2_high_load_pause_or_shrink_never_grow() -> None:
    """AC-AD-2: Given load_avg_1m = 0.9 * cpu_count (high load) and healthy RAM.
    When regulate(prev_batch_size=64, snapshot) is called.
    Then next_batch_size <= prev_batch_size (never grow under high load).
    """
    cpu_count = 4
    controller = _default_controller()
    snapshot = _snap(
        cpu_count=cpu_count,
        load_avg_1m=0.9 * cpu_count,
        ram_available_fraction=0.6,
    )
    directive = controller.regulate(64, snapshot)

    assert directive.next_batch_size <= 64, (
        f"AC-AD-2: next_batch_size must not grow under high CPU load. "
        f"Got next_batch_size={directive.next_batch_size} (was 64)."
    )
    # Under high load, either pause>0 or shrink must occur.
    assert directive.pause_seconds > 0 or directive.next_batch_size < 64, (
        f"AC-AD-2: under high load, must pause or shrink. "
        f"Got pause={directive.pause_seconds}, next_batch={directive.next_batch_size}."
    )


# ===========================================================================
# AC-AD-3: Healthy system -> grow (bounded) + zero pause
# ===========================================================================

def test_ac_ad_3_healthy_system_grows_zero_pause() -> None:
    """AC-AD-3: Given ram=0.6 (healthy) and load=0.2*cpu_count (low).
    When regulate(prev_batch_size=32, snapshot) is called (batch well below max).
    Then:
    - next_batch_size >= prev_batch_size (grow or stay — never shrink on healthy).
    - pause_seconds == 0 (no pause needed on a healthy system).
    """
    cpu_count = 4
    controller = _default_controller(max_batch=256)
    snapshot = _snap(
        cpu_count=cpu_count,
        load_avg_1m=0.2 * cpu_count,
        ram_available_fraction=0.6,
    )
    directive = controller.regulate(32, snapshot)

    assert directive.next_batch_size >= 32, (
        f"AC-AD-3: must not shrink on a healthy system. "
        f"Got next_batch_size={directive.next_batch_size} (was 32)."
    )
    assert directive.pause_seconds == 0.0, (
        f"AC-AD-3: pause must be 0 on a healthy system. "
        f"Got pause_seconds={directive.pause_seconds}."
    )


# ===========================================================================
# AC-AD-4: Relative thresholds — same load RATIO on cpu_count=4 vs cpu_count=64
#           produces the same directive CLASS (both pause/shrink)
# ===========================================================================

def test_ac_ad_4_relative_thresholds_same_ratio_same_class() -> None:
    """AC-AD-4: Given the same load RATIO (0.9*cpu_count) on different cpu_counts.
    When regulate is called for cpu_count=4 and cpu_count=64.
    Then BOTH directives are in the same class:
    - Both pause/shrink (or both grow) — the behaviour does not flip based on
      absolute cpu_count.

    This test PROVES the controller uses no absolute core constants. A controller
    that hardcodes "load > 8" would give a different result for cpu_count=4 (load=3.6)
    vs cpu_count=64 (load=57.6) even though both are at 90% utilisation.
    """
    controller = _default_controller()

    snap_4 = _snap(cpu_count=4, load_avg_1m=0.9 * 4, ram_available_fraction=0.6)
    snap_64 = _snap(cpu_count=64, load_avg_1m=0.9 * 64, ram_available_fraction=0.6)

    directive_4 = controller.regulate(64, snap_4)
    directive_64 = controller.regulate(64, snap_64)

    # Both must be the same class: both pause/shrink or both no-action/grow.
    def _is_stressed(d: RegulationDirective, prev: int) -> bool:
        return d.pause_seconds > 0 or d.next_batch_size < prev

    stressed_4 = _is_stressed(directive_4, 64)
    stressed_64 = _is_stressed(directive_64, 64)

    assert stressed_4 == stressed_64, (
        f"AC-AD-4: same load RATIO (0.9*cpu) must produce same directive class "
        f"regardless of absolute cpu_count. "
        f"cpu=4: pause={directive_4.pause_seconds}, next={directive_4.next_batch_size} "
        f"(stressed={stressed_4}). "
        f"cpu=64: pause={directive_64.pause_seconds}, next={directive_64.next_batch_size} "
        f"(stressed={stressed_64}). "
        f"This means the controller uses an absolute cpu threshold — must be relative."
    )


# ===========================================================================
# AC-AD-5: Bounded output (never > max_batch, never < min_batch, pause <= max_pause)
# ===========================================================================

def test_ac_ad_5_bounded_output_never_exceeds_limits() -> None:
    """AC-AD-5: Given any system state and any prev_batch_size within range.
    When regulate is called.
    Then:
    - next_batch_size >= min_batch (>= 1).
    - next_batch_size <= max_batch.
    - pause_seconds >= 0 and <= max_pause.
    """
    controller = _default_controller(min_batch=4, max_batch=128, max_pause=10.0)

    snapshots = [
        _snap(cpu_count=4, load_avg_1m=3.6, ram_available_fraction=0.05),  # stressed
        _snap(cpu_count=4, load_avg_1m=0.1, ram_available_fraction=0.8),   # healthy
        _snap(cpu_count=1, load_avg_1m=0.95, ram_available_fraction=0.5),  # single cpu high
    ]
    batch_sizes = [1, 4, 64, 128, 256]

    for snap in snapshots:
        for bs in batch_sizes:
            d = controller.regulate(bs, snap)
            assert d.next_batch_size >= 4, (
                f"AC-AD-5: next_batch_size must be >= min_batch=4. "
                f"Got {d.next_batch_size} for prev={bs}."
            )
            assert d.next_batch_size <= 128, (
                f"AC-AD-5: next_batch_size must be <= max_batch=128. "
                f"Got {d.next_batch_size} for prev={bs}."
            )
            assert d.pause_seconds >= 0.0, (
                f"AC-AD-5: pause_seconds must be >= 0. Got {d.pause_seconds}."
            )
            assert d.pause_seconds <= 10.0, (
                f"AC-AD-5: pause_seconds must be <= max_pause=10.0. Got {d.pause_seconds}."
            )


# ===========================================================================
# AC-AD-6: Deterministic / pure — same input same output, no global state
# ===========================================================================

def test_ac_ad_6_deterministic_pure_same_input_same_output() -> None:
    """AC-AD-6: Given the same SystemSnapshot and prev_batch_size.
    When regulate is called twice.
    Then both RegulationDirective results are identical.
    (Pure function — no internal state mutation, no randomness.)
    """
    controller = _default_controller()
    snap = _snap(cpu_count=4, load_avg_1m=2.5, ram_available_fraction=0.3)

    d1 = controller.regulate(32, snap)
    d2 = controller.regulate(32, snap)

    assert d1.next_batch_size == d2.next_batch_size, (
        f"AC-AD-6: regulate must be deterministic. "
        f"Got next_batch_size {d1.next_batch_size} vs {d2.next_batch_size}."
    )
    assert d1.pause_seconds == d2.pause_seconds, (
        f"AC-AD-6: regulate must be deterministic. "
        f"Got pause_seconds {d1.pause_seconds} vs {d2.pause_seconds}."
    )


# ===========================================================================
# AC-AD-7: wait_until_healthy — uses injected sleep, bounded retries
# ===========================================================================

def test_ac_ad_7_wait_until_healthy_injected_sleep_not_real() -> None:
    """AC-AD-7: Given a reader scripted to return stressed snapshots (0.05 RAM) 3 times
    then a healthy snapshot (0.5 RAM), and an injected sleep callable.
    When wait_until_healthy(reader, sleep=injected_sleep) is called.
    Then:
    - The injected sleep callable is called at least once (waited for recovery).
    - time.sleep is NOT called (only injected sleep is used).
    - The function returns after the healthy snapshot is observed (bounded).
    """
    import time as _time_mod

    # Script: 3 stressed reads, then 1 healthy read.
    reads = [
        _snap(cpu_count=4, load_avg_1m=3.6, ram_available_fraction=0.05),
        _snap(cpu_count=4, load_avg_1m=3.6, ram_available_fraction=0.05),
        _snap(cpu_count=4, load_avg_1m=3.6, ram_available_fraction=0.05),
        _snap(cpu_count=4, load_avg_1m=0.2, ram_available_fraction=0.5),
    ]
    read_idx = [0]

    def _scripted_reader() -> SystemSnapshot:
        snap = reads[min(read_idx[0], len(reads) - 1)]
        read_idx[0] += 1
        return snap

    injected_sleep = MagicMock()

    # Ban time.sleep to catch accidental direct usage.
    original_sleep = _time_mod.sleep

    def _banned_sleep(duration: float) -> None:
        raise AssertionError(
            f"time.sleep({duration}) called directly; injected sleep= must be used."
        )

    _time_mod.sleep = _banned_sleep
    try:
        controller = _default_controller()
        controller.wait_until_healthy(
            reader=_scripted_reader,
            sleep=injected_sleep,
        )
    finally:
        _time_mod.sleep = original_sleep

    assert injected_sleep.call_count >= 1, (
        f"AC-AD-7: injected sleep must be called at least once for stressed reads. "
        f"Got call_count={injected_sleep.call_count}."
    )


# ===========================================================================
# AC-AD-8: cpu_count=1 — no div-by-zero; conservative under load
# ===========================================================================

def test_ac_ad_8_cpu_count_1_no_div_by_zero_conservative() -> None:
    """AC-AD-8: Given cpu_count=1 and load_avg_1m=0.95 (high utilisation).
    When regulate is called.
    Then:
    - No exception is raised (no div-by-zero or ZeroDivisionError).
    - Result is conservative: pause > 0 or next_batch < prev (not grows under high load).
    """
    controller = _default_controller()
    snap = _snap(cpu_count=1, load_avg_1m=0.95, ram_available_fraction=0.6)

    # Must not raise.
    directive = controller.regulate(32, snap)

    # Conservative under load: should not grow.
    assert directive.next_batch_size <= 32, (
        f"AC-AD-8: cpu_count=1 high load must be conservative (not grow). "
        f"Got next_batch_size={directive.next_batch_size} (was 32)."
    )


def test_ac_ad_8_cpu_count_1_idle_no_crash() -> None:
    """AC-AD-8 (idle): Given cpu_count=1 and load_avg_1m=0.0 (idle).
    When regulate is called.
    Then no exception is raised.
    """
    controller = _default_controller()
    snap = _snap(cpu_count=1, load_avg_1m=0.0, ram_available_fraction=0.8)

    # Must not raise.
    directive = controller.regulate(16, snap)

    assert directive.next_batch_size >= 1, (
        f"AC-AD-8: next_batch_size must be >= 1 even on idle single-cpu. "
        f"Got {directive.next_batch_size}."
    )


# ===========================================================================
# AC-AD-9: psutil missing -> get_system_reader fallback
# ===========================================================================

def test_ac_ad_9_psutil_missing_get_system_reader_fallback() -> None:
    """AC-AD-9: Given psutil is NOT installed (patched out of sys.modules).
    When get_system_reader() is called and the returned reader is invoked.
    Then:
    - No ImportError or crash.
    - Returns a callable reader.
    - The reader returns a SystemSnapshot.
    - ram_available_fraction may be None (psutil unavailable => fraction unavailable).
    - cpu_count is populated from os.cpu_count().
    - regulate() still works (treats None ram as tight/conservative).
    """
    import importlib
    import sys as _sys

    # Temporarily hide psutil.
    original_psutil = _sys.modules.get("psutil")
    _sys.modules["psutil"] = None  # type: ignore[assignment]

    try:
        # Re-import the resources module to force re-evaluation without psutil.
        import partgraph.util.resources as resources_mod
        importlib.reload(resources_mod)

        reader = resources_mod.get_system_reader()
        assert callable(reader), "AC-AD-9: get_system_reader() must return a callable."

        snap = reader()
        assert isinstance(snap, resources_mod.SystemSnapshot), (
            f"AC-AD-9: reader must return SystemSnapshot. Got: {type(snap)!r}"
        )
        assert snap.cpu_count >= 1, (
            f"AC-AD-9: cpu_count must be >= 1 from os.cpu_count(). Got: {snap.cpu_count}."
        )
        # ram_available_fraction is None when psutil is absent.
        assert snap.ram_available_fraction is None or isinstance(
            snap.ram_available_fraction, float
        ), (
            f"AC-AD-9: ram_available_fraction must be None or float. "
            f"Got: {snap.ram_available_fraction!r}"
        )

        # Controller must handle None ram (treats as tight/conservative).
        controller = _default_controller()
        directive = controller.regulate(32, snap)
        # Must not raise; result is bounded.
        assert directive.next_batch_size >= 1, (
            f"AC-AD-9: regulate must work with None ram. "
            f"Got next_batch_size={directive.next_batch_size}."
        )
    finally:
        # Restore psutil in sys.modules.
        if original_psutil is not None:
            _sys.modules["psutil"] = original_psutil
        else:
            _sys.modules.pop("psutil", None)


# ===========================================================================
# Structural / return type contracts
# ===========================================================================

def test_regulation_directive_has_required_fields() -> None:
    """Given a RegulationDirective.
    When its fields are inspected.
    Then it has next_batch_size (int) and pause_seconds (float).
    """
    controller = _default_controller()
    snap = _snap()
    d = controller.regulate(32, snap)

    assert isinstance(d, RegulationDirective), (
        f"regulate must return RegulationDirective; got {type(d)!r}"
    )
    assert hasattr(d, "next_batch_size"), "RegulationDirective must have next_batch_size."
    assert hasattr(d, "pause_seconds"), "RegulationDirective must have pause_seconds."
    assert isinstance(d.next_batch_size, int), (
        f"next_batch_size must be int; got {type(d.next_batch_size)!r}"
    )
    assert isinstance(d.pause_seconds, (int, float)), (
        f"pause_seconds must be numeric; got {type(d.pause_seconds)!r}"
    )


def test_system_snapshot_has_required_fields() -> None:
    """Given a SystemSnapshot constructed with all fields.
    When its fields are inspected.
    Then cpu_count, load_avg_1m, ram_available_fraction are accessible.
    """
    snap = SystemSnapshot(cpu_count=8, load_avg_1m=1.5, ram_available_fraction=0.4)

    assert snap.cpu_count == 8
    assert snap.load_avg_1m == pytest.approx(1.5)
    assert snap.ram_available_fraction == pytest.approx(0.4)
