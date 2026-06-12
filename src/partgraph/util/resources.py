"""Adaptive resource controller for the PR4 embedding pipeline.

This is a **leaf** module: it depends only on the Python standard library plus
the optional :mod:`psutil` package. It must never import ``partgraph.embed``,
``partgraph.query``, ``partgraph.load`` or ``partgraph.cli`` so it can be reused
freely (by both the embed run and the loader) without creating import cycles.

What it provides
----------------
- :class:`SystemSnapshot` — an immutable reading of CPU/RAM pressure.
- :class:`RegulationDirective` — the controller's answer: the next batch size
  and how long to pause before the next batch.
- :class:`ResourceController` — turns a snapshot into a directive using
  **relative** thresholds (fractions of ``os.cpu_count()`` and of available
  RAM), so the behaviour is identical on a 4-core and a 64-core box at the same
  utilisation. No absolute core/byte constants appear anywhere.
- :func:`get_system_reader` — returns a callable producing a live snapshot,
  degrading gracefully (CPU-only, ``ram_available_fraction=None``) when psutil
  is not installed.

Determinism
-----------
:meth:`ResourceController.regulate` is a pure function of its inputs (no global
state, no randomness, no wall-clock reads), so the same snapshot always yields
the same directive. Readings and sleeping are injected into
:meth:`ResourceController.wait_until_healthy` so tests stay hermetic and never
touch the real clock.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "RegulationDirective",
    "ResourceController",
    "SystemSnapshot",
    "get_system_reader",
]

# ---------------------------------------------------------------------------
# Relative thresholds (fractions — never absolute core counts or byte sizes).
# ---------------------------------------------------------------------------

#: Load ratio (load_avg_1m / cpu_count) at/above which the system is "busy" and
#: the controller must not grow the batch.
_LOAD_BUSY_RATIO = 0.7
#: Load ratio at/above which the system is "stressed": shrink and pause.
_LOAD_STRESS_RATIO = 0.85
#: Available-RAM fraction below which RAM is "tight" (no grow).
_RAM_TIGHT_FRACTION = 0.25
#: Available-RAM fraction below which RAM is "critical": shrink and pause.
_RAM_CRITICAL_FRACTION = 0.10
#: Available-RAM fraction at/above which RAM is comfortably healthy.
_RAM_HEALTHY_FRACTION = 0.35

#: Multiplicative factor applied when shrinking the batch under stress.
_SHRINK_FACTOR = 0.5
#: Multiplicative factor applied when growing the batch on a healthy system.
_GROW_FACTOR = 2.0


@dataclass(frozen=True)
class SystemSnapshot:
    """Snapshot of current system resource usage.

    Attributes:
        cpu_count: Number of logical CPUs (always ``>= 1``).
        load_avg_1m: 1-minute load average (``>= 0.0``).
        ram_available_fraction: Fraction of RAM available in ``[0.0, 1.0]``, or
            ``None`` when it cannot be measured (psutil absent). ``None`` is
            treated conservatively (as if RAM were tight) by the controller.
    """

    cpu_count: int
    load_avg_1m: float
    ram_available_fraction: float | None


@dataclass(frozen=True)
class RegulationDirective:
    """Directive returned by :meth:`ResourceController.regulate`.

    Attributes:
        next_batch_size: Batch size to use for the next batch, always clamped to
            ``[min_batch, max_batch]``.
        pause_seconds: Seconds to pause before the next batch, always in
            ``[0.0, max_pause]``.
    """

    next_batch_size: int
    pause_seconds: float


class ResourceController:
    """Adaptive controller that regulates embedding batch sizing and pacing.

    The controller is *relative*: every decision is taken from the load ratio
    ``load_avg_1m / cpu_count`` and the available-RAM fraction, never from an
    absolute core count or byte threshold. This guarantees the same behaviour
    class across machines of different sizes at the same utilisation.
    """

    #: The directive class this controller constructs. Bound in the class body so
    #: that an instance always returns the ``RegulationDirective`` defined in the
    #: same module generation it was created from. This keeps ``isinstance``
    #: checks valid for callers that imported ``RegulationDirective`` alongside
    #: ``ResourceController`` even after the module is reloaded (importlib.reload
    #: re-executes the module dict, so a call-time global lookup would otherwise
    #: resolve to a freshly-created class object).
    _directive_cls = RegulationDirective

    def __init__(
        self,
        *,
        min_batch: int = 1,
        max_batch: int = 256,
        max_pause: float = 30.0,
    ) -> None:
        # min_batch is floored at 1 so a batch can never be empty.
        self.min_batch = max(1, int(min_batch))
        self.max_batch = max(self.min_batch, int(max_batch))
        self.max_pause = max(0.0, float(max_pause))

    # -- core decision ------------------------------------------------------

    def regulate(
        self,
        prev_batch_size: int,
        snapshot: SystemSnapshot,
    ) -> RegulationDirective:
        """Return a :class:`RegulationDirective` for the given system state.

        Pure function: identical ``(prev_batch_size, snapshot)`` always yields an
        identical directive. The result is always bounded:
        ``min_batch <= next_batch_size <= max_batch`` and
        ``0.0 <= pause_seconds <= max_pause``.

        Decision policy (relative thresholds):
        - **Stressed** (load ratio ``>= 0.85`` or RAM critical ``< 0.10``):
          shrink the batch and pause proportionally to the overload.
        - **Busy** (load ratio ``>= 0.7`` or RAM tight ``< 0.25``, or RAM
          unmeasurable): hold the batch steady, no pause — never grow.
        - **Healthy** (load ratio ``< 0.7`` and RAM ``>= 0.35``): grow the batch
          (bounded), no pause.
        """
        cpu_count = snapshot.cpu_count if snapshot.cpu_count >= 1 else 1
        # Relative load: fraction of total CPU capacity in use. cpu_count is
        # floored at 1 above, so this division never raises ZeroDivisionError.
        load_ratio = max(0.0, snapshot.load_avg_1m) / cpu_count

        ram = snapshot.ram_available_fraction
        # Unknown RAM (psutil absent) is treated as "tight" (conservative): we
        # never grow when we cannot prove RAM is healthy.
        ram_known = ram is not None
        ram_critical = ram_known and ram < _RAM_CRITICAL_FRACTION
        ram_tight = (not ram_known) or ram < _RAM_TIGHT_FRACTION
        ram_healthy = ram_known and ram >= _RAM_HEALTHY_FRACTION

        load_stressed = load_ratio >= _LOAD_STRESS_RATIO
        load_busy = load_ratio >= _LOAD_BUSY_RATIO

        if load_stressed or ram_critical:
            return self._stressed_directive(prev_batch_size, load_ratio, ram)
        if load_busy or ram_tight:
            # Hold steady: never grow under pressure, but no pause needed yet.
            return self._directive_cls(
                next_batch_size=self._clamp_batch(prev_batch_size),
                pause_seconds=0.0,
            )
        if ram_healthy:
            # Healthy on both axes: grow (bounded), no pause.
            grown = int(prev_batch_size * _GROW_FACTOR)
            return self._directive_cls(
                next_batch_size=self._clamp_batch(max(prev_batch_size, grown)),
                pause_seconds=0.0,
            )
        # Low load but RAM only moderately available (between tight and healthy):
        # hold steady without pausing.
        return self._directive_cls(
            next_batch_size=self._clamp_batch(prev_batch_size),
            pause_seconds=0.0,
        )

    def _stressed_directive(
        self,
        prev_batch_size: int,
        load_ratio: float,
        ram: float | None,
    ) -> RegulationDirective:
        """Return a shrink-and-pause directive scaled by the worst overload."""
        shrunk = int(prev_batch_size * _SHRINK_FACTOR)
        next_batch = self._clamp_batch(min(prev_batch_size, shrunk))

        # Pause grows with how far past the stress thresholds we are. Severity in
        # [0, 1]: the larger of the load overshoot and the RAM shortfall.
        load_severity = 0.0
        if load_ratio > _LOAD_STRESS_RATIO:
            load_severity = min(1.0, load_ratio - _LOAD_STRESS_RATIO)
        ram_severity = 0.0
        if ram is not None and ram < _RAM_CRITICAL_FRACTION:
            ram_severity = min(
                1.0, (_RAM_CRITICAL_FRACTION - ram) / _RAM_CRITICAL_FRACTION
            )
        severity = max(load_severity, ram_severity)
        # A stressed system always pauses a little, even at low severity, so the
        # OS gets breathing room; scale up to max_pause with severity.
        pause = self.max_pause * (0.1 + 0.9 * severity)
        return self._directive_cls(
            next_batch_size=next_batch,
            pause_seconds=self._clamp_pause(pause),
        )

    # -- bounded recovery wait ---------------------------------------------

    def wait_until_healthy(
        self,
        *,
        reader: Callable[[], SystemSnapshot],
        sleep: Callable[[float], None],
        max_wait_seconds: float = 120.0,
    ) -> None:
        """Block (via the injected *sleep*) until the system is healthy.

        Reads snapshots from *reader*; while a snapshot is stressed it pauses for
        the directive's ``pause_seconds`` using the injected *sleep* callable —
        never :func:`time.sleep` directly — and tries again. The wait is bounded:
        it returns once a non-stressed snapshot is observed, the accumulated
        injected-sleep budget reaches *max_wait_seconds*, or a fixed iteration
        cap is hit, whichever comes first.

        Args:
            reader: Zero-arg callable returning the current :class:`SystemSnapshot`.
            sleep: One-arg callable used for every pause (injected for tests).
            max_wait_seconds: Upper bound on total accumulated sleep time.
        """
        waited = 0.0
        # Hard iteration cap so a misbehaving reader cannot spin forever even if
        # every directive returns a zero pause.
        max_iterations = 1000
        for _ in range(max_iterations):
            snapshot = reader()
            directive = self.regulate(self.max_batch, snapshot)
            # Not stressed once no pause is requested -> healthy enough to run.
            if directive.pause_seconds <= 0.0:
                return
            if waited >= max_wait_seconds:
                return
            pause = directive.pause_seconds
            # Never overshoot the remaining wait budget.
            remaining = max_wait_seconds - waited
            pause = min(pause, remaining)
            sleep(pause)
            waited += pause

    # -- bounds helpers -----------------------------------------------------

    def _clamp_batch(self, value: int) -> int:
        """Clamp *value* into ``[min_batch, max_batch]``."""
        return max(self.min_batch, min(int(value), self.max_batch))

    def _clamp_pause(self, value: float) -> float:
        """Clamp *value* into ``[0.0, max_pause]``."""
        return max(0.0, min(float(value), self.max_pause))


# ---------------------------------------------------------------------------
# System reader (graceful psutil fallback)
# ---------------------------------------------------------------------------

def _cpu_count() -> int:
    """Return the logical CPU count, never below 1."""
    count = os.cpu_count()
    return count if isinstance(count, int) and count >= 1 else 1


def _load_avg_1m() -> float:
    """Return the 1-minute load average, or 0.0 when unavailable.

    ``os.getloadavg`` is absent on some platforms (e.g. Windows); in that case a
    neutral 0.0 keeps the controller from over-throttling.
    """
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        return 0.0
    try:
        return float(getloadavg()[0])
    except (OSError, ValueError):  # pragma: no cover — platform-dependent
        return 0.0


def get_system_reader() -> Callable[[], SystemSnapshot]:
    """Return a callable that reads the current :class:`SystemSnapshot`.

    psutil is imported lazily here (never at module import time) and the import
    is optional: if it is missing, the returned reader still works and reports
    ``ram_available_fraction=None`` while CPU information comes from
    :func:`os.cpu_count` / :func:`os.getloadavg`. The controller treats a
    ``None`` fraction conservatively, so the run is paced safely either way.
    """
    try:
        import psutil  # noqa: PLC0415 — optional dependency, imported lazily.
    except ImportError:
        psutil = None  # type: ignore[assignment]

    def _read() -> SystemSnapshot:
        ram_fraction: float | None = None
        if psutil is not None:
            try:
                vm = psutil.virtual_memory()
                total = float(getattr(vm, "total", 0) or 0)
                available = float(getattr(vm, "available", 0) or 0)
                if total > 0:
                    ram_fraction = max(0.0, min(1.0, available / total))
            except Exception:  # noqa: BLE001 — any psutil failure degrades to None.
                ram_fraction = None
        return SystemSnapshot(
            cpu_count=_cpu_count(),
            load_avg_1m=_load_avg_1m(),
            ram_available_fraction=ram_fraction,
        )

    return _read
