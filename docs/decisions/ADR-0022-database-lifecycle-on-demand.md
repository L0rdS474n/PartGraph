# ADR-0022: The database runs on demand — documented, detected, never executed

- Status: Accepted
- Date: 2026-07-28

## Implementation status (read this first)

This ADR spans **two** pull requests. That is a first for this repository —
every prior ADR landed with one PR — so the split is stated plainly here rather
than left to be inferred from which sections happen to have code behind them.

| | PR-B1 (this PR) | PR-B2 (not yet written) |
| --- | --- | --- |
| `docs/db-lifecycle.md` operator procedures | **shipped** | — |
| `partgraph db doctor` (read-only diagnostic) | **shipped** | — |
| `partgraph.util.lifecycle.volume_exists()` | **shipped** | — |
| Bounded `find_partgraph_instances()` fan-out | **shipped** | — |
| "documents and detects, never executes" guard | **shipped** | — |
| `ensure_running()` lazy-start helper | not present | to build |
| `PARTGRAPH_AUTOSTART=0` escape hatch | not present | to build |
| Commands that need the database starting it lazily | not present | to build |

Sections 1-6 below describe **shipped** behaviour. Section 7 describes what
PR-B2 completes and is explicitly **not implemented yet** — no `ensure_running`
symbol exists in `src/` today, and no command starts the database implicitly.
An idle-stop policy is out of scope for both and belongs to PR-C.

## Context

ADR-0021 gave `partgraph db down` the power to stop **every** lifecycle owner,
not just Compose. It did not address why the database was running at all.

The quadlet-generated unit `partgraph-dgraph.service` carries
`[Install] WantedBy=default.target`, so it starts the database **at every
login**, whether or not anyone invokes `partgraph`. The measured cost on the
affected host was a single instance idle for **14 h 10 m** at **10.2 GB** peak
memory. `db down` stops it; nothing stopped it from coming straight back.

Two facts constrain every possible answer:

1. **Quadlet units cannot be `systemctl --user disable`d** (podman-systemd.unit(5)).
   They are generated at boot from a `.container` file; there is no persistent
   enablement symlink to remove. The only documented removal is editing
   `WantedBy=` in the `.container` file or in a drop-in, then
   `systemctl --user daemon-reload`.
2. **That directory is not ours.** `$HOME/.config/containers/systemd/` holds
   PartGraph's unit *and five units belonging to unrelated projects*
   (`cve-alpha`, `cve-loader`, `cve-ratel`, `cve-zero`, `min-web` — the same
   stack ADR-0021 promised never to touch). Any code that wrote there would be
   mutating a directory shared with software this repository does not own, has
   never tested against, and cannot roll back.

## Decision

### 1. The repository documents and detects. It never executes.

PartGraph ships the **procedure** (`docs/db-lifecycle.md`) and a **read-only
diagnostic** (`partgraph db doctor`). It does not, and may not, write into the
systemd user unit directory, and it does not run `systemctl --user
daemon-reload`. Removing the autostart is an operator action, taken knowingly,
on files the operator owns.

**Enforcement is mechanical, not prose.**
`tests/unit/test_repo_never_executes_lifecycle_mutations.py` parses every
tracked `*.py` under `src/` and fails if any of `containers/systemd`,
`daemon-reload` or `quadlet` appears inside the argument subtree of a call this
process would itself execute (`subprocess.run`/`Popen`/`os.system`/`exec*`/…)
or a filesystem write (`open`/`write_text`/`replace`/`unlink`/…).

The line it draws is **semantic, not textual**, and it has to be: `db doctor` is
*required* to print `WantedBy=` and `daemon-reload` as instructions, and
`lifecycle.py`'s docstrings legitimately say "quadlet" eight times. A blanket
string ban would have made two acceptance criteria mutually unsatisfiable. So a
docstring, a comment and a `console.print(...)` are all left alone, while the
same literal inside an executed call is a failure.

The scanner resolves this repository's actual idioms rather than a textbook one:
a same-module wrapper function is dangerous if its own body calls something
dangerous (fixed point, any depth — `_run_capture` is how *every* real
subprocess call in `lifecycle.py` is written); a bare `argv` name is resolved
against its own scope's assignments; a term split across a pathlib `/` chain is
rejoined. Eight positive controls and three negative controls are self-tested
in the same file before it is trusted against the real tree.

### 2. `volume_exists()` — a new read-only leaf function

```python
volume_exists(*, engine_prefix=None, which=None, environ=None) -> bool | None
```

READ-ONLY, exactly one subprocess call: `<engine> volume inspect --format json
partgraph_dgraph_data`. Tri-state:

- **True** — the engine confirmed the volume exists (`inspect` exit 0);
- **False** — the engine confirmed it does not (non-zero exit);
- **None** — could not be determined: the call itself failed to run.

Three properties are load-bearing:

- **The return code is authoritative.** A corrupt or unparseable body on a
  *successful* inspect still returns True. Downgrading a confirmed positive
  because a payload failed to parse is the same class of bug ADR-0021 § 2a
  removed from `_mounts_data_volume`, pointed the other way.
- **`ContainerEngineError` is never caught here.** It propagates, exactly like
  `find_partgraph_instances()`: a probe that never ran must not decay into a
  guessed answer. The CLI catches it, as `db down` already does around its own
  `engine_command()` call.
- **There is no `volume_name=` parameter, by construction.** The signature's
  parameter set is exactly `{engine_prefix, which, environ}`, all keyword-only,
  and a test asserts that set. `PARTGRAPH_DATA_VOLUME` is a frozen constant like
  `PARTGRAPH_CONTAINER_NAME` and `PARTGRAPH_UNIT_NAME`; a caller-supplied target
  would reopen precisely the poisoned-target surface ADR-0021's allow-list
  discipline exists to close, one function below where that discipline is
  enforced.

**Why a bare `bool | None` and not a DTO.** `UnitState` is a dataclass because
it carries **four related facts** about one subject (`present`, `load_state`,
`active_state`, `wanted_by_default`) that are only meaningful read together, and
because adding a fifth later must not break callers. `volume_exists()` answers
**one** question and has no second fact to grow toward — a size, a mountpoint or
a driver would be a *different* function with a different cost profile, not a
field on this one. Wrapping a single boolean in a class would add a name, an
import and an attribute access to every call site and buy nothing. The tri-state
is carried by `None`, exactly as `UnitState.wanted_by_default` and
`_mounts_data_volume` already do, so the module has one convention for
"undetermined" rather than two.

A new named bound, `VOLUME_INSPECT_TIMEOUT_S = 10.0`, extends ADR-0007's
bounded-constant discipline: never a bare literal at a call site.

### 3. `partgraph db doctor` — the reporting half of ADR-0021

`db down` can stop every lifecycle owner. `db doctor` lets an operator **see**
what that same selector policy sees, plus what will restart the database, and it
can change none of it.

It reports, in this order:

1. running instances PartGraph owns (selector `S1`/`S2`), report-only port
   holders (`S3`), and any container whose ownership could not be verified
   (`UNKNOWN`);
2. the unit's presence and its raw `LoadState`/`ActiveState`, and whether
   `WantedBy` includes `default.target`;
3. whether the named data volume exists;
4. the remediation text — **unconditionally**, on every run.

The selector tags are ADR-0021's; the four-row S1/S2/UNKNOWN/S3 table lives in
[ADR-0021 § 2](ADR-0021-db-down-all-lifecycle-owners.md#2-selector-policy-s1--s2--unknown--s3-locked)
and is **linked, not restated**, so the two documents cannot drift.

**Output order is load-bearing.** `PARTGRAPH_UNIT_NAME`
(`partgraph-dgraph.service`) *contains* `PARTGRAPH_CONTAINER_NAME`
(`partgraph-dgraph`) as a substring, so the instance section must be printed
before the unit section: otherwise the first line mentioning the container is
the unit's line, and anything scanning output for "the line about the container"
finds the wrong one.

**Remediation is printed unconditionally**, not gated behind "autostart is
currently on". It is a static runbook, and a reader who has just been told the
answer is `unknown` needs it as much as one told `yes`. A conditionally-visible
instruction is also harder to reason about than an always-visible one.

#### 3a. Three deliberate divergences from `db up` / `db down`

- **A missing container engine is a finding, not a fatal error.** `db up` and
  `db down` genuinely need an engine to do their job, so failing loudly is
  correct there. `doctor` only reports: it catches `ContainerEngineError`, states
  that the instance and volume sections could not be determined, still runs the
  engine-independent unit query, and exits 0. A diagnostic that refuses to run
  because the thing it diagnoses is broken is not a diagnostic. It also issues
  **no** engine call at all once detection has failed.
- **A wedged enumeration is a finding too.** `find_partgraph_instances()`
  deliberately does not absorb a `ps` timeout (an enumeration that never happened
  must not degrade to an empty tuple, which reads as "nothing is running").
  `db down` turns that into exit 1. `doctor` absorbs it at the CLI layer and
  reports "could not be enumerated".
- **The exit code is always 0 once the command ran, and carries no health
  signal.** This is a real divergence from `db status`, `db check-index` and
  `db down`, whose non-zero exits all mean something is wrong. Every finding
  here — including a database that is up when you wanted it down — lives in the
  *output*. The command's own `--help` says so in as many words, because the
  failure mode of this decision is somebody wiring `db doctor` into monitoring
  and getting a permanently green check. Use `db status` for that.

#### 3b. Every value is rendered honestly, or as unknown

A tri-state never collapses in the renderer. `WantedBy` prints as `yes`, as a
statement that some other target wants the unit, or as `unknown` — and the
`unknown` line asserts neither direction, not even implicitly. The volume prints
`present` / `absent` / `unknown`. `LoadState` and `ActiveState` print
**verbatim**: paraphrasing them would hide the exact detail the operator ran the
command to see.

Printing raw systemd text verbatim is only safe because **all** of it goes
through a single `_console.print(..., markup=False, soft_wrap=True)` funnel at
the end of `doctor()`. That is deliberate architecture, not style: a test parses
`cli.py` and asserts every `.print(...)` inside `doctor()` carries
`markup=False`, and with one call site the property is structural instead of a
per-field habit that the next added field can forget. `ActiveState` is the field
exercised at runtime for this, not a container name — names are already
grammar-validated by ADR-0021's allow-list and cannot carry `[`.

### 4. Bounding the enumeration fan-out (a change to ADR-0021's code)

`find_partgraph_instances()` called `container inspect` — one subprocess, up to
`INSPECT_TIMEOUT_S` (10 s) each — **unconditionally for every usable row**,
before classification, with no ceiling on row count. Each call was bounded; the
sweep was not. On a host with thousands of containers and a degraded engine that
is hours of wall clock, and `db doctor` advertises itself as always safe to run.

PR-B1 raises that exposure, so PR-B1 owns the fix. Three bounds, in the order
they should be preferred:

1. **Skip rows the policy can already classify.** An exact `S1` name match is
   `S1` with or without mount evidence — ADR-0021 § 2's priority order returns
   before the volume test is consulted — so the call cannot change the verdict.
   This **removes** cost rather than truncating coverage, and weakens nothing.
2. **`MAX_INSPECTED_ROWS = 64`** — a finite ceiling on inspect calls per
   enumeration. Binding against a *large* host.
3. **`INSPECT_SWEEP_BUDGET_S = 30.0`** — a finite wall-clock budget across the
   whole sweep, checked before each call. Binding against a *wedged* engine,
   where the call count is small but each call burns the full watchdog.

Bounds 2 and 3 are both needed, because they bind different failure modes: a
call cap alone still permits 64 x 10 s = 640 s against a wedged engine, and a
time budget alone bounds no call count on a fast host. Worst-case sweep wall
clock is now `INSPECT_SWEEP_BUDGET_S + INSPECT_TIMEOUT_S` (~40 s), because the
deadline is checked *before* the call it admits, so an admitted call can overrun
it by at most one watchdog. 64 is an order of magnitude above the six containers
actually present on the affected host, and an order of magnitude below the
2 000-row case the acceptance test pins.

**What the ceiling costs, stated plainly.** Bounds 2 and 3 do truncate coverage
on a host large or slow enough to hit them, and that is a real trade, not a free
one. It is paid in *precision*, never in *false success*: a refused row's mount
status becomes `None` — UNDETERMINED, the identical tri-state a failed inspect
produces — so ADR-0021 § 2a's rules apply unchanged. A refused row that holds one
of PartGraph's ports still escalates to `UNKNOWN`, which `db down` reports and
exits 1 on. What is genuinely lost is an S2 duplicate past the ceiling that
publishes none of our ports; that is the same case ADR-0021 § 2b already accepted
losing, for the same reason (Badger refuses two writers on one data directory, so
such a container crashes on the lock rather than running silently). The whole
sweep's degradation is logged **once**, with a count and no engine-derived
string — a thousand identical warnings would bury every other line.

Two alternatives were rejected. **Batching** all IDs into one
`container inspect id1 id2 …` call would have removed the cost entirely rather
than capping it, and is the better design in the abstract — but resolving
per-container failures out of a batched response is a different contract than
the per-row one ADR-0021's tests pin, and rewriting those tests to fit an
implementation is backwards. **`ps --filter volume=`** is forbidden outright:
ADR-0021 § 3 already rules that `--filter` is a regex on both engines and can
never be ownership authority.

`Instance.mounts_data_volume` therefore now has a third meaning: "never asked".
It was already documented as display-only, with `owned_by` authoritative for the
distinction; its docstring now says so explicitly rather than implying that
`False` always means an inspect happened.

### 5. Procedure 1 — removing the autostart (`WantedBy=`)

Documented in `docs/db-lifecycle.md`, verified against podman-systemd.unit(5):
create `$HOME/.config/containers/systemd/partgraph-dgraph.container.d/`, write an
`override.conf` whose `[Install]` section contains an **empty** `WantedBy=`
(an empty assignment resets an inherited list rather than adding to it), then
`systemctl --user daemon-reload`.

Two properties of the *document*, both mechanically tested:

- **A drop-in, never a direct edit** of the generated `.container` file. A
  drop-in's own path names the single unit it overrides; "open the file in that
  directory and edit it" is an instruction that can be followed *correctly* on
  the wrong file.
- **The specific unit is named, and the neighbours are fenced off.** The
  document names `partgraph-dgraph.container` beside the instructions and tells
  the reader to touch nothing else in that directory. An earlier draft passed
  every other content test while never once saying which of the six files was
  ours — a document that, followed correctly, could stop a stranger's service.
- **No operator home path anywhere**: `$HOME` and `%h` only, so the procedure is
  copy-pastable by any operator. The repository-wide private-data scanner
  (`tests/unit/test_repo_skeleton.py`) enforces this across every tracked file.

### 6. Procedure 2 — `StopTimeout=`, and the caveat that must travel with it

The same drop-in directory can carry `[Service] StopTimeout=`. The document
presents it with its honest caveat **adjacent**, and the test asserts adjacency
rather than mere presence, because a caveat two sections away from the recipe is
a caveat nobody reads:

> A `StopTimeout=` drop-in does **not** give the container an init process.
> `dgraph/standalone` declares no `ENTRYPOINT` and no `STOPSIGNAL`, and its
> `/run.sh` runs `dgraph alpha` in the foreground under bash; PID 1 is bash,
> which defers signals until its foreground command exits, so **SIGTERM is still
> not delivered** and the outcome is **still SIGKILL**. Raising the timeout only
> lengthens the wait for the same kill.

ADR-0021 § 7a fixed this with `init: true` — but **only for Compose-started
instances**, because the quadlet unit runs `podman run` directly and never passes
through Compose. So the document's real recommendation is not to raise the
timeout at all: stop using the quadlet unit (§ 5) and start the database with
`partgraph db up`, which goes through Compose and gets the init process. No data
is at risk either way; Badger's write-ahead log meant the earlier SIGKILLs cost
nothing (613,396 `Part` nodes verified intact).

### 7. What PR-B2 completes — NOT IMPLEMENTED

Removing the autostart leaves a gap this ADR opens and does not close: the
database no longer starts by itself, so a command that needs it must say so, or
start it. PR-B2 adds:

- **`ensure_running()`** in `partgraph.util.lifecycle` — start the database if
  it is not already up, idempotently, through the Compose path (which gets the
  init process, § 6), and never through the quadlet unit.
- **`PARTGRAPH_AUTOSTART=0`** — an explicit escape hatch for operators who want
  the database started only by hand, and for CI, where an implicit start would
  be a surprise.
- **Lazy start** for the commands that genuinely need a live database.

None of these exists today. `db doctor` deliberately does not start anything,
and nothing in PR-B1 depends on them. An **idle-stop** policy (stopping a
database nobody has used for some interval) is out of scope for both PRs and is
PR-C's subject.

## Alternatives rejected

- **Have PartGraph edit the unit file itself.** Rejected on ownership grounds,
  not difficulty: that directory holds five units belonging to another stack.
  The gap between "write the file we generated" and "write a file in a shared
  directory" is exactly where an automated fix stops being safe. A `db doctor
  --fix` flag is the same decision wearing a flag.
- **`systemctl --user disable partgraph-dgraph.service`.** Does not work on
  quadlet units at all (§ Context); documenting it would have been documenting a
  no-op.
- **`systemctl --user mask`.** It works, but it is a bigger hammer than asked
  for: it makes the unit unstartable *by any means*, including deliberately, and
  it is easy to forget you did it. Removing `WantedBy=` removes exactly the
  behaviour complained about and leaves `systemctl --user start` working.
- **Making `db doctor`'s exit code carry a verdict.** Tempting, and rejected:
  there is no non-arbitrary mapping from "the unit exists and autostart is on"
  to "failure". That is a *finding*, and whether it is bad depends entirely on
  what the operator wants. `db status` already owns the "is it healthy" exit
  code.

## Verification

Specified test-first; every test is hermetic — none opens a socket, starts a
container, or reads a real clock. `subprocess.run`, `shutil.which` and cli.py's
own `engine_command`/`probe_health` are the only patch points, so the real
`unit_state()`, `find_partgraph_instances()` and `volume_exists()` run
end-to-end through the CLI.

`tests/unit/test_lifecycle_volume.py` pins `volume_exists()`: True on exit 0,
False on non-zero, None on timeout and on `OSError`, True even when a successful
body is unparseable; exactly one subprocess call; no `-f`/`--force`/`rm`/
`create`/`prune` token in its argv; `shell=False`, `capture_output`, `text`,
`check=False` and `timeout` read from `VOLUME_INSPECT_TIMEOUT_S` rather than a
literal; engine auto-detection through `engine_command()`; `ContainerEngineError`
propagating uncaught; and the signature's exact keyword-only parameter set, so no
`volume_name=` can appear later.

`tests/unit/test_cli_db_doctor.py` drives `db doctor` through the CLI against a
scripted fake that recognises **only** four read-only call shapes and raises on
anything else — a fail-fast backstop — and *additionally* scans
`subprocess.run`'s entire `call_args_list` for any of stop/rm/down/up/prune/
create/start/enable/disable/daemon-reload/kill/restart/reload. It pins the unit
line, all three `WantedBy` renderings (including that an unknown one claims
neither direction), S1/S2 shown as PartGraph's own, S3 shown as report-only and
never as ours, all three volume renderings, unconditional remediation text that
is printed and never executed, exit 0 across every finding, a missing engine and
a wedged `ps` both absorbed, and the real cve-graph fixture never named in any
argv **or** in any output line — with a positive control proving a generic
container holding a watched port *does* still surface, so that guarantee cannot
pass by being vacuous. Two tests cover markup: one runtime (a bracket-bearing
`ActiveState` surviving verbatim) and one static (every `.print(...)` inside
`doctor()` carries `markup=False`). One test bounds the fan-out: 2 000 synthetic
unrelated rows must not produce O(rows) subprocess calls.

`tests/unit/test_db_lifecycle_docs.py` pins `docs/db-lifecycle.md`: a `$HOME`/`%h`
placeholder present, `WantedBy=` named with removal language beside it, the exact
phrase `systemctl --user daemon-reload`, the removal documented as a drop-in, the
specific unit file named in the same window, a warning against the neighbouring
units, `StopTimeout=` documented as a drop-in, and the honest caveat — no init
process, SIGTERM undelivered, still SIGKILL — adjacent to it.

`tests/unit/test_repo_never_executes_lifecycle_mutations.py` scans every tracked
`*.py` under `src/` (§ 1), and self-tests its own scanner with eight positive and
three negative controls first.

`ruff check .` is clean; the full non-integration suite is **1278 passed, 0
failed, 0 skipped, 0 collection errors**.

**Not verified, and deliberately so.** Nothing here was exercised against a live
container engine or a real systemd unit in this PR — no container was started and
nothing was written to any unit directory, which is the same promise § 1 makes to
the operator. The operator procedures in § 5 and § 6 are derived from
podman-systemd.unit(5) and from the § 6 measurements recorded in ADR-0021, not
from a fresh live run.
