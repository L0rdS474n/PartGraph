"""Activity state for the idle auto-stop decision (ADR-0023).

This is a **leaf** module: its top-level imports are the Python standard
library only, plus :mod:`psutil` imported **lazily**, inside a function (the
same ARCH-1 discipline :mod:`partgraph.util.resources` already documents — a
module-level ``import psutil`` here would make merely importing
``partgraph.util`` require it). It must never import :mod:`partgraph.cli`,
:mod:`partgraph.util.lifecycle`, :mod:`partgraph.util.container` or any of the
embed/query/load layers: it knows **nothing about containers**, and
``lifecycle`` knows nothing about activity in return. The two leaves are
combined in exactly one place, ``partgraph.cli``'s ``db idle-stop`` command.
Both halves of that mutual ignorance are enforced mechanically in
``tests/unit/test_activity_architecture.py``; like ``lifecycle``, this module
is deliberately **not** re-exported from ``partgraph/util/__init__.py``.

Why disk state at all
---------------------
``partgraph`` is a one-shot CLI: every invocation runs its command and exits.
Nothing survives that exit to notice, half an hour later, that the database has
gone unused — there is no daemon, no event loop, no background thread. The only
thing that can act afterwards is something outside the process: an opt-in
``systemd --user`` timer that periodically runs ``partgraph db idle-stop`` as
its own separate one-shot command. This module supplies the state those two
unrelated invocations cooperate through:

- an **activity stamp** (:func:`touch_activity` / :func:`read_activity_stamp`)
  — "PartGraph last did real database work at T";
- a **lease** (:func:`held_lease` / :func:`read_lease`) — "a PartGraph process
  is doing real database work *right now*";

plus the pure decision :func:`evaluate_idle`, which reads only that state (and
one caller-supplied "is the database reachable" boolean) and answers whether a
stop is safe. It starts nothing, stops nothing, and never signals or kills a
process: the stop itself always goes through
:func:`partgraph.util.lifecycle.stop_all`.

Failure direction
-----------------
Every degradation here fails toward **not stopping**:

- an unreadable, oversized, malformed or otherwise unparseable lease is
  ``UNDETERMINED`` — it blocks the stop and is left on disk, never deleted.
  "I could not tell" is never recorded as "I checked, and it is gone" (the same
  asymmetry ``lifecycle``'s own ``UNKNOWN`` tag exists for);
- only a *confirmed* dead process — a clean ``NoSuchProcess`` (which
  ``ZombieProcess`` subclasses), or a live PID whose ``create_time`` differs
  from the recorded one, i.e. a recycled PID — lets a lease be cleaned;
- a bookkeeping failure never propagates: :func:`touch_activity` and
  :func:`acquire_lease` warn once per state directory and return, because the
  database command they are a side effect of must not crash over its own
  telemetry. It is still never *claimed* as a success: ``touch_activity``
  returns whether the stamp landed, and :func:`evaluate_idle` reports a write
  that did not with its own :data:`REASON_STAMP_UNRECORDABLE` tag instead of a
  bootstrap it never performed.

The one place that fails toward *stopping* is deliberate: a stamp implausibly
far in the future (see :data:`STAMP_FUTURE_POISON_CEILING_MINUTES`) is treated
as untrustworthy rather than as "just active forever", because a single forward
clock jump would otherwise disable idle-stop permanently and silently.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = [
    "DEFAULT_IDLE_TIMEOUT_MINUTES",
    "MAX_LEASE_FILE_BYTES",
    "MAX_STAMP_FILE_BYTES",
    "REASON_DISABLED",
    "REASON_FRESH_STAMP",
    "REASON_LIVE_LEASE",
    "REASON_NOTHING_TO_DO",
    "REASON_STALE",
    "REASON_STAMP_BOOTSTRAPPED",
    "REASON_STAMP_POISON_RECOVERED",
    "REASON_STAMP_UNRECORDABLE",
    "REASON_UNDETERMINED_LEASE",
    "STAMP_FUTURE_POISON_CEILING_MINUTES",
    "IdleDecision",
    "Lease",
    "acquire_lease",
    "activity_stamp_path",
    "default_state_dir",
    "evaluate_idle",
    "held_lease",
    "lease_path",
    "lease_paths",
    "read_activity_stamp",
    "read_lease",
    "release_lease",
    "touch_activity",
]

#: Module logger. Bookkeeping failures are warned here (once per state
#: directory and kind) instead of raising, and every message is path-free.
_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Documented default idle budget, in minutes, when the operator sets nothing.
#: Parsing ``PARTGRAPH_IDLE_TIMEOUT_MINUTES`` is deliberately NOT this leaf's
#: job — it lives in ``partgraph.cli._idle_timeout_minutes()``, mirroring
#: ``_autostart_enabled()``'s own precedent ("this is the CLI's policy, so the
#: CLI owns it"). This leaf only ever receives an already-parsed float.
DEFAULT_IDLE_TIMEOUT_MINUTES: float = 30.0

#: How far into the future a stored stamp may sit before it stops being read as
#: "legitimately more recent than my own clock" and starts being read as
#: UNTRUSTWORTHY.
#:
#: A JUDGEMENT CALL, disclosed as such (mirroring ``STOP_GRACE_SECONDS`` and
#: ``AUTOSTART_READY_TIMEOUT_S``), sized against two opposite errors:
#:
#: - Too small, and an ordinary backward NTP correction (sub-second to a few
#:   seconds in practice) would be mistaken for poisoning, letting a genuinely
#:   fresh stamp be overwritten with an older one — the wrong direction for a
#:   guard that protects a database in use. Ten minutes is enormous headroom.
#: - Too large, and the damage from a *real* forward jump lasts longer. It is
#:   strictly below :data:`DEFAULT_IDLE_TIMEOUT_MINUTES` so the worst case of
#:   any poisoning event — "the stamp reads fresh for up to the ceiling" —
#:   stays well inside a single idle cycle rather than being unbounded.
#:
#: The alternative (the naive "never write earlier than the existing stamp"
#: rule, with no ceiling) was rejected because one bad ``now()`` landing far in
#: the future would then be protected by that same rule forever: idle-stop
#: would become a silent, permanent no-op, and the idle cost this feature
#: exists to remove would return invisibly. Silence is exactly how the original
#: incident persisted.
STAMP_FUTURE_POISON_CEILING_MINUTES: float = 10.0

#: Size bounds applied BEFORE either file is decoded — mirroring
#: :data:`partgraph.util.lifecycle.MAX_PS_OUTPUT_BYTES`' own "bounded, then
#: decoded" discipline, here against a much smaller expected payload (a single
#: fixed-shape marker object, not a whole-host enumeration). This matters more
#: than it looks: the monotonic write rule means :func:`touch_activity` READS
#: the existing stamp before writing, so the bounded read path runs on every
#: database-touching command, not only under the opt-in timer.
MAX_STAMP_FILE_BYTES = 4096
MAX_LEASE_FILE_BYTES = 4096

#: Reason tags naming WHY an :class:`IdleDecision` was reached, so a caller (or
#: a test) can assert the reason rather than only the boolean. Plain strings,
#: mirroring ``lifecycle``'s own ``"S1"``/``"S2"`` selector-tag style. Every
#: value is safe to print verbatim: no path, no separator, no operator data.
REASON_DISABLED = "disabled"
REASON_LIVE_LEASE = "live-lease"
REASON_UNDETERMINED_LEASE = "undetermined-lease"
REASON_FRESH_STAMP = "fresh-stamp"
REASON_STALE = "stale"
REASON_NOTHING_TO_DO = "nothing-to-do"
REASON_STAMP_BOOTSTRAPPED = "stamp-bootstrapped"
REASON_STAMP_POISON_RECOVERED = "stamp-poison-recovered"

#: The database was reachable and a stamp was owed, but the write did not land
#: (an unwritable, root-owned or full state directory). Deliberately NOT
#: :data:`REASON_NOTHING_TO_DO`: that tag means "the database is down, so there
#: is nothing to protect and nothing to stop", while this one means "the
#: database IS up and I could not record it" — the same "could not tell" versus
#: "checked" distinction this module already refuses to collapse for leases.
#: The boundary between the two is structural, not a guard: ``nothing-to-do``
#: returns from the ``not db_reachable`` branch before any write is attempted.
REASON_STAMP_UNRECORDABLE = "stamp-unrecordable"

#: File names inside the state directory. The lease is scoped BY PID: a single
#: shared lease file would let a second concurrent invocation clobber a first,
#: still-live one, and if the second then finished first, ``db idle-stop``
#: would see "no lease at all" while the first is still doing real work.
_STAMP_FILENAME = "activity.json"
_LEASE_PREFIX = "activity_lease."
_LEASE_SUFFIX = ".json"
_TMP_SUFFIX = ".tmp"

#: Keys in the two JSON payloads. Neither file ever stores a path.
_STAMP_KEY = "last_active_utc"
_LEASE_PID_KEY = "pid"
_LEASE_CREATE_TIME_KEY = "create_time"
_LEASE_ACQUIRED_KEY = "acquired_utc"

#: Tolerance when comparing a recorded ``create_time`` against the one psutil
#: reports now. The value round-trips exactly through JSON, so this is not
#: needed for correctness — it is a deliberate bias toward the SAFE direction:
#: a false "dead" would let a stop through while real work is in flight, while
#: a false "live" only postpones a stop. A millisecond is many orders of
#: magnitude below any plausible PID-reuse interval.
_CREATE_TIME_TOLERANCE_S = 1e-3

#: Lease liveness tri-state. "Undetermined" is a first-class answer, never
#: silently folded into either of the other two.
_LEASE_LIVE = "live"
_LEASE_DEAD = "dead"
_LEASE_UNDETERMINED = "undetermined"

#: Repository root: src/partgraph/util/activity.py -> util -> partgraph -> src
#: -> <repo root>. Resolved once, so :func:`default_state_dir` never depends on
#: the current working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

#: Warn-once bookkeeping, keyed by (kind, state directory) so a flood against
#: ONE unwritable target during a paginated run is suppressed while a genuinely
#: different failure elsewhere still gets its own warning. Process-local by
#: design: this is log hygiene, never persisted state.
_WARNED: set[str] = set()


class _NeverRaised(Exception):
    """Sentinel used when an injected psutil stand-in lacks ``NoSuchProcess``.

    Catching this class matches nothing, so a seam missing that attribute
    degrades to "undetermined" (blocking the stop) instead of raising an
    ``AttributeError`` out of :func:`evaluate_idle`.
    """


# ---------------------------------------------------------------------------
# DTOs (frozen, mirroring lifecycle's Instance/UnitState/DownResult)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lease:
    """One recorded "a PartGraph process is working right now" marker.

    Attributes:
        pid: The recording process's PID.
        create_time: That process's start time as psutil reports it. The PID
            alone is not identity — PIDs are recycled — so both halves must
            match before a lease counts as live. This is psutil's own
            documented anti-PID-recycling technique.
        acquired_utc: When the lease was taken, ISO-8601 in UTC. Recorded for
            operator diagnosis only; no decision reads it.
    """

    pid: int
    create_time: float
    acquired_utc: str


@dataclass(frozen=True)
class IdleDecision:
    """The answer :func:`evaluate_idle` returns.

    Attributes:
        should_stop: True iff stopping the database is safe right now.
        reason: One of the ``REASON_*`` tags, naming which rule decided.
    """

    should_stop: bool
    reason: str


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def default_state_dir() -> Path:
    """Return the state directory the existing checkpoints already use.

    The same ``<repo root>/data/state`` directory that holds the normalize and
    load checkpoints (``partgraph.cli``'s ``NORMALIZE_CHECKPOINT_PATH`` /
    ``LOAD_CHECKPOINT_PATH``): one place for PartGraph's own small on-disk
    state, not a second one invented for this feature. Always absolute.
    """
    return _REPO_ROOT / "data" / "state"


def activity_stamp_path(state_dir: Path | str) -> Path:
    """Return the activity stamp's path inside *state_dir*."""
    return Path(state_dir) / _STAMP_FILENAME


def lease_path(state_dir: Path | str, pid: int | None = None) -> Path:
    """Return the lease path for *pid* (this process by default).

    PID-scoped on purpose — see :data:`_LEASE_PREFIX`.
    """
    resolved_pid = os.getpid() if pid is None else int(pid)
    return Path(state_dir) / f"{_LEASE_PREFIX}{resolved_pid}{_LEASE_SUFFIX}"


def lease_paths(state_dir: Path | str) -> tuple[Path, ...]:
    """Return every lease file present in *state_dir*, regardless of liveness.

    Discovery is by GLOB: the filename is authoritative for "a lease file
    exists here", and a file's own (possibly malformed) content is never
    trusted to say where it lives. Returns an empty tuple when the directory
    does not exist or cannot be listed.
    """
    try:
        return tuple(
            sorted(Path(state_dir).glob(f"{_LEASE_PREFIX}*{_LEASE_SUFFIX}"))
        )
    except OSError:
        return ()


# ---------------------------------------------------------------------------
# Bounded, shape-checked reads
# ---------------------------------------------------------------------------


def _read_text_bounded(path: Path, max_bytes: int) -> str | None:
    """Read *path* as UTF-8 text, or return None — never raise, never block.

    The file's TYPE is checked with ``stat`` before it is ever opened: a
    directory where a file was expected, or a symlink pointing at a FIFO or a
    device node, must degrade promptly rather than raise ``IsADirectoryError``
    or block forever in ``open()`` waiting for a writer that will never come.
    Oversized content is discarded WITHOUT being decoded.
    """
    try:
        info = os.stat(path)  # follows symlinks: the TARGET's type is what matters
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _load_json_object(text: str) -> dict | None:
    """Parse *text* as a JSON object, or return None (arrays included)."""
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_utc(raw: str) -> datetime | None:
    """Parse an ISO-8601 instant that MUST carry a timezone, else None.

    A naive value is rejected rather than assumed to be UTC: this module only
    ever writes aware timestamps, so a naive one is content we did not produce,
    and guessing its zone could silently shift a decision by hours.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# Atomic, never-raising writes
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _resolve_now(now: Callable[[], datetime] | None) -> datetime:
    return _utcnow() if now is None else now()


def _warn_once(kind: str, directory: Path, message: str) -> None:
    """Log *message* at WARNING at most once per (kind, directory).

    A paginated run heartbeats once per page for hours; an unwritable state
    directory must not produce one identical warning per page. The scope is
    deliberately per target and per kind, never global: a different failure
    elsewhere still gets its own line. The message never contains a path.
    """
    key = f"{kind}:{directory}"
    if key in _WARNED:
        return
    _WARNED.add(key)
    _LOG.warning(message)


def _discard(path: Path) -> None:
    """Remove *path* if present, ignoring every filesystem error."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _atomic_write(directory: Path, target: Path, payload: str, *, kind: str) -> bool:
    """Write *payload* to *target* atomically. Return True iff it landed.

    Temp file plus ``os.replace``, the same discipline the normalize and load
    checkpoints already use, so a crash mid-write cannot leave a half-written
    marker at the real path. A failure is warned once (per target directory and
    kind) and swallowed: the database command this write is a side effect of
    must never fail because its own bookkeeping did. A temp file that survived
    a failed rename is removed, so a failure leaves no debris either.
    """
    temp = target.with_name(target.name + _TMP_SUFFIX)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, target)
    except OSError:
        _warn_once(
            kind,
            directory,
            "Could not record PartGraph activity state; the idle auto-stop "
            "timer may act on a stale marker. Further identical warnings for "
            "this state directory are suppressed.",
        )
        _discard(temp)
        return False
    return True


# ---------------------------------------------------------------------------
# The activity stamp
# ---------------------------------------------------------------------------


def read_activity_stamp(state_dir: Path | str) -> datetime | None:
    """Return the recorded instant of last activity, or None.

    None means "no usable stamp" for every reason there could be: absent,
    oversized, a directory, a symlink to a FIFO, unparseable JSON, the wrong
    shape, or an unparseable/naive timestamp. Callers treat all of those
    identically, so none of them needs its own branch. Never raises.
    """
    text = _read_text_bounded(activity_stamp_path(state_dir), MAX_STAMP_FILE_BYTES)
    if text is None:
        return None
    payload = _load_json_object(text)
    if payload is None:
        return None
    raw = payload.get(_STAMP_KEY)
    if not isinstance(raw, str):
        return None
    return _parse_utc(raw)


def _stamp_is_protected(existing: datetime, moment: datetime) -> bool:
    """True iff *existing* must not be regressed to *moment*.

    The monotonic rule, bounded: an existing stamp AHEAD of the value being
    written wins — a database confirmed active must never be made to look older
    — but only while the gap stays within
    :data:`STAMP_FUTURE_POISON_CEILING_MINUTES`. Beyond that the existing value
    is not "a slightly faster clock", it is untrustworthy, and protecting it
    would disable this feature permanently.
    """
    if existing <= moment:
        return False
    return (existing - moment) <= timedelta(minutes=STAMP_FUTURE_POISON_CEILING_MINUTES)


def touch_activity(
    *, state_dir: Path | str, now: Callable[[], datetime] | None = None
) -> bool:
    """Record that PartGraph just did real database work.

    Monotonic-safe within the poison ceiling (a backward clock step cannot make
    an active database look idle) and self-healing beyond it (a stamp poisoned
    implausibly far into the future is overwritten by the very next legitimate
    write, not protected until wall-clock time catches up with it — which for
    an extreme poisoning could be centuries).

    Never raises: a failure is warned once per state directory and returned
    from. *now* is injectable so callers and tests can pin the instant.

    Returns:
        True iff the durable record is trustworthy once this call returns —
        either :func:`_atomic_write` reported that the stamp landed, or the
        monotonic guard correctly skipped the write because the stamp already
        on disk is at least as recent. That second case is a SUCCESS, not a
        failure: "landed" here means "the record is right", never "bytes were
        written just now". False means a write was owed, was attempted, and did
        not land — the only state in which a caller may not claim it recorded
        anything. Returning the boolean does not make it a raised error: every
        caller in :mod:`partgraph.cli` is free to discard it, exactly as it
        discarded the previous ``None``, because a bookkeeping failure must
        never propagate into the database command it is a side effect of.
    """
    directory = Path(state_dir)
    moment = _resolve_now(now)
    existing = read_activity_stamp(directory)
    if existing is not None and _stamp_is_protected(existing, moment):
        return True
    payload = json.dumps({_STAMP_KEY: moment.astimezone(UTC).isoformat()})
    return _atomic_write(directory, activity_stamp_path(directory), payload, kind="stamp")


# ---------------------------------------------------------------------------
# The lease
# ---------------------------------------------------------------------------


def _import_psutil():
    """Import psutil lazily, or return None.

    INVARIANT (do not "optimize" by hoisting): this import MUST stay inside a
    function body. ``partgraph/util/__init__.py`` imports this module's
    siblings eagerly and ``partgraph.cli`` sits on that same path, so a
    module-level import here would make importing the CLI require psutil
    (mirrors ``resources.py``'s own ARCH-1 note).
    """
    try:
        import psutil  # noqa: PLC0415 — lazy on purpose; see the docstring.
    except ImportError:
        return None
    return psutil


def _create_time_of(pid: int, psutil_module) -> float | None:
    """Return *pid*'s start time, or None when it cannot be established."""
    module = _import_psutil() if psutil_module is None else psutil_module
    if module is None:
        return None
    try:
        return float(module.Process(pid).create_time())
    except Exception:  # noqa: BLE001 — any psutil failure degrades to None.
        return None


def acquire_lease(
    *,
    state_dir: Path | str,
    pid: int | None = None,
    now: Callable[[], datetime] | None = None,
    psutil_module=None,
) -> None:
    """Record that *pid* (this process by default) is working right now.

    Identity is ``(pid, create_time)``, never the PID alone. When the start
    time cannot be established at all (psutil absent, or the lookup refused),
    ``create_time`` is written as null: such a lease reads back as
    UNDETERMINED, which BLOCKS an idle stop. That is the deliberate direction —
    a lease that cannot prove its identity must not be mistaken for a dead one
    while its process is still doing real work.

    Never raises: a write failure is warned once and swallowed, exactly as in
    :func:`touch_activity`.
    """
    directory = Path(state_dir)
    resolved_pid = os.getpid() if pid is None else int(pid)
    payload = json.dumps(
        {
            _LEASE_PID_KEY: resolved_pid,
            _LEASE_CREATE_TIME_KEY: _create_time_of(resolved_pid, psutil_module),
            _LEASE_ACQUIRED_KEY: _resolve_now(now).astimezone(UTC).isoformat(),
        }
    )
    _atomic_write(
        directory, lease_path(directory, pid=resolved_pid), payload, kind="lease"
    )


def release_lease(*, state_dir: Path | str, pid: int | None = None) -> None:
    """Drop *pid*'s lease. Idempotent, and never raises."""
    _discard(lease_path(state_dir, pid=pid))


@contextmanager
def held_lease(
    *,
    state_dir: Path | str,
    pid: int | None = None,
    now: Callable[[], datetime] | None = None,
    psutil_module=None,
) -> Iterator[None]:
    """Hold a lease for the duration of the wrapped block.

    Released in a ``finally``, so a command whose own work raises still gives
    the lease back — and the original exception propagates unmodified. A lease
    that outlived its process (a SIGKILL, a power cut) is cleaned by the next
    :func:`evaluate_idle` that confirms the PID is gone.
    """
    resolved_pid = os.getpid() if pid is None else int(pid)
    acquire_lease(
        state_dir=state_dir, pid=resolved_pid, now=now, psutil_module=psutil_module
    )
    try:
        yield
    finally:
        release_lease(state_dir=state_dir, pid=resolved_pid)


def _read_lease_file(path: Path) -> Lease | None:
    """Parse one lease file into a :class:`Lease`, or None when malformed.

    "Malformed" deliberately includes every shape this module never writes: a
    non-object, a missing or non-integer ``pid``, a non-positive ``pid``, a
    missing/non-numeric/non-finite ``create_time`` (which is exactly what an
    identity-less lease records), oversized content, or a non-regular file.
    Callers must treat None as UNDETERMINED, never as "no lease".
    """
    text = _read_text_bounded(path, MAX_LEASE_FILE_BYTES)
    if text is None:
        return None
    payload = _load_json_object(text)
    if payload is None:
        return None
    pid = payload.get(_LEASE_PID_KEY)
    create_time = payload.get(_LEASE_CREATE_TIME_KEY)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if isinstance(create_time, bool) or not isinstance(create_time, (int, float)):
        return None
    if not math.isfinite(float(create_time)):
        return None
    acquired = payload.get(_LEASE_ACQUIRED_KEY)
    return Lease(
        pid=pid,
        create_time=float(create_time),
        acquired_utc=acquired if isinstance(acquired, str) else "",
    )


def read_lease(state_dir: Path | str, pid: int | None = None) -> Lease | None:
    """Return *pid*'s recorded lease, or None when absent or malformed."""
    return _read_lease_file(lease_path(state_dir, pid=pid))


def _lease_status(lease: Lease, psutil_module) -> str:
    """Classify *lease* as live, dead, or undetermined.

    Only two outcomes count as DEAD, and both are positive confirmations:
    a clean ``NoSuchProcess`` (``ZombieProcess`` subclasses it, so a zombie
    lands here too — verified against the installed psutil), or a live PID
    whose ``create_time`` does not match the recorded one, which is the
    recycled-PID case a naive ``pid_exists()`` check would call live forever.

    Everything else — ``AccessDenied``, an absent psutil, any unexpected error
    — is UNDETERMINED: we know something holds that PID but cannot prove it is
    the same process, so the stop must not go through on that basis.
    """
    if psutil_module is None:
        return _LEASE_UNDETERMINED
    no_such_process = getattr(psutil_module, "NoSuchProcess", _NeverRaised)
    try:
        create_time = float(psutil_module.Process(lease.pid).create_time())
    except no_such_process:
        return _LEASE_DEAD
    except Exception:  # noqa: BLE001 — AccessDenied and friends: cannot tell.
        return _LEASE_UNDETERMINED
    if math.isclose(
        create_time, lease.create_time, rel_tol=0.0, abs_tol=_CREATE_TIME_TOLERANCE_S
    ):
        return _LEASE_LIVE
    return _LEASE_DEAD


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def _scan_leases(directory: Path, psutil_module) -> tuple[bool, bool]:
    """Classify every lease file present. Return ``(live, undetermined)``.

    Every file is visited before anything is decided, so a dead lease is still
    cleaned on a run whose outcome was already fixed by a live one. Only
    confirmed-dead files are removed; an undetermined one is left exactly where
    it is, because it was never shown to be stale.
    """
    live = False
    undetermined = False
    for path in lease_paths(directory):
        lease = _read_lease_file(path)
        if lease is None:
            undetermined = True
            continue
        status = _lease_status(lease, psutil_module)
        if status == _LEASE_LIVE:
            live = True
        elif status == _LEASE_DEAD:
            _discard(path)
        else:
            undetermined = True
    return live, undetermined


def _stamp_decision(
    directory: Path,
    moment: datetime,
    idle_timeout_minutes: float,
    *,
    db_reachable: bool,
) -> IdleDecision:
    """Decide from the activity stamp alone (no lease blocks the stop).

    Split out of :func:`evaluate_idle` so each function states one rule set:
    this one owns "what does the stamp say", including the no-stamp bootstrap
    and the poisoned-stamp self-heal, which reach the SAME branch on purpose —
    an untrustworthy stamp is worth exactly as much as no stamp at all, and
    routing them together means the bootstrap path is the only one that has to
    be right. The two are still reported DISTINCTLY so a self-heal is never
    silently indistinguishable from a first install.

    All three reported outcomes name what is actually on disk afterwards: a
    write that did not land is :data:`REASON_STAMP_UNRECORDABLE`, never a
    bootstrap or self-heal it did not perform.
    """
    stamp = read_activity_stamp(directory)
    poisoned = stamp is not None and (stamp - moment) > timedelta(
        minutes=STAMP_FUTURE_POISON_CEILING_MINUTES
    )
    if stamp is None or poisoned:
        if not db_reachable:
            return IdleDecision(should_stop=False, reason=REASON_NOTHING_TO_DO)
        # INVARIANT that makes touch_activity's two-state bool sufficient here:
        # this branch runs ONLY when the stamp is absent or poisoned, so a write
        # is always genuinely owed and touch_activity's "skipped, the record was
        # already correct" success is structurally unreachable from this caller.
        # True therefore means "the stamp on disk now says `moment`", nothing
        # weaker. Widening the gating above to admit an already-correct stamp
        # would reintroduce exactly the ambiguity this branch exists to remove.
        if not touch_activity(state_dir=directory, now=lambda: moment):
            return IdleDecision(should_stop=False, reason=REASON_STAMP_UNRECORDABLE)
        return IdleDecision(
            should_stop=False,
            reason=REASON_STAMP_POISON_RECOVERED if poisoned else REASON_STAMP_BOOTSTRAPPED,
        )

    # Clamped at zero: a stamp modestly ahead of this clock (ordinary skew,
    # within the ceiling) is "just active", never a negative age that a
    # comparison could read as ancient.
    age_minutes = max((moment - stamp).total_seconds(), 0.0) / 60.0
    if age_minutes >= idle_timeout_minutes:
        return IdleDecision(should_stop=True, reason=REASON_STALE)
    return IdleDecision(should_stop=False, reason=REASON_FRESH_STAMP)


def evaluate_idle(
    *,
    state_dir: Path | str,
    idle_timeout_minutes: float,
    db_reachable: bool,
    now: Callable[[], datetime] | None = None,
    psutil_module=None,
) -> IdleDecision:
    """Decide whether stopping the database is safe right now.

    The rules, in the order they are applied:

    1. ``idle_timeout_minutes <= 0`` (or not a positive number at all) is the
       escape hatch: a total no-op that reads nothing and never touches psutil.
    2. A LIVE lease blocks the stop unconditionally, even against a stamp old
       enough to demand one on its own — work in progress outranks the clock.
    3. An UNDETERMINED lease also blocks it, and is left on disk.
    4. No usable stamp — including one poisoned beyond the future ceiling — is
       resolved by *db_reachable*: if the database is up, a correct stamp is
       written now and this observation becomes the instant the first full
       budget window is measured from; if it is down there is nothing to
       protect and nothing to stop, and no stamp is fabricated for a database
       that was never seen running. When the database is up but that write does
       not land, the answer is :data:`REASON_STAMP_UNRECORDABLE` — the decision
       reports the write's real outcome rather than asserting one it never
       observed, and still does not stop anything.
    5. Otherwise the stamp's age decides: an age at or beyond the budget is
       stale (stop), anything younger is fresh. A stamp modestly ahead of the
       clock is clamped to age zero rather than read as negative.

    Pure with respect to the container engine: it neither knows nor asks how
    the database is run. The only write it can make is the bootstrap/self-heal
    stamp in rule 4.
    """
    if not idle_timeout_minutes > 0:
        return IdleDecision(should_stop=False, reason=REASON_DISABLED)

    directory = Path(state_dir)
    moment = _resolve_now(now)
    module = _import_psutil() if psutil_module is None else psutil_module

    live, undetermined = _scan_leases(directory, module)
    if live:
        return IdleDecision(should_stop=False, reason=REASON_LIVE_LEASE)
    if undetermined:
        return IdleDecision(should_stop=False, reason=REASON_UNDETERMINED_LEASE)

    return _stamp_decision(
        directory, moment, idle_timeout_minutes, db_reachable=db_reachable
    )
