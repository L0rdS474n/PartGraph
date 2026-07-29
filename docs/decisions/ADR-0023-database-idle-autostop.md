# ADR-0023: The database stops itself when nobody is using it

- Status: Accepted
- Date: 2026-07-28

## Context

ADR-0022 removed two of the three ways the database ended up running when
nobody had asked for it: the quadlet unit's login-time autostart (documented and
detected in PR-B1, defused by the operator in PR-B2) and the assumption that a
database must be started by hand before any command works (PR-B2's lazy
autostart). What it did **not** address, and said so explicitly — "Idle
auto-stop belongs to neither PR" — is what happens *after*.

That gap is the operator's original complaint, unresolved:

> after the first `partgraph search` it stays up indefinitely and the 10.2 GB
> idle cost returns.

Lazy autostart makes the database appear on first use. Nothing makes it go away
again. A single interactive search at 09:00 leaves a Dgraph Alpha resident for
the rest of the day, and the fact that it now starts *on demand* rather than at
login only changes **when** the idle cost begins, never that it ends.

This ADR is PR-C: the database stops itself once it has gone unused for a
configured interval, without ever stopping one that is in use.

## Decision

### 1. A host-side timer, because a one-shot CLI cannot observe its own idleness

`partgraph` is a one-shot CLI. Every invocation parses its arguments, runs its
command and exits. There is no daemon, no event loop, no supervisor, and no
background thread that survives the exit — so **nothing inside the process can
ever notice, thirty minutes later, that nobody used the database again**. An
in-process idle timer is not a design this repository rejected on taste; it is
one that cannot exist here at all, because the process whose timer it would be
is already gone.

The only thing that can act after `partgraph` has exited is something outside
it. So idle auto-stop is a **separate one-shot command**, `partgraph db
idle-stop`, invoked periodically by an opt-in `systemd --user` timer — exactly
the pattern ADR-0014 already established for `partgraph-refresh-all.timer`: the
repository ships `systemd/partgraph-db-idle-stop.{service,timer}` and **never**
enables them; installing them is a documented, operator-run procedure
(`docs/scheduling.md` § 7).

Two invocations that never overlap in time therefore have to cooperate through
**disk**, which is what `partgraph.util.activity` provides: an activity *stamp*
("PartGraph last did real database work at T") and per-process *leases* ("a
PartGraph process is doing real work right now"), plus the pure decision
`evaluate_idle()` that reads only those and answers "is a stop safe".

#### Alternatives evaluated and rejected for the trigger

- **An in-process timer / background thread.** Not viable, per the above: the
  process exits. A variant — "keep the last command alive for the idle window"
  — turns every `partgraph search` into a foreground process that refuses to
  return for half an hour. Rejected without reservation.
- **systemd socket activation** (`.socket` + `Accept=`), stopping the container
  when its socket goes quiet. Rejected on two independent grounds. First, it
  answers a different question: socket activation governs *starting* a service
  on the first connection, and the idle-stop half of it
  (`systemd-socket-proxyd` with an inactivity timeout) requires the traffic to
  flow **through** systemd — so PartGraph's ports would have to be re-fronted by
  a proxy, changing how the database is reached by every client, including
  clients this repository does not own. Second, it measures the wrong thing: a
  quiet socket is not an idle database (a long-running embed holds no connection
  between batches), and a busy socket is not necessarily PartGraph's work. The
  activity stamp measures what actually matters — whether a *PartGraph command*
  did real work — and needs no change to how anything connects.
- **`podman auto-update`.** Rejected because it does not do this, and must not
  be described as though it did: `podman auto-update` **updates images** — it
  pulls a newer image for containers labelled
  `io.containers.autoupdate=registry` and restarts them. It has no idleness
  concept, no inactivity timer, and no ability to leave a container stopped. It
  would *start* work, not end it. It appears here only because it is the
  `podman`-shaped answer a reader might expect to find considered.
- **A cron entry instead of a systemd timer.** Not rejected — `db idle-stop` is
  an ordinary command and cron can run it. The systemd units are shipped because
  ADR-0014 already ships units for the refresh path and the journal makes an
  unattended action auditable; nothing in the design depends on systemd.

### 2. A live lease blocks the stop, unconditionally

Every one of the nine database-touching commands (`stats`, `search`, `show`,
`embed`, `refresh-links`, `refresh`, `db apply-schema`, `db check-index`, and
`ingest jlcparts`' load stage — PR-B2's autostart allowlist, reused verbatim)
now runs its real work inside `held_lease(...)`. The lease is released in a
`finally`, so a command that raises still gives it back.

While any lease is live, `db idle-stop` does not stop the database — **even if
the activity stamp is stale**. Stamp freshness is a heuristic about the recent
past; a live lease is a fact about the present, and it wins. This is what makes
a multi-hour `partgraph embed` or a scheduled `partgraph refresh` safe from a
timer that fires in the middle of it, with no command needing to know the timer
exists.

**Identity is `(pid, create_time)`, never the PID alone.** PIDs are recycled; a
lease naming PID 555 must not keep a database alive because *some* process now
holds 555. Recording the process's start time alongside its PID, and requiring
both to match, is psutil's own documented technique for exactly this, and it is
what turns "a file exists" into "that process is still the one that wrote it".

**The tri-state.** A lease is LIVE, DEAD, or UNDETERMINED, and the third is a
first-class answer rather than a rounding error:

| Observation | Verdict | Effect |
| --- | --- | --- |
| `create_time` matches | LIVE | stop blocked; file kept |
| `NoSuchProcess` (`ZombieProcess` subclasses it) | DEAD | file cleaned; fall through to the stamp |
| PID exists, `create_time` differs | DEAD (recycled) | file cleaned; fall through to the stamp |
| `AccessDenied`, or any unexpected psutil error | UNDETERMINED | stop blocked; **file kept** |
| Unparseable content, wrong shape, oversized, not a regular file | UNDETERMINED | stop blocked; **file kept** |

This mirrors `partgraph.util.lifecycle`'s own `UNKNOWN` ownership tag and its
governing rule: *"I could not tell" must never be recorded as "I checked, and it
is not ours."* Only a positive confirmation of death lets a lease be deleted, and
only a positive confirmation of death lets a stop proceed past it.

**Nothing is ever signalled or killed.** This module never sends a signal, never
calls `kill`, and never touches a process it inspects; psutil is used for
*reading* liveness only. The stop itself always goes through
`partgraph.util.lifecycle.stop_all()` — the same sweep `db down` uses — so the
S1/S2/S3 selector policy, the `stop`-only verb surface, and the promise never to
touch the unrelated cve-graph stack are **inherited**, not re-derived. `db
idle-stop` never issues `rm`, `volume rm`, `prune`, `-v` or `--volumes`, and it
never starts anything, regardless of `PARTGRAPH_AUTOSTART`.

### 3. The poisoned-stamp ceiling, and why the naive monotonic guard was unsafe

The stamp is written monotonically: a write whose `now()` is **earlier** than
the stamp already on disk is refused. A backward clock step (an NTP correction
mid-run, two heartbeats completing out of order) must not make a database that
was just confirmed active look *older*, because "older" is the direction that
gets it stopped.

The naive form of that rule — *never write earlier than the existing stamp*,
with no upper bound — was specified, reviewed, and **found unsafe**. One bad
`now()` landing far in the future (a real RTC/NTP fault, a caller bug) is
written once, and from then on the same rule refuses every subsequent *correct*
`now()`, forever. `evaluate_idle` reads a stamp that is permanently "just
active", `db idle-stop` becomes a silent, permanent no-op, and the idle cost this
ADR exists to eliminate returns **invisibly**. Silence is precisely how the
original incident persisted for fourteen hours; a design whose failure mode is
"silently stops working" is not acceptable here.

The fix, applied identically at both places a stamp is judged against a clock: a
stamp more than `STAMP_FUTURE_POISON_CEILING_MINUTES` (**10**) ahead of a
genuine `now()` is not "a slightly fast clock", it is **untrustworthy**.

- `touch_activity` stops protecting it: the next legitimate write overwrites it
  immediately. Self-healing happens on the very next database command — not when
  wall-clock time eventually catches up with the poisoned value, which for an
  extreme poisoning is centuries away.
- `evaluate_idle` treats it as if **no stamp existed**, routing through the same
  bootstrap logic (§ 4) that a fresh install already goes through, and reports
  it as `stamp-poison-recovered` rather than as an ordinary bootstrap — so the
  anomaly stays observable instead of being indistinguishable from a first run.

Ten minutes is a judgement call, disclosed as one (the same standing as
`STOP_GRACE_SECONDS` and `AUTOSTART_READY_TIMEOUT_S`). It is far larger than any
plausible ordinary skew — sub-second to a few seconds — so a genuine small
backward correction is never mistaken for poisoning.

**Bounding the damage, stated precisely rather than generally.** A poisoning
inside the ceiling suppresses idle-stop until real time passes the poisoned
value *and then* a full budget elapses — worst case `ceiling + budget`, always
finite, never the unbounded "forever" the naive rule produced. Whether that is
*small* depends on the budget, and the ADR should not claim more than it can:

Worst case is a poisoning of the full 10 minutes, so suppression is
`10 + budget`:

| Budget (`PARTGRAPH_IDLE_TIMEOUT_MINUTES`) | Worst-case suppression | As a multiple of the budget |
| --- | --- | --- |
| 30 (default) | 40 min | 1.3x |
| 60 | 70 min | 1.2x |
| 5 | 15 min | 3x |

At the **default**, the ceiling is a third of the budget and the overshoot is a
fraction of one idle cycle — that is the case the constant was sized for, and
the repository's own test pins `ceiling < DEFAULT_IDLE_TIMEOUT_MINUTES` to keep
it so. An operator who configures a budget *below* the ceiling inverts that
relationship: at a 5-minute budget, a stamp poisoned 9 minutes ahead is still
inside the ceiling, so it is protected rather than healed, and idle-stop stays
suppressed for `9 + 5 = 14` minutes — nearly three full cycles, not a fraction
of one. Still bounded, still self-healing, and still never permanent; but "a
fraction of one idle cycle" is a claim about the **default budget only**, which
is why it is scoped that way here. Nothing enforces the relationship for a
configured value, and a budget at or below the ceiling is a supported
configuration.

### 4. No stamp at all: bootstrap, do not stop

A first install, a cleared `data/state`, or a database an operator started by
hand leaves `db idle-stop` with no stamp to reason from. It does **not** stop
the database, and it does not treat "no evidence of activity" as "evidence of no
activity". Instead:

- **Database reachable:** write a stamp anchored at this observation and report
  `stamp-bootstrapped`. That observation becomes the instant the first full
  budget window is measured from, so a freshly-observed database gets a complete
  idle window before anything happens to it. It also means idle-stop self-heals
  on **its own** schedule, without waiting for some other command to run.
- **Database not reachable:** report `nothing-to-do` and write nothing. There is
  nothing to protect and nothing to stop, and fabricating an activity record for
  a database that was never seen running would be a lie on disk.

The alternative — "no stamp means nothing has happened, so stop it" — would make
the *first* run of a freshly installed timer stop a database somebody had just
started by hand. Rejected.

**Recorded, not fixed: the bootstrap tag asserts a write it does not observe.**
`_stamp_decision` calls `touch_activity()` and then reports
`stamp-bootstrapped` (or `stamp-poison-recovered`) unconditionally, but
`touch_activity` is contracted to *never raise* — an unwritable `data/state`
warns once and returns — so the decision cannot see whether the stamp actually
landed. On a read-only state directory every run therefore reports a bootstrap
that never happened, and the next run repeats it. The *direction* is safe (it
never stops anything, and the warn-once line does fire on the first attempt),
but a tag asserting an unverified write is the same "could not tell" collapsing
into "checked" that this module forbids for leases — so it is named here rather
than left to be found.

It is recorded instead of fixed because the honest fix is not free: reporting it
truthfully needs either a new public reason tag (contract surface no test pins,
which this PR should not invent unilaterally) or reusing `nothing-to-do`, which
would conflate "the database is not reachable" with "the database is up but I
could not record it" — two states this ADR deliberately keeps apart. A follow-up
should add the distinct tag, wire it to `touch_activity`'s existing internal
write result, and pin it with its own test.

**Resolved 2026-07-29 — the follow-up landed.** The distinct tag is
`stamp-unrecordable` (`REASON_STAMP_UNRECORDABLE`), returned by
`_stamp_decision` from **both** callers that share the write — the bootstrap
path and the poison-recovery path — whenever the stamp did not land. It is
wired to exactly the internal result this section named: `_atomic_write` already
computed "did it land", `touch_activity` no longer discards it and now returns
`bool` instead of `None`. `nothing-to-do` was *not* reused, keeping the two
states this section insists on keeping apart; the boundary stays structural
rather than a guard, because `nothing-to-do` returns from the `not db_reachable`
branch before any write is attempted. `True` from `touch_activity` covers the
monotonic guard's intentional "no write was needed, the record is already
correct" no-op as a success, and `_stamp_decision` carries a comment recording
why that case cannot reach it. Pinned by eight tests: four on
`touch_activity`'s return value (write landed, `os.replace` failed,
`mkdir` failed, write correctly skipped), three in `tests/unit/test_activity.py`
on the new tag (bootstrap failure leaves no stamp on disk, poison-recovery
failure leaves the poisoned stamp untouched, the constant is distinct from all
seven existing tags), and one in `tests/unit/test_cli_idle_stop.py` proving
`db idle-stop` prints the leaf's own constant and stays path-free on this
branch. What deliberately did **not** change: the 15 `touch_activity` call
sites in `partgraph.cli` still discard the result, exactly as they discarded
`None` — a bookkeeping failure must still never propagate into the database
command it is a side effect of. The failure direction is unchanged: an
unrecordable stamp still stops nothing.

### 5. `PARTGRAPH_IDLE_TIMEOUT_MINUTES`

Parsing lives in `partgraph.cli._idle_timeout_minutes()`, not in the leaf,
following `_autostart_enabled()`'s precedent exactly: this is CLI policy, so the
CLI owns it, and `evaluate_idle()` stays a pure function of its arguments. Read
from the environment at call time, never captured at import.

| Value (after `.strip()`) | Result | Why |
| --- | --- | --- |
| unset | `30.0` | the documented default |
| `""` / whitespace | `30.0` | an empty value is not a decision |
| `"banana"`, `"5m"`, `"30.5.5"` | `30.0` | a typo must not silently reconfigure anything |
| `"inf"`, `"-inf"`, `"nan"` | `30.0` | `nan` in `age >= timeout` is always False — a silent disable |
| `"0"`, `"0.0"` | `0.0` (disabled) | the documented escape hatch |
| `"-1"`, `"-1000000"` | `0.0` (disabled) | unguarded, `age >= -1` is always True — always stop |
| `"45.5"`, `"999999999999"` | verbatim | no ceiling; see below |

Every unusable value falls back to the **documented default**, never toward
either extreme: not toward "stop immediately" (which is what a naive
`float(x) or 0`, or letting `nan` through, would produce in two different wrong
directions), and not toward "silently disabled" (which would surprise an
operator who was plainly trying to *configure* the feature, not switch it off).
Only a value that parses **and** is non-positive disables it, which collapses the
`0` escape hatch and every negative into one rule.

**No ceiling, deliberately** — unlike `AUTOSTART_READY_TIMEOUT_S`, which has
one. The risk directions are not symmetric: an oversized autostart budget costs
a human a real foreground wait, while an oversized idle budget only means
idle-stop rarely fires, which is the safe direction already (nothing destructive
happens either way).

**But the systemd cadence does get a floor.** The unit fires every 10 minutes,
and the repository's own unit test bounds that cadence to
`[60 s, 3600 s]`. The asymmetry is deliberate: the cadence controls how often a
real **process is spawned**, where "too often" is an unbounded, genuine waste of
the host resources this feature exists to conserve; the timeout only controls a
**threshold** applied once that process runs, where "too small" costs at worst
one extra autostart round-trip on next use, and where the live-lease rule blocks
a premature stop regardless. The two knobs compose: a database goes down at
worst one cadence after it goes idle — about 40 minutes on the defaults.

### 6. The refresh timer, from both sides

`docs/scheduling.md` § 7 is the operator-facing version of this; the decision is
recorded here. The two timers are **independently opt-in** and neither knows the
other's schedule.

- **A refresh that is running is safe.** `refresh` and `refresh-links` hold
  leases like every other database-touching command, so a concurrent
  `db idle-stop` is blocked by § 2's generic rule. No special case exists, and
  none is needed — idle-stop cannot tell a scheduled run from an interactive
  one, and must not have to.
- **Between runs, idle auto-stop may legitimately stop the database, and the
  next scheduled refresh will then fail.** `scripts/partgraph-refresh-all.sh`
  forces `PARTGRAPH_AUTOSTART=0` (ADR-0022's amendment to ADR-0014 D1), so a
  scheduled run never starts a container implicitly. It exits non-zero with the
  existing path-free "is the database running?" hint, and the wrapper propagates
  that to the scheduler.

That failure is **disclosed, not designed away**. Suppressing it would require
`db idle-stop` to read `partgraph-refresh-all.timer`'s calendar — coupling two
units an operator deliberately installed separately, and making a stop decision
depend on a schedule the stopping command has no business knowing. An operator
who wants both is given three plain options in `docs/scheduling.md` § 7 (keep the
database up over the window; raise the budget above the refresh interval; or
accept the failed run, which is visible in the journal rather than silent).

ADR-0022's "Amendment to ADR-0014 D1" section carries a forward reference to
this section, so a reader arriving at ADR-0014 or ADR-0022 alone is not
surprised by the existence of a third timer.

## Consequences

- The nine database-touching commands each acquire and release a lease, and
  stamp activity on completion. The four paginated ones (`embed`,
  `refresh-links`, `refresh`, and the `ingest jlcparts` load stage) additionally
  heartbeat **per page**, at the checkpoint each already had — the progress-bar
  update for three of them, and the `Loader(progress=...)` callback the load
  stage already threaded through — so a multi-hour run never looks idle. Reusing
  the existing per-page checkpoint rather than adding a parallel one is what
  keeps the heartbeat from drifting out of step with the work it reports.
- Two small files appear in `data/state/`, beside the normalize and load
  checkpoints: `activity.json` and, only while a command runs,
  `activity_lease.<pid>.json`. Both are written atomically (temp file plus
  `os.replace`), contain no filesystem path, and are bounded on read.
- A bookkeeping failure never breaks a database command: an unwritable state
  directory warns **once** per directory and per kind, then is ignored. A
  paginated run against an unwritable directory produces one warning, not one
  per page.
- `psutil` becomes load-bearing (see the limitations below). It is imported
  **lazily**, inside a function, so importing `partgraph.util` or the CLI does
  not require it — the same ARCH-1 invariant `partgraph.util.resources` already
  documents.

## Accepted limitations

Stated here rather than left to be discovered.

1. **An undetermined lease blocks idle auto-stop indefinitely, with no
   operator-visible signal short of inspecting the state directory by hand.** If
   a lease file is left behind by a process killed with SIGKILL *and* its
   liveness cannot be positively resolved — a corrupted file, or a psutil
   `AccessDenied` — the safe direction (block the stop, keep the file) is also a
   permanent one: nothing expires it, nothing reports it, and the only symptom is
   that the database stops being stopped. The reason tag
   (`undetermined-lease`) is returned by `evaluate_idle()` and printed by the
   command, so it *is* in the journal for anyone who looks; there is no
   `db doctor` line for it, no age-out, and no operator warning. A future PR
   should surface undetermined leases in `db doctor` and consider expiring a
   lease whose recorded PID has been unresolvable across many consecutive
   checks. It is not fixed here because every candidate fix trades a permanent
   block for a heuristic that can stop a database that is genuinely in use, and
   that trade needs its own decision rather than being smuggled in with this one.

   A narrower hardening of the same area is available and was not taken: the
   recorded `pid` is bounded *below* (a non-positive value is rejected as
   malformed) but not *above*. An absurd value on its own is harmless — checked
   against the real psutil, `Process(999999999)` on a host with
   `pid_max = 4194304` raises `NoSuchProcess`, so the lease is classified DEAD
   and cleaned, not blocked. Only garbage that *additionally* defeats resolution
   lands in this limitation. Bounding `pid` against
   `/proc/sys/kernel/pid_max` would reclassify some such garbage as dead
   outright rather than leaving it a permanent block; it is a follow-up, not a
   correctness gap, and it is Linux-specific in a way the rest of this leaf is
   not.

2. **`psutil` is unpinned and this repository has no lockfile, while PR-C is the
   first change to make it load-bearing for a stop-or-not decision.** It has been
   a declared dependency since the embed pipeline, but only ever *advisorily*:
   `partgraph.util.resources` degrades to CPU-only pacing when it is absent or
   misbehaves, and nothing is lost. Here, psutil's `Process(pid).create_time()`
   is what distinguishes "the process holding this lease is still alive" from "a
   recycled PID", i.e. what decides whether a database gets stopped. The failure
   direction is safe by construction — an absent or erroring psutil yields
   UNDETERMINED, which blocks the stop (and therefore also limitation 1) — but
   the dependency's *version* is not pinned and its resolution is not
   reproducible, so a future release changing the semantics of
   `create_time()`/`NoSuchProcess` would change this decision's behaviour with no
   diff in this repository. Pinning dependencies and committing a lockfile is
   out of scope for a change already spanning a leaf, nine call sites, two unit
   files and two documents; it is named here as a real, accepted risk rather than
   left implicit.

3. **The shipped unit reports `success` even when the command was never
   found — including on every firing, forever, if the timer is enabled before
   the CLI is reachable.** `ExecStart=` carries a leading `-`, so systemd
   ignores the command's exit status. Confirmed directly, with disposable
   transient units:

   | `ExecStart=` | `Result` | `ExecMainStatus` | `ActiveState` |
   | --- | --- | --- | --- |
   | `/nonexistent/…` | `exit-code` | `203` | **`failed`** |
   | `-/nonexistent/…` | `success` | `0` | `inactive` |

   In both cases the journal carries `Unable to locate executable '…': No such
   file or directory`, so § "Verification" is literally accurate that every
   reason a run did nothing is recorded. But what the `-` removes is the
   **`failed` unit state** — the one signal `systemctl --user status`,
   `list-timers` and most monitoring ever look at.

   The concrete failure: an operator follows `docs/scheduling.md` § 7 and runs
   `systemctl --user enable --now` *before* symlinking the CLI into
   `~/.local/bin`. Nothing enforces that order. The timer then fires every ten
   minutes, reports success every time, and the 10.2 GB idle cost this ADR
   exists to remove never goes away. **By this ADR's own standard — silence is
   how the original problem ran unnoticed for fourteen hours — that is a third
   permanent, silent no-op mode**, alongside the poisoned stamp (§ 3, fixed) and
   the undetermined lease (limitation 1, accepted).

   Why the `-` stays anyway. Without it, `systemd-analyze verify` exits non-zero
   with `Command … is not executable` for every operator who has not yet
   installed the CLI at that path — a repository whose shipped units do not
   verify cleanly is a worse and much more likely failure than this one, and the
   unit test pins the clean, empty-output exit. Dropping the `-` would also
   contradict this ADR's own "a timer run is never marked failed" ruling for the
   command's *ordinary* outcomes.

   The mitigation is therefore documentation, and it is now explicit rather than
   implied: `docs/scheduling.md` § 7 numbers the install steps, states that the
   symlink step must precede enabling the timer and why, adds a verification
   command between them, and carries a "When the timer says success but the
   database never stops" table mapping each journal line — including `Unable to
   locate executable` — to what it means. A future PR could close it properly by
   giving the service an `ExecCondition=` that tests for the binary (so a missing
   CLI leaves the unit *skipped*, which is visible, rather than *successful*), or
   by having `db doctor` report whether the timer has ever completed a real
   check.

## Verification

Specified test-first; every unit test is hermetic — none starts a container,
opens a socket, or reads the real wall clock (`now` and the psutil module are
both injectable seams).

- `tests/unit/test_activity.py` — the leaf: atomic writes, no path in either
  file's content, the monotonic rule and its poison ceiling (both boundaries),
  warn-once scoping, bounded/typed reads (oversized content, a directory, a
  symlink to a FIFO — the last two in a subprocess with a hard timeout so a
  hanging implementation fails rather than hangs), lease identity and the full
  tri-state, and every `evaluate_idle` branch including the exact staleness
  boundary.
- `tests/unit/test_activity_architecture.py` — both halves of the mutual
  ignorance (activity imports no container/lifecycle/CLI module; lifecycle
  imports no activity module), the lazy `psutil` import checked at the AST level,
  and the "not re-exported from `partgraph.util`" precedent.
- `tests/unit/test_cli_idle_stop.py` — the command: the zero-I/O disabled path,
  a live lease blocking a stop, delegation to the real `stop_all` with `db
  down`'s own seam (verified by invoking the captured `compose_down` and
  comparing argv), the real cve-graph host fixture left untouched, never
  autostarting with `PARTGRAPH_AUTOSTART=1`, and exit 0 *with* a path-free
  diagnostic when a survivor or an undetermined container remains.
- `tests/unit/test_cli_activity_wiring.py` and
  `..._wiring_architecture.py` — per-command ordering (stamp after the work,
  inside the lease; no stamp when the work raised) plus a mechanical AST
  reachability scan over all nine commands, so a tenth added later cannot forget
  the wiring.
- `tests/unit/test_systemd_idle_stop_units.py` — the shipped units: no `User=`,
  `%h` only, the named hardening directives, a cadence inside a sane bound,
  nothing in the repository enabling them, and `systemd-analyze verify` exiting 0
  with empty output against the real files. **The scope of the "nothing enables
  it" guarantee, stated rather than assumed:** that scan reads tracked `*.py`
  and `*.sh` files only, so a CI workflow, a Makefile, a container build, or the
  unit files themselves could enable the timer without the scan noticing. Today
  nothing does — the only two tracked lines that pair the unit's name with
  `enable` are `docs/scheduling.md`'s operator instruction and the `.timer`'s own
  comment quoting it, and the repository has no Makefile — but that is a fact
  about the current tree, not something this test would catch changing outside
  those two extensions.
- `tests/unit/test_scheduling_idle_stop_interaction.py` — the refresh
  interaction of § 6, proven against the real decision function.

**Not executed:** no timer was installed or enabled on the authoring host, no
container was started or stopped, and no real database was left idle to observe
a real stop. The stop path itself is the one PR-A already exercised end-to-end
for `db down`; PR-C adds the decision about *when* to invoke it.
