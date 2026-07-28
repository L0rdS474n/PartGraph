# ADR-0022: The database runs on demand — documented, detected, never executed

- Status: Accepted
- Date: 2026-07-28

## Implementation status (read this first)

This ADR spans **two** pull requests. That is a first for this repository —
every prior ADR landed with one PR — so the split is stated plainly here rather
than left to be inferred from which sections happen to have code behind them.

| | PR-B1 | PR-B2 |
| --- | --- | --- |
| `docs/db-lifecycle.md` operator procedures | **shipped** | — |
| `partgraph db doctor` (read-only diagnostic) | **shipped** | — |
| `partgraph.util.lifecycle.volume_exists()` | **shipped** | — |
| Bounded `find_partgraph_instances()` fan-out | **shipped** | — |
| "documents and detects, never executes" guard | **shipped** | — |
| `ensure_running()` lazy-start helper | — | **shipped** |
| `PARTGRAPH_AUTOSTART` escape hatch | — | **shipped** |
| Commands that need the database starting it lazily | — | **shipped** |
| Compose `restart: "no"` | — | **shipped** |
| Idle auto-stop | out of scope | out of scope |

Sections 1-6 describe PR-B1. Section 7 describes PR-B2, which is now
**delivered**: `ensure_running()` exists in
`src/partgraph/util/lifecycle.py`, nine commands start the database lazily
through it, and `PARTGRAPH_AUTOSTART` turns that off.

**Idle auto-stop belongs to neither PR.** Stopping a database nobody has used
for some interval is PR-C's subject and nothing here implements, approximates
or prepares for it. PR-B2 only ever *starts*: no code path in it stops a
container, and `db down` remains the only way anything in this repository
stops one.

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

### 7. The database starts on first use (PR-B2)

Removing the autostart leaves a gap § 5 opens and does not close: the database
no longer starts by itself, so a command that needs it must say so, or start
it. PR-B2 closes it — and closes it *only* in the start direction. The
operator's request was that the database start when `partgraph` is first
invoked, not that it sit running: 14 h 10 m idle at a 10.2 GB peak is the cost
being removed, and a lazy start removes it without asking anyone to remember a
`db up`.

#### 7a. `ensure_running()` — the contract

```python
ensure_running(
    *,
    probe_health: Callable[[], Any],
    compose_up: Callable[[], None],
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> None
```

Three steps, in order:

1. **Probe health. If healthy, return.** `compose_up` is not called and nothing
   is slept on. Since `compose_up` is the only seam that can reach a container
   engine, the common case costs exactly one local HTTP probe and **zero engine
   subprocesses**. This is not an optimisation; it is the property that makes
   autostart acceptable on every command rather than a tax on all of them.
2. **Otherwise call `compose_up` exactly once.** Any exception it raises is
   absorbed (§ 7b). Never retried.
3. **Poll**: `sleep(AUTOSTART_POLL_INTERVAL_S)`, probe, return on healthy,
   raise `AutostartTimeoutError` once `AUTOSTART_READY_TIMEOUT_S` is spent. The
   deadline is computed **once**, after step 2, so the start command's own
   duration — a first-run image pull, say — is not charged against the
   readiness budget.

Success is signalled by returning `None` and failure only by raising. There is
deliberately no boolean return: a caller who ignored a `False` would go on to
talk to a database that is not there, and this function exists precisely to
stop that happening.

`probe_health` and `compose_up` are **required, keyword-only, no default**,
following `stop_all`'s `compose_down` (ADR-0021): a caller must decide
explicitly how health is checked and how the database is started, rather than
silently inheriting a permissive default that would make the whole function a
no-op. `sleep`/`monotonic` default to `None` and resolve at *call* time to
`time.sleep`/`time.monotonic`, mirroring `_resolve_which` — so production
never passes them, and tests never sleep for real.

`AutostartTimeoutError` subclasses `RuntimeError`, like `ContainerEngineError`,
and its `str()` **is** the complete user-facing message: one line, path-free,
naming the budget it spent and the one command that answers the question the
operator is left with (`partgraph db status` — did it come up after all?). The
CLI prints it verbatim and exits 1, with no traceback.

Two new bounded constants extend ADR-0007's discipline to the start path:
`AUTOSTART_READY_TIMEOUT_S = 120.0` and `AUTOSTART_POLL_INTERVAL_S = 1.0`.

**Both are judgement calls, and unlike `STOP_GRACE_SECONDS` neither is backed
by a measurement.** `STOP_GRACE_SECONDS` has live numbers behind it (0.2 s with
`init: true`, 60.2 s without); Dgraph's *first-run readiness* time on this host
has never been measured. What bounds the choice is the asymmetry of the two
errors. Too small is the worse one: the command fails while the database it
asked for comes up seconds later, and the operator is told the opposite of what
happened — on a store that reached 613,396 `Part` nodes, Badger's log replay is
not instant. Too large costs only a wait on a database that will never come up.
So the budget errs upward, and 120 s is a generous-but-not-absurd landing
point. The poll interval is floored by the health probe's own per-request
timeout (`HEALTH_PROBE_TIMEOUT_S = 2.0`): polling faster than that queues
requests against a socket that is not listening yet and buys nothing.

#### 7b. Absorb-and-poll: why `compose_up` is not `compose_down`

`stop_all`'s `compose_down` **propagates**. `ensure_running`'s `compose_up` is
**absorbed** — logged, never raised. That asymmetry is the design, not an
oversight, and it is worth stating why in full because it looks like an
inconsistency.

A stop command's failure is evidence: if teardown failed, the database may
still be running, and reporting success would be a lie ADR-0021 exists to
prevent. A **start** command's failure is not evidence of anything about the
database. Two `partgraph` invocations can race — the operator runs `search` in
one terminal and `stats` in another — and the loser is told *"container name
`partgraph-dgraph` is already in use"* by an engine that is, at that moment,
starting the very database the loser wants. Treating that exit code as
authoritative would fail a command that is about to succeed.

So the start attempt is best-effort and **the health probe is the sole
verdict**. No fabricated lock, no lock file, no PID file, no advisory
flock — health is the truth. This one decision makes both cases fall out
correctly with no special-casing:

- a **genuine, unrecoverable** start failure (no engine, port taken, corrupt
  volume) is still reported — one bounded wait later, by step 3, because health
  never arrives;
- a **lost race** proceeds normally, because health does arrive.

The two are separated by what happens next, never by what the start command
said.

The cost of absorbing is that the diagnosis could be thrown away, and it is
not. It is kept in **two** places, for two different readers:

- The absorbed exception's **type name** is logged at `WARNING` through the
  module's own `_LOGGER`, alongside `_stop_unit_if_active`'s and
  `_mounts_data_volume`'s existing absorbed-failure records.
- The absorbed exception becomes the eventual `AutostartTimeoutError`'s
  `__cause__` (`raise … from absorbed`), and `partgraph.cli` renders its type
  on a second stderr line when the poll does fail. This is the operator-facing
  copy, and it exists because the log record reaches a terminal only through
  `logging.lastResort`: nothing in `src/partgraph/` configures logging, so any
  future logging setup could redirect or drop it. A user-facing diagnostic
  must not depend on that.

Nothing is rendered on the **recovered** path — a lost race whose winner
brought the database up did not go wrong, and printing an error for it is
exactly the false alarm § 7c exists to prevent.

Only the type, never the message, in both places: a class name cannot contain a
path separator, whereas engine stderr routinely quotes a compose file location,
and every error line this project prints is path-free. The type is enough to
separate the two failures that matter — `ContainerEngineError` ("there is no
container engine on this host") from `AutostartComposeError` ("the engine ran
and refused"). It is **not** enough to tell a lost race from a genuine non-zero
exit: those share a type, and the honest answer is that only the poll's outcome
distinguishes them.

#### 7c. The `compose_up` seam does not print

`cli.py`'s `_run_compose` prints a red `Error:` line to stderr *before*
raising. Reusing it verbatim as the `compose_up` seam would have made the
losing side of a race print `Error: failed to start the Dgraph database (the
container engine exited with code 125)` and then complete successfully with
exit 0 — a message describing something that did not go wrong, on a command
that worked.

So `_autostart_compose_up()` is a separate, print-free function producing the
**identical argv** (`<engine> compose -f <abs COMPOSE_FILE> up -d`), the same
`shell=False` list-argv discipline, the same engine detection through
`compose_command()`, but its **own** watchdog — `AUTOSTART_COMPOSE_TIMEOUT_S`,
not `db up`'s `COMPOSE_TIMEOUT_S` (see Breaking changes § 2). It signals failure by
raising; whether that failure matters is the poll's decision, and only if it
does does anything reach the operator.

#### 7d. `PARTGRAPH_AUTOSTART` — the parsing table

Read from `os.environ` at call time, stripped, compared case-insensitively.

| Value | Autostart |
| --- | --- |
| unset | **on** |
| `""` (empty) | **on** |
| `0`, `false`, `no`, `off` (any case, any surrounding whitespace) | **off** |
| `1`, `true`, `yes`, `on` | **on** |
| anything else (`banana`, `disable`, `n`, …) | **on** |

Three decisions are packed in here.

**Why parsing lives in `cli.py`, not the leaf.** `ensure_running()` decides
from its injected seams and a health probe only. If it also consulted the
environment, the same function would answer differently for identical
arguments, and every future caller would inherit an invisible dependency on a
variable it never mentions. The environment is CLI policy; the CLI owns it.

**Why `off` is in the disable set.** The ADR names exactly one spelling
(`PARTGRAPH_AUTOSTART=0`), but `false`/`no`/`off` are the numeric, generic-CLI
and systemd vocabularies for the identical intent, and an operator reaching for
this variable is overwhelmingly likely to type one of them. The two directions
of error are **not** symmetric. Failing open on an unrecognised value changes
nothing, because autostart is already the default. Failing open on a value the
operator plainly meant as "off" does the *opposite* of what they typed — and
the action being withheld is starting a container on a host that also runs an
unrelated cve-graph stack. That asymmetry is what makes `off` mandatory rather
than merely nice.

**Why the set stops at four.** The same asymmetry, pointed the other way.
Every token added to the disable set is a token a typo can land *inside*,
silently switching a default-on feature off with no diagnostic — the one
failure mode that is genuinely hard to notice. `n`, `disable`, `0.0` and
friends are therefore a deliberate non-goal, not an oversight to be fixed
later.

#### 7e. Where autostart is wired, and where it is not

Nine commands need a live database: `search`, `show`, `stats`, `embed`,
`refresh`, `refresh-links`, `db apply-schema`, `db check-index`, and
`ingest jlcparts` — the last **only** once it reaches its load stage, never
during fetch or normalize, which touch no database at all.

Nine commands, but **three** wiring sites, because seven of them reach Dgraph
exclusively through one helper:

1. `_connect_dgraph()` — a thin wrapper that runs the autostart gate and then
   calls the existing `_build_dgraph_client()`. Every one of those seven
   commands (and `_stage_load`) calls it.
2. `db apply-schema`, which uses its own gRPC path.
3. `db check-index`, which queries the HTTP endpoint directly.

A wrapper rather than a gate inside `_build_dgraph_client()` itself: that
function's single job is constructing a client with the right gRPC ceiling, and
every caller of it — including a future one that already holds a live database
— would otherwise inherit a container-start side effect it never asked for.
Keeping "connect to the database" and "make sure there is a database"
separately callable is exactly what lets sites 2 and 3 take the second without
the first.

Both explicit sites are placed **after** their local, non-database work: after
`load_schema()` reads the schema file, so a missing or malformed local schema is
reported without starting a container for it. The same rule governs
`_connect_dgraph()`'s call sites — all of them sit after every flag has been
validated, so a bad `--limit` never starts anything.

Commands that must **never** autostart, and why:

- **`db status`** — ADR-0018 makes it an engine-independent health probe. A
  probe that starts what it measures is a broken instrument.
- **`db down`** — starting what you are trying to stop needs no argument.
- **`db doctor`** — PR-B1 made it strictly read-only (§ 3a).
- **`db up`** — its own single `compose up -d` *is* the job.
- **`version`, every `--help`** — they touch nothing.

#### 7f. `restart: "no"` in `docker/docker-compose.yml`

`restart: unless-stopped` became `restart: "no"` (quoted: bare `no` is YAML's
boolean `False`, a different and invalid Compose value).

Under rootless podman nothing revives a container at boot — there is no
user-session daemon running then — so the old policy advertised a lifecycle
guarantee it could not keep on this host. What actually restarted the database
was the quadlet unit, and removing that is the entire point of § 5. With every
command now able to start the database itself, an engine-level restart policy
has nothing left to contribute. `container_name`, the loopback port bindings,
`init: true` and `stop_grace_period` are untouched.

## Breaking changes

This is a real contract change, not an internal refactor, and it changes what
`partgraph` does on a host where the database is down. Three consequences,
stated plainly:

1. **Read-only commands can now start a container as a side effect.**
   `search`, `show` and `stats` modify nothing in the database and never will —
   but from this release they may create and start `partgraph-dgraph` before
   reading from it. Anyone who relied on "a read-only command touches no
   container lifecycle" no longer can. `PARTGRAPH_AUTOSTART=0` restores the old
   behaviour exactly.
2. **Behaviour and timing change for a down database.** Previously any of the
   nine commands failed fast with the path-free "Is the database running? Start
   it with `partgraph db up`." hint. Now, unless `PARTGRAPH_AUTOSTART` is set
   to a disable token, they attempt a start and then wait. **The worst case is
   240 s — four minutes — before any error text appears**, and it is bounded by
   two constants that are added, not multiplied:

   | | | |
   | --- | --- | --- |
   | the `compose up -d` call | `AUTOSTART_COMPOSE_TIMEOUT_S` | 120 s |
   | the readiness poll after it | `AUTOSTART_READY_TIMEOUT_S` | 120 s |
   | **worst-case total** | | **240 s** |

   The ordinary case is nothing like that: a container create and start on an
   existing image and volume is seconds, and a database that is already up
   costs one HTTP probe. 240 s is reached only when the engine is wedged, or
   the database comes up and never answers.

   `AUTOSTART_COMPOSE_TIMEOUT_S` is deliberately **not** the 1800 s
   `COMPOSE_TIMEOUT_S` that `db up` uses. Inheriting it made the worst case
   1800 + 120 s — about **32 minutes** of silence on a command someone typed to
   look up a part. `db up` keeps 1800 s because the operator asked for a
   database explicitly and expects a first-run image pull; autostart is
   implicit and gets a budget sized for someone waiting at a prompt. The cost
   of the smaller bound is that a first-run image pull over a slow link may be
   cut short — in which case the operator is told to run `partgraph db up`,
   which is the command that keeps the generous budget, precisely because it
   was asked for.

   A script that measured its own runtime, or that treated a fast non-zero exit
   as "the database is down", sees different timing and a different message
   (`AutostartTimeoutError`'s, naming `partgraph db status`).
3. **A host with no container engine now fails slowly instead of fast.** With
   neither podman nor docker on `PATH`, `compose_command()` raises
   `ContainerEngineError` — which `ensure_running()` absorbs like any other
   start failure. The invocation therefore spends the **full 120 s readiness
   budget** before reporting a timeout, where it previously failed immediately.
   (The `compose up` half costs nothing here: detection fails before any
   subprocess is spawned.) This is the direct, accepted cost of § 7b's rule
   that the start command's outcome is never authoritative: the same absorption
   that lets a lost race succeed makes an unstartable host wait. The error
   names the absorbed failure's type on a second line, so this case is
   distinguishable from "the database came up and never answered". Setting
   `PARTGRAPH_AUTOSTART=0` restores the fast failure, and that is the
   documented answer for CI and for scripted use.

4. **`db check-index` is no longer unconditionally engine-independent.**
   ADR-0019's AC-IDX-25 pinned that `db check-index` never calls a container
   engine, mirroring `db status`. The *check* still does not — it reaches
   Dgraph over HTTP — but the *command* now runs the autostart gate first, so
   with autostart enabled and the database down it will invoke the engine. The
   command's own docstring says so explicitly rather than continuing to claim
   otherwise, and `PARTGRAPH_AUTOSTART=0` restores the unconditional
   guarantee. **`db status` is unaffected**: it is absent from the allowlist
   entirely and remains engine-free under all settings, because a probe that
   starts what it measures cannot report on it (ADR-0018).

The migration for every one of these is one variable, and it is the same
variable in all three cases.

## Amendment to ADR-0014 D1

ADR-0014 D1 reads *"the DB is never started here … The scheduling layer also
never manages the database lifecycle."* As written, that is **no longer
unconditionally true**, and this section amends it rather than leaving the
contradiction to be discovered.

`partgraph refresh` and `partgraph refresh-links` are both in § 7e's allowlist,
and `systemd/partgraph-refresh-all.service` — fired weekly by
`partgraph-refresh-all.timer` — runs exactly those two commands, unattended.
With autostart default-on and no opt-out, that timer would have started a
container **on a schedule**: the same unattended, nobody-asked-for-it container
this ADR exists to eliminate, reintroduced through the scheduling door after
§ 5 closed the login door.

**The amended guarantee.** D1's promise holds for the scheduling layer, and it
now holds *because the shipped scheduling layer opts out explicitly* rather
than because the CLI is incapable of starting anything. The opt-out lives in
the **wrapper**, `scripts/partgraph-refresh-all.sh`, which does

```sh
export PARTGRAPH_AUTOSTART=0
```

before it invokes `partgraph`, unconditionally and for both phases.

**Why the wrapper and not the unit — a correction.** An earlier draft of this
section put the guarantee in
`systemd/partgraph-refresh-all.service` as
`Environment=PARTGRAPH_AUTOSTART=0`, placed *after* the optional
`EnvironmentFile=-%h/.config/partgraph/refresh-all.env`, and justified that
placement by claiming systemd resolves same-key `Environment=`/
`EnvironmentFile=` directives in file order, last one wins.

**That claim is false, and the guarantee built on it did not hold.**
`systemd.exec(5)` states, of `EnvironmentFile=`, verbatim:

> Settings from these files override settings made with Environment=.

Unconditionally — the sentence makes no reference to the order the directives
appear in, and a live check confirmed it in both orders: `EnvironmentFile=`
won each time. So *any* `PARTGRAPH_AUTOSTART=` line in an operator's own
`refresh-all.env` — a file this repository documents and deliberately supports
— silently beat the unit's setting, and the weekly unattended run would have
started a container. Reordering the directives cannot fix it; only moving the
mechanism can.

A shell `export` inside the wrapper is applied **after** systemd has finished
assembling the environment and before `partgraph` is exec'd, so it is the last
word by construction and is immune to the precedence rule entirely. It also
covers the cron installation, which never involves systemd at all. The value
is hard-coded rather than read from the environment, because the environment
is precisely the channel that cannot be trusted to carry it; an operator who
genuinely wants a scheduled run to start the database edits that visible line
in a file they installed.

The unit **keeps** `Environment=PARTGRAPH_AUTOSTART=0` as defence in depth,
with its comment rewritten to say what it is: a second layer covering the case
where the wrapper is bypassed (an `ExecStart=` pointed straight at `partgraph`,
or a drop-in that replaces it) while the env file sets nothing for this key.
It is explicitly **not** the guarantee, and no longer claims to be.

An `EnvironmentFile`-only resolution was rejected for a separate reason: the
only env file this repo references is optional (leading `-`,
missing-file-is-non-fatal) and operator-created, and most installs will never
have one. A guarantee that depends on a file most operators do not have is not
a guarantee.

The unit's header comment and `docs/scheduling.md`'s `[!WARNING]` block both
name `PARTGRAPH_AUTOSTART` and both now name the wrapper as the mechanism, so
a reader is told *why* the "never manages the database lifecycle" claim holds
instead of finding it contradicted by `--help` — or, worse, believing a
mechanism that does not work.

**Scope of the amendment.** D1 is amended, not repealed. The scheduling layer
still runs no daemon, still does not health-check the database, and still
propagates a failed run to the host scheduler with the existing path-free hint.
What changed is that "never starts the database" is now a property of the
shipped unit's configuration rather than of the CLI's capabilities, and
therefore something an operator can undo — deliberately, by editing that line.

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
- **A lock file / PID file / advisory flock around the start (PR-B2).** The
  obvious answer to two invocations racing to start one container, and rejected
  because it invents state that can outlive the thing it describes: a stale
  lock after a SIGKILL — and § 6 records that SIGKILL was this container's
  normal exit until `init: true` landed — blocks a start that should have
  succeeded, and now needs its own staleness heuristic. Health is already an
  authoritative, self-cleaning answer to the only question being asked. See
  § 7b.
- **Propagating `compose_up`'s failure, for symmetry with `compose_down`
  (PR-B2).** It reads consistently and is wrong: the loser of a start race gets
  a genuine non-zero exit from an engine that is starting the database
  perfectly well, so the command would fail while succeeding. § 7b.
- **Reusing `_run_compose` as the `compose_up` seam (PR-B2).** Free, and it
  makes a recovered-from race print a red `Error:` line and then exit 0. § 7c.
- **Autostarting inside `_build_dgraph_client()` itself (PR-B2).** One fewer
  function, and it welds "start a container" onto every present and future
  caller of a helper whose job is constructing a gRPC client — including
  callers that already have a live database. § 7e.
- **Making `db status` autostart (PR-B2).** Never seriously considered, and
  recorded because it is the one that looks helpful: ADR-0018 makes it an
  engine-independent probe, and a probe that starts what it measures cannot
  report on it.
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

### PR-B2

`tests/unit/test_lifecycle_ensure_running.py` pins `ensure_running()`: the
healthy short-circuit calling neither `compose_up` nor `sleep` nor `monotonic`;
exactly one `compose_up` followed by polling until healthy; the bounded wait,
driven entirely by an injected clock scripted as fractions of the real
constants rather than hard-coded seconds, so no test depends on elapsed time; a
start failure that never recovers timing out cleanly; a start failure that
*does* recover returning normally; the absorbed failure appearing in this
module's own logger, path-free; the timeout message naming the budget and
`partgraph db status` on one path-free line; both required seams keyword-only
with no default; `sleep`/`monotonic` defaulting to `None` and resolving to the
patched `time` module at call time; and the four new names present in
`__all__`, which is where `test_lifecycle_architecture.py`'s re-export guard
derives its forbidden set from at run time.

`tests/unit/test_cli_autostart.py` drives the real `ensure_running()`
end-to-end through `search` for the argv-level contract — the autostart argv
equals `db up`'s byte for byte, with `shell=False` and a bounded `timeout=` —
and proves that an absorbed start failure followed by health recovery prints no
error text at all. The remaining allowlisted commands are verified by ordering
(each command's own database-touching mock raises unless autostart already
fired) plus seam identity (`probe_health` by `is`, and `compose_up` invoked in
isolation and asserted to issue `db up`'s argv), so a dummy callable placed
after the database work cannot satisfy them. The negatives — `db status`,
`db down`, `db doctor`, `db up`, `version`, and `--help` on all nine — assert
`ensure_running` is never called, and the parsing table is exercised value by
value.

`tests/conftest.py` forces `PARTGRAPH_AUTOSTART=0` for every test via an
autouse fixture, so no test that has never heard of autostart can start a
container during a plain `pytest` run;
`tests/unit/test_autostart_hermeticity.py` parses `conftest.py` and fails if
that fixture stops existing or stops being autouse.
`tests/unit/test_scheduling_autostart_disabled.py` pins the amendment above by
EXECUTING the wrapper against a stub `partgraph` with a hostile
`PARTGRAPH_AUTOSTART` already exported into its environment, and asserting the
stub observed `PARTGRAPH_AUTOSTART=0` on both phases — a behavioural check of
the mechanism that actually holds, not a text match on the unit file. The
unit's `Environment=` line is pinned separately, as defence in depth.

**Not verified, and deliberately so.** No container was started for this PR
either. `AUTOSTART_READY_TIMEOUT_S` and `AUTOSTART_COMPOSE_TIMEOUT_S` are
therefore unmeasured judgement calls (§ 7a and Breaking changes § 2 say so),
and the race described in § 7b was reasoned about and modelled in tests, never
reproduced against two live `partgraph` processes. The `EnvironmentFile=`
precedence correction in the amendment above IS verified — against
`systemd.exec(5)`'s own text and a live systemd reproduction in both directive
orders — but the shipped unit itself has not been installed and run by this
PR.

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
