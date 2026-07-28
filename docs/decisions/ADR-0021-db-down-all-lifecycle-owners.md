# ADR-0021: `partgraph db down` stops every lifecycle owner, not just Compose

- Status: Accepted
- Date: 2026-07-28

## Context

`partgraph db down` was exactly one call:

```python
_run_compose(["down"], action="stop")
```

That knows exactly **one** lifecycle owner: Compose. Compose only manages the
containers **it created and labelled itself** (the `com.docker.compose.project`
labels written on `compose up`).

A **second lifecycle owner exists on the affected host**: a quadlet-generated
`systemd --user` unit, `partgraph-dgraph.service`, produced from the *same*
`docker/docker-compose.yml` by a `compose2quadlet`-style converter. It declares
the same `ContainerName=partgraph-dgraph`, the same image, the same host ports
`8081`/`9081`/`8001` and the same named volume `partgraph_dgraph_data`, and it
carries `Restart=on-failure` and `WantedBy=default.target`. Compose cannot stop
it — it never created it, so it carries no Compose project label — and
`WantedBy=default.target` brings it back at the next login. `db down` therefore
printed `Dgraph stopped.` and exited `0` while a PartGraph database kept
running.

**That second owner is Podman-specific, and the fix is deliberately not.**
Quadlet is a Podman feature — Podman generates the `systemd --user` units from
`.container` files (`podman-systemd.unit(5)`) — so on a host running Docker
this particular second owner cannot exist, and neither can the login-time
resurrection it causes. The sweep below is nevertheless written to be
engine-agnostic. Two things about that are **verified in this repository's own
source**, and are the only things asserted here: the container phase builds its
argv through `engine_command()` (ADR-0009) rather than a hard-coded `podman`
literal, and the unit phase degrades to "no unit" without side effects — with no
`systemctl` on PATH, `unit_state()` returns `present=False` without running any
subprocess at all.

What is **not** verified is the step after that: that the resulting argv, run
against a real Docker daemon, stops the container as intended. That expectation
is inherited from ADR-0009's engine-interchangeability claim, which is itself
documentation-derived — no Docker daemon has ever been available on the host
this project is developed on (`/usr/bin/docker` there is a shell shim that execs
podman; there is no `dockerd`). So "a Docker user keeps the Compose-and-container
half of `db down`" is a statement about how the code is *built*, not a result
anyone here has observed, and it should be read with the same caution as every
other Docker claim on this branch.
What a Docker host *can* have instead is the daemon's own `restart` policy
bringing the container back at boot — a different mechanism with the same
symptom, addressed in ADR-0022 § 7f, which also states plainly that the Docker
behaviour there is read off Docker's documentation and has never been exercised
on a real Docker daemon by this project.

### What the incident actually was (measured, not assumed)

The operator reported *"four instances running"*. That report was investigated
directly, and the finding must be recorded plainly because it differs from the
report:

- At inspection there were **ZERO PartGraph containers running** and **exactly
  four dgraph-family containers**, and **every one of them belonged to the
  unrelated cve-graph stack** (`cve-zero`, `cve-alpha`, `cve-ratel`,
  `cve-loader`; an unrelated `min-web`/`nginx` container was also present).
- **There were never four duplicate PartGraph instances.** Nothing in this ADR
  should be read as evidence of PartGraph duplication.
- The **confirmed, measured waste** is a *single* PartGraph instance left
  running unattended by the second owner: **14 h 10 m idle, 10.2 GB peak
  memory, 8 m 49 s of CPU**. That is the cost this ADR removes.

The honest problem statement is therefore *"`db down` cannot stop what it did
not start"*, not *"`db down` leaks duplicates"*. The fix is still required —
one silently-surviving 10.2 GB instance is exactly the failure the command
exists to prevent — and it must be built so that it **provably never touches the
cve-graph stack**, which shares the host, the `dgraph/*` image family, and (for
`cve-alpha`) neighbouring port numbers.

## Decision

### 1. A new leaf: `partgraph.util.lifecycle`

`db down` delegates the whole sweep to one call into a new leaf module,
`partgraph.util.lifecycle.stop_all()`. The leaf's top-level imports are stdlib
plus `partgraph.util.container` (engine detection); `partgraph.util.health` is
imported **lazily** inside `stop_all`. It must never import `partgraph.cli` or
any embed/query/load module, so the CLI and future callers (a `ensure_running()`
helper, an idle-stop policy) can all import it without a cycle. Like
`health`/`index_health` (ADR-0018 Section 4) it is deliberately **not**
re-exported from `partgraph/util/__init__.py`. Both rules are enforced
mechanically by a source-text scan in
`tests/unit/test_lifecycle_architecture.py`, not by prose alone.

### 2. Selector policy S1 / S2 / UNKNOWN / S3 (locked)

Ownership is decided **in Python, by exact string comparison, over a full
enumeration**, and the four tags are tested in this **priority order**:

| # | Tag         | Selector                                                             | Action                      |
| - | ----------- | -------------------------------------------------------------------- | --------------------------- |
| 1 | **S1**      | container name is EXACTLY `partgraph-dgraph`                         | stop                        |
| 2 | **S2**      | CONFIRMED to mount the named volume EXACTLY `partgraph_dgraph_data`  | stop                        |
| 3 | **UNKNOWN** | mount status undeterminable (`inspect` failed) AND holds a watched port | **never stop; see §§ 2a, 2b** |
| 4 | **S3**      | CONFIRMED not to mount it, but holds host port `8081`/`9081`/`8001`  | **REPORT ONLY, never stop** |

Anything matching none of the four is not represented at all — it never appears
in a result under some other tag.

Exactness is load-bearing in both directions:

- S1 is string equality, never a prefix/suffix/substring test:
  `partgraph-dgraph-backup`, `Partgraph-Dgraph` and `" partgraph-dgraph"` are
  **not** PartGraph's.
- S2 compares the **volume name** for equality. `.endswith()` would wrongly
  match `partgraph_dgraph_data_backup`, and `in` would wrongly match
  `not_partgraph_dgraph_data`. (A legitimate `.endswith()` on this same volume
  name already exists in `tests/integration/test_dgraph_lifecycle.py` for a
  different purpose; it must not be copied into this selector.)
- S3 exists because *something* answering on PartGraph's port is worth telling
  the operator about — but "it is on my port" is not "it is mine", so it is
  never a stop target.

### 2a. UNKNOWN: an undetermined answer must not read as success

S2 is the **only** thing that recognises a PartGraph instance running under a
name Compose never chose — precisely the quadlet duplicate this ADR exists to
stop — and S2 is decided **solely** by one `container inspect` call. The first
implementation degraded any inspect failure (timeout, non-zero exit, unparseable
payload) to "does not mount our volume". That is a **false success**: the same
enumeration runs as both the pre-stop sweep and the post-stop verification, so a
container whose inspect failed twice was invisible to both, and `db down` exited
**0** — which this ADR contracts to mean "no PartGraph instance survives" —
while it kept running.

The mount probe is therefore **tri-state**: mounts / does not mount / **could
not be determined**. `None` is never collapsed into `False`.

- **UNKNOWN is tested BEFORE the S3 port check.** A container we could not
  classify must never be confidently downgraded to "just a port holder, safe to
  leave". S3 now means *positively confirmed not ours*, which is a stronger and
  more honest claim than before.
- **UNKNOWN is SCOPED to containers holding a watched port** (§ 2b).
- **UNKNOWN is never a stop target**, exactly like S3. We stop only what we
  positively know is ours; an unverifiable container is reported, not acted on.
  This keeps the cve-graph guarantee intact: an inspect failure can never
  escalate into stopping a foreign container.
- **`DownResult.undetermined`** carries the names still tagged UNKNOWN **after
  the final verification pass, and only that pass**. An inspect failure confined
  to the pre-stop sweep that resolves before verification is absorbed — the same
  "do not over-fire" treatment phase-1 systemctl failures get (§ 5). Only
  verification decides the verdict.
- **The exit code follows.** `db down` exits **1** whenever `undetermined` is
  non-empty, with a message containing "could not verify" and deliberately
  *not* "still running", so the two conditions stay textually distinct: one says
  the sweep positively failed, the other says the sweep cannot honestly claim to
  have succeeded. Exit 0 keeps its full strength — it promises that nothing
  PartGraph owns survived, and an unverifiable container cannot support that
  promise.
- **Both conditions are reported when both occur.** Survivors and undetermined
  containers are independent findings that can arise in the same run, so the CLI
  prints a line for each — as *separate* lines, never merged into one sentence —
  and only then exits 1. Reporting just the first would silently drop the other
  from an operator who needs both.

This mirrors the `_NOT_RUNNING_STATES` deny-list and `UnitState.wanted_by_default
is None` decisions already recorded here: throughout this module, an
undetermined answer degrades toward suspicion and a loud exit, never toward a
silent all-clear.

### 2b. …but the alarm is scoped to containers on PartGraph's own ports

The first cut of § 2a escalated **every** un-inspectable row. `ps --all` returns
every container on the host, and each one is inspected — including all five
cve-graph and `min-web` containers. So a single transient `inspect` timeout on,
say, `cve-alpha` during the verification pass produced:

```
Error: could not verify whether 1 container(s) belong to PartGraph: cve-alpha.
```

…and exit 1, even when PartGraph's own instance had stopped perfectly. Naming a
cve-graph container as *possibly PartGraph's* is exactly the conflation this ADR
exists to dispel — it made the honesty mechanism produce a dishonest sentence.

**The scope.** A row escalates to UNKNOWN only if it also holds one of
`PARTGRAPH_WATCHED_PORTS`. An un-inspectable row that holds none of our ports
and is not an exact S1 name match is **excluded entirely**, like any other
unrelated container. The reasoning: an instance we cannot identify has to be
*serving on one of PartGraph's own ports* to be the survivor the exit code is
about. One that holds none of them is neither interfering with PartGraph nor
PartGraph's business to raise an alarm about.

**The S1 path is unaffected.** The exact-name check runs *before* the mount
branch, so a container named exactly `partgraph-dgraph` is still S1 whether or
not its inspect succeeded — an exact name match needs no corroboration. The
scoping can only ever affect rows that already failed S1.

**A name heuristic was considered and rejected.** Widening the escalation to
"names that look like ours" would reintroduce precisely the `-backup` / `-old`
substring collisions that are forbidden for stop selectors (§ 2), and no case
was found that port scoping misses but a name heuristic would catch. Ports are
the only extra signal; the name is never one.

**The trade-off, stated honestly.** A hypothetical S2-only duplicate — one that
mounts `partgraph_dgraph_data` under some other name, publishes *none* of our
ports, *and* whose inspect fails on the verification pass — is now excluded
rather than reported. That is acceptable for a concrete reason, not by
assumption: **Badger refuses two writers on one data directory**. A second
container on that volume cannot run silently alongside the first; it fails to
acquire the directory lock and crashes rather than corrupting. A crashed
container is not `_is_stoppable` and so was never a survivor under any version
of this logic. The case the scoping gives up is one that cannot occur quietly.

#### Forbidden selectors

- **Image name is FORBIDDEN.** `dgraph/*` also matches cve-graph's `cve-zero`,
  `cve-alpha` and `cve-ratel`. An image-family selector would have stopped a
  foreign production stack.
- **The Compose project label is FORBIDDEN.** On the affected host the compose
  project name is literally `docker` (it is derived from the *directory* holding
  the compose file). A `com.docker.compose.project=docker` selector is both
  useless as an identity and dangerously broad.

### 3. `ps --filter` is never ownership authority

`podman ps --filter name=` is a **regular expression**, not an equality test:
`--filter name=partgraph-dgraph` also matches `partgraph-dgraph-backup` and
`my-partgraph-dgraph-2`. The module therefore enumerates with
`<engine> ps --all --format json` and classifies in Python. `--filter` and
`--ignore` never appear in any argv, and no Go-template field name (`{{.…}}`)
is relied upon.

The `--format json` **outer envelope** is undocumented and differs between
engines (a single JSON array vs. newline-delimited JSON), so the parser accepts
**both**. The **row shape** differs too, in three places, and all three are
accepted:

| Field   | Podman                                | Docker                                        |
| ------- | ------------------------------------- | --------------------------------------------- |
| id      | `"Id"`                                | `"ID"`                                        |
| name    | `"Names"` list                        | `"Names"` comma-joined string                 |
| ports   | `"Ports"` list of dicts (`host_port`) | `"Ports"` string, `"0.0.0.0:8081->8080/tcp"`  |

The docker-shaped name string is split **before** the allow-list runs, never
after, so a separator can never smuggle a second value past validation. The
docker-shaped ports string is split on commas, and each mapping's host port is
read after the LAST `:` of the host side (so an IPv6 host such as
`[::]:8081->8080/tcp` parses correctly); a segment without `->` publishes no
host port and is skipped, as is a host side that is not a plain in-range
integer — including a published RANGE such as `8081-8083`, which degrades to no
port for that segment. Ports only ever ADD the report-only S3 tag and never
override S1/S2/UNKNOWN, so a degraded port parse can never mis-stop anything;
its only cost is a missed S3 advisory.

Accepting all three shapes is what makes `PARTGRAPH_CONTAINER_ENGINE=docker`
route through this module genuinely rather than nominally. Before it,
`_row_ports` understood only Podman's shape, so on a Docker engine every
container degraded to "publishes no ports" and **S3 silently never fired at
all** — an undisclosed observability gap rather than a safety one, since ports
are not a stop authority.

Degradation is per-row: a non-dict array element, a row carrying no container ID
under either spelling, a row missing `Names`, and a row whose `Names` is an
empty list or an empty string are all **omitted** — never crashing the batch,
and never collapsing a batch into a silently empty selection. Empty, unparseable
or oversized output degrades to an empty result.

### 4. Phase order is load-bearing

1. **Query the unit.** `systemctl --user show partgraph-dgraph.service …`; if it
   is present *and* active, `systemctl --user stop partgraph-dgraph.service`.
2. **`compose down`** (the injected `compose_down` callback), still without
   `-v`.
3. **Engine `stop`** of every S1/S2 container that survived phases 1-2.
4. **Verification re-enumeration**, then one health probe.

The unit is stopped **first** so systemd does not restart the container that
phases 2-3 are about to remove (`Restart=on-failure`). The engine sweep runs
**last** because it is the only phase that reaches a container neither owner
admits to.

### 5. Deliberate error asymmetry between phase 1 and phase 2

- A **phase-1 failure is ABSORBED**: a non-zero `systemctl` exit or a timeout is
  logged and phases 2-4 still run. A systemd unit that refuses to stop must not
  prevent the phases that actually reach the container.
- A **phase-2 exception is NOT absorbed**: whatever `compose_down` raises
  propagates out of `stop_all()` **completely unmodified**, short-circuiting
  phases 3 and 4. A failed Compose invocation means the state of the world is
  unknown; sweeping and then reporting success on top of it would be a lie.

`compose_down` is REQUIRED, keyword-only, and carries **no default**: every
caller must decide explicitly whether and how Compose is invoked, rather than
silently no-op-ing phase 2 by inheriting a permissive default.

### 6. `stop` targets the container ID, never the name (Gate 3a)

Every engine `stop` (and every `container inspect`) targets the **opaque,
engine-assigned container ID**. The name is display-only.

This is not cosmetic. **S2 classifies by volume mount, independently of the
name.** Enforcing by name would reintroduce a TOCTOU window between the survivor
enumeration and the stop call: between the two, a *different* container could in
principle come to answer to that name, and the stop would land on it. An
engine-assigned ID of a live container cannot be reused that way. Targeting by
ID also means a foreign container's name never reaches a subprocess argv at all.

### 7. Verb surface: `stop` only

The module never runs `rm`, `volume rm`, `prune`, `-v` or `--volumes`. The named
data volume — and therefore every ingested part — always survives a `db down`.
Compose's own `down` still removes the containers Compose created; that is
Compose's behaviour, not an additional verb PartGraph issues.

### 7a. Making `stop` actually reach Dgraph

`stop` is only the *right* verb if the SIGTERM it implies actually arrives. It
did not, and the reason turned out to be nothing like the first diagnosis. This
section records the wrong answer as well as the right one, because the wrong one
was already shipped and a future reader deserves to know why the numbers look
the way they do.

**What surfaced it.** The journal on this host recorded, after a 14-hour run:

> `StopSignal SIGTERM failed to stop container partgraph-dgraph in 10 seconds,
> resorting to SIGKILL`

with the unit exiting **137**. `STOP_GRACE_SECONDS` was 10 and
`docker/docker-compose.yml` declared no `stop_grace_period` at all, so it also
inherited Compose's own 10s default: nowhere in PartGraph did Dgraph get more
than ten seconds.

**The first, WRONG explanation.** The obvious reading — a 14-hour Dgraph has
more to flush, so ten seconds is not enough — led to raising the budget to 60
and adding a matching `stop_grace_period`. That hypothesis was **disproven by
direct re-measurement**:

```
$ time podman stop -t 60 partgraph-dgraph   # container up ~1 minute
warning: StopSignal SIGTERM failed to stop container in 60 seconds,
         resorting to SIGKILL
real 1m0,221s
$ podman inspect ... --format '{{.State.ExitCode}}'
137
```

A container barely a minute old, with essentially nothing to flush, burned the
entire 60-second window and was SIGKILLed anyway. **The failure was never
load-dependent, and no grace-period value could ever have fixed it.**

**The real, structural cause.** `dgraph/standalone:v25.3.4` declares no
`ENTRYPOINT` and no `STOPSIGNAL`; its `CMD` is `/run.sh`, which reads verbatim —
including upstream's own acknowledgement:

```sh
# TODO properly handle SIGTERM for all three processes.
dgraph zero &
dgraph alpha
```

PID 1 is bash, running `dgraph alpha` as its **foreground** command, and bash
defers acting on a received signal until that foreground command exits. SIGTERM
sent to PID 1 is therefore never forwarded to `dgraph alpha` — regardless of
uptime, load, or how long anyone waits.

**The fix: `init: true`.** Setting it on the `dgraph` service puts a real init
process (podman's `podman-init`/tini) at PID 1 instead of bash, and an init
process *does* forward SIGTERM to its child. Measured on this host:

| Configuration    | PID 1          | `podman stop -t 60` | Exit code        |
| ---------------- | -------------- | ------------------- | ---------------- |
| without `init`   | `bash /run.sh` | 60.2 s              | 137 (SIGKILL)    |
| with `init: true`| `podman-init`  | **0.2 s**           | **143 (SIGTERM)**|

A unit test pins `init: true` and `stop_grace_period` **together**, because
either alone is not the fix: without the flag the budget is close to useless,
and without the budget the flag has nothing to bound.

**What the numbers mean now.** `STOP_GRACE_SECONDS` stays **60** and
`STOP_TIMEOUT_S` stays **90.0** (a 30-second margin, three times the minimum the
tests enforce, because the failure modes are not symmetric: too small a margin
destroys a slow-but-healthy shutdown and misreports it, too large only delays
the report of an already-wedged engine). But their *meaning* changed. Before,
60 was an always-paid delay that never once produced a graceful shutdown. Now it
is a **rarely-reached ceiling**: a healthy Dgraph exits in a fraction of a
second, and the budget only matters when there genuinely is a lot to flush.

**60 is still a judgement call, not a measurement.** The 0.2 s figure was taken
on a near-empty instance and says nothing about a busy one — the busy case has
**never been measured**. 60 is retained as a deliberate safety factor because
the trade is lopsided for a local tool: a slower `db down` costs a human a few
seconds, while a premature SIGKILL costs recovery work on a multi-GB store.

Badger's write-ahead log meant the earlier SIGKILLs cost **no data** — 613,396
`Part` nodes verified intact afterwards. This whole section is about **shutdown
correctness and restart cost, not data integrity**.

**HONESTY BOUNDARY — what this does NOT cover.** Both parts of the fix reach
only the lifecycle paths PartGraph owns directly: this module's own engine
`stop` sweep and Compose. Neither reaches the quadlet path, and this is worth
stating explicitly rather than leaving implied: **the quadlet unit does not go
through Compose, so it gets no init process from `init: true` at all**, and when
`stop_all` stops `partgraph-dgraph.service` that unit's own
`ExecStop=podman rm -v -f` applies the stop timeout baked into the container at
generation time. Nothing in this repository can influence either. A
quadlet-started Dgraph therefore still gets bash as PID 1 and is still SIGKILLed;
fixing that needs host-side unit changes (a `StopTimeout=` drop-in and an init
process in the generated unit), which is **PR-B1** work and deliberately out of
scope here. A test pins that the `systemctl` argv never carries our grace value,
precisely so this claim cannot silently rot into an implied guarantee. `db down`
does not promise a graceful shutdown in every case — only on the two paths
PartGraph controls.

### 8. Untrusted engine output is validated at the boundary

- Container names **and** IDs must match a **positive allow-list** — the
  Docker/podman grammar `^[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z`, plus a finite length
  bound — before they are used for anything. A deny-list of hostile characters
  could never be exhaustive. A rejected row is excluded before classification,
  so its raw text reaches neither a subprocess argv nor a rendered message.
  (This is also why no separate "Rich `markup=False` for a `[`-bearing name"
  test is needed: a name containing brackets is rejected earlier, at the
  allow-list.)

#### Amendment (2026-07-28): end-anchor corrected from `$` to `\Z`

**This ADR originally recorded the grammar as `^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`,
and that is what shipped in `d419c82`.** The line above now quotes `\Z` because
that is what is enforced today; this note exists so the change is visible rather
than silently rewritten into the record.

In Python, `$` matches at end of string **or just before a trailing newline**.
`^...$` therefore accepted exactly one `"\n"` at the end of an otherwise-valid
identifier — `_accepted_identifier("partgraph-dgraph\n")` returned the string
including the newline — even though the charset itself never lists `\n`. The
embedded-newline case (`"bad\nname"`) never exposed this, because the characters
*after* an embedded newline must still satisfy the pattern and do not. `\Z`
matches end of string only.

**Only the anchor changed; the accepted charset is identical.** That was checked
rather than assumed: a differential over 60,088 generated strings (including
`\n \r \t \x00 ; | & $ < >`) found every divergence between the old and new
pattern to be a string ending in a newline, and no other difference.

Every `$`-anchored allow-list in `src/` carried the same quirk and was corrected
together — ten patterns across six modules. Four of them are quoted in other
ADRs (ADR-0010, ADR-0011, ADR-0016, ADR-0017), each of which now points back
here.

**What this did, and what it did not, enable** — stated precisely, because the
two are easy to conflate:

- **Real, for this module:** an accepted name or ID carrying a trailing newline
  reached rendered diagnostic output. `markup=False` suppresses Rich markup but
  does not strip control characters, so the newline printed as a genuine line
  break in output an operator is entitled to treat as trustworthy. No argv
  injection followed from it — a newline is not a shell metacharacter to
  `subprocess` with `shell=False` — but the module's stated promise about what
  reaches a rendered message did not hold.
- **Not exploitable, in the SQLite adapter:** `_SAFE_IDENTIFIER_RE`
  (`partgraph.sources.jlcparts`) had the identical quirk, and it guards SQL
  identifier interpolation — but a column named `lcsc\n` could never reach
  `_build_query`, because the query is built from the **intersection** of
  database-derived column names with a hard-coded literal set, and
  `"lcsc\n" != "lcsc"`. That was traced end to end against a real hostile
  SQLite database, not reasoned about. It was defence in depth with a hole in
  it, not a live injection path, and it should not be cited as one.
- The systemd unit name is a **frozen constant**, never built from an
  engine-returned string, so a poisoned container name can never influence which
  unit gets stopped.
- `systemctl` is always invoked as a list argv with `shell=False`, always with
  `--user`, never with `--system` and never under `sudo`.
- **Every** subprocess call carries a finite, named timeout
  (`ENUMERATE_TIMEOUT_S`, `INSPECT_TIMEOUT_S`, `STOP_TIMEOUT_S`,
  `SYSTEMCTL_TIMEOUT_S`), and the Compose call now carries one too. `ps` stdout
  is additionally bounded by `MAX_PS_OUTPUT_BYTES` **before** it is decoded, so
  a wedged or hostile engine cannot make `db down` spend unbounded time or
  memory inside `json.loads`. This extends ADR-0007's bounded-constant
  precedent from time to output size.

### 9. `DownResult` deliberately carries no `message` field

`HealthResult` (ADR-0018) and `IndexIntegrityResult` (ADR-0019) each carry a
ready-to-print `message`, and their CLI commands print it verbatim. `DownResult`
does **not**, on purpose.

`db down` has several possible outcomes that compose from the *same* structured
data — the stopped set, the survivor set, the report-only port holders, the
health advisory, and the dry-run rendering of all of them. Giving the leaf a
`message` would split that command's user-facing text across two modules and
force the leaf to guess which outcome the CLI intends to render. Instead
`DownResult` carries only structured fields
(`stopped`, `skipped_foreign_port_holders`, `unit_stopped`, `survivors`,
`still_serving_health`, all display **names**, never IDs) and **all** of `db
down`'s text lives in `partgraph.cli`. `db status`/`db check-index` keep their
existing split; this ADR does not retrofit them.

## BREAKING CHANGES

Three deliberate, enumerated breaking changes to the `db down` contract:

- **(a) stdout shape.** Was raw Compose pass-through (whatever `compose down`
  printed, forwarded verbatim). It is now a **structured, single-line summary**
  composed by PartGraph — for example
  `Dgraph stopped. Also stopped outside Compose: partgraph-dgraph. The data
  volume is preserved.` Compose's own stdout is still forwarded, but it is no
  longer the whole story and must not be parsed as such.
- **(b) exit code is now meaningful.** Was Compose's return code, i.e. "did the
  Compose call succeed". It is now **0 iff no PartGraph instance survives the
  sweep AND every enumerated container's ownership could be verified**, else
  **1** — a container that Compose was perfectly happy about but that is still
  running now exits non-zero, and so does one whose ownership `container
  inspect` could not determine during verification (§ 2a). The two are reported
  by textually distinct messages ("still running" vs. "could not verify") so a
  script can tell "it failed" from "it cannot say". `--dry-run` always exits 0.
- **(c) new processes may be invoked.** `db down` may now run
  `systemctl --user show`/`stop` and a direct engine `stop`, in addition to the
  Compose call. On a host with no `systemd --user` session, `systemctl` is
  simply never invoked (no error, the sweep continues).

Also new: the `--dry-run` flag, which performs **only** the read-only steps —
the unit query, the two enumerations and the health probe — mutates nothing, and
always exits 0. It **reports** every one of those observations, including
whether the health endpoint is answering: a preview must not pay for an answer
and then discard it, and "the database is answering right now" is the one signal
that still says something when no container matches any selector at all.

## Relationship to ADR-0009 (extended, not broken)

ADR-0009's engine-detection contract is **EXTENDED, not broken**. Every engine
argv this feature issues still originates from `compose_command()` or
`engine_command()`; no engine name is hard-coded anywhere in `lifecycle.py` (a
source-text scan enforces it), and `PARTGRAPH_CONTAINER_ENGINE` is honoured
identically because detection routes through the same helper. What is new is
only that `db down` now resolves **two** prefixes instead of one: the
compose-plugin prefix from `compose_command()` for phase 2, and the bare engine
prefix from `engine_command()` for the enumeration and stop sweep. Either
resolution failing raises `ContainerEngineError`, and both are caught and
reported exactly the way `db up` already does — one clean stderr `Error` line,
exit 1, no traceback.

`systemctl` is, correctly, a **hard-coded literal**. Detection exists for the
container engine because Docker and Podman are genuinely interchangeable
implementations of one interface, and a host may have either. `systemctl` has no
interchangeable alternative: there is no second binary that speaks the systemd
D-Bus API, and a host either has systemd or does not. The absent case is handled
by a PATH lookup (`shutil.which("systemctl")`), which returns
`UnitState(present=False, …)` **without invoking any subprocess at all** —
not by pretending there is an alternative implementation to detect.

## Relationship to ADR-0018 (supersedes one sentence)

ADR-0018 narrowed ADR-0009 by removing `status` from the Compose-routed trio.
Its "Relationship to ADR-0009" section then stated, at
`docs/decisions/ADR-0018-db-status-health-probe.md:116-117`:

> `db up` and `db down` remain Compose-routed and engine-detected exactly as before — only `status` is now engine-independent.

**That sentence is superseded by this ADR.** It remains true of `db up`. It is
no longer true of `db down`: `db down` is still engine-detected (more so — it
now resolves two prefixes), but it is no longer *only* Compose-routed. It sweeps
every lifecycle owner: the systemd unit, Compose, and finally the engine
directly. The corrected statement is:

> `db up` remains Compose-routed and engine-detected exactly as before.
> `db status` is engine-independent (ADR-0018). `db down` is engine-detected and
> multi-owner: Compose is one of three phases, not the whole command
> (ADR-0021).

Nothing else in ADR-0018 changes; `db status` is untouched by this work.

## Design decisions the acceptance tests did not fully determine

Recorded so a later reader does not mistake them for accidents:

- **Running-state filter.** `ps --all` deliberately lists stopped containers
  too. A container in a state that positively means "not running" (`exited`,
  `created`, `dead`, `removing`, `stopped`, `stopping`, `configured`) is neither
  stopped again nor counted as a survivor — otherwise a long-dead container
  would make `db down` exit 1 forever. The check is a **deny**-list rather than
  an allow-list of running states on purpose: an *unrecognized* state on a
  container already known to be PartGraph's degrades toward stopping it and,
  if it then persists, toward a loud non-zero exit — never toward a silent
  "everything is down" while something may still be serving.
- **Absorbed failures are logged, not silent.** Phase-1 failures and non-zero
  engine `stop` exits are recorded through `logging` (single-line, path-free,
  carrying no engine-derived string — only the frozen unit name and integer exit
  codes). This is the only reporting channel available to a leaf that, by
  Section 9, must not print; without it, absorbing the failure would be
  swallowing it.
- **Compose calls are now bounded too.** `_run_compose` gained a finite
  `timeout` (1800 s for `db up`, which may pull the image; 300 s for `db down`,
  which only stops what exists) and converts a timeout into the same clean
  `Error` line + exit 1 as its other failures, instead of a traceback.
- **`stopped` means "stopped and verified gone".** A container whose `stop`
  returned 0 but that is still running at verification is reported as a
  survivor, not as stopped.

## Migration

- **Scripts that parsed `db down`'s stdout** must stop doing so; it is now a
  PartGraph-composed summary. Call `<engine> compose -f docker/docker-compose.yml
  down` directly to get the old raw behaviour.
- **Scripts that ignored `db down`'s exit code** should now check it. A non-zero
  exit means either that a PartGraph instance is *still running* or that a
  container's ownership *could not be verified* — both previously went
  unreported as a plain exit 0. The two are distinguishable by message, but a
  script that only branches on the code should treat both as "the database may
  still be up"; re-running `db down` is safe and idempotent.
- **Hosts carrying a `partgraph-dgraph.service` quadlet unit**: `db down` now
  stops that unit as well. It does **not** disable it — a unit with
  `WantedBy=default.target` will still start again at the next login. Removing
  or disabling that unit is an explicit operator decision and is out of scope
  here; `db down --dry-run` shows what the sweep sees without changing anything.
- Verified at authoring time (`grep` across the repo): no in-repo shell script,
  systemd unit, cron/wrapper (ADR-0014's scheduling artifacts) or CI workflow
  parses `db down` stdout or branches on its exit code, so nothing in this
  repository breaks.

## Verification

Specified test-first, all hermetic — no test opens a socket, starts a container,
sleeps, or reads the wall clock. Every subprocess outcome is injected through a
**stateful** `subprocess.run` fake whose in-memory container set genuinely
shrinks when a `stop` succeeds, so "was stopped" and "survived the stop" are
distinguishable rather than a static snapshot.

`tests/unit/test_lifecycle.py` (leaf contract) pins: the frozen constants and
their compose-file agreement; finite positive timeouts, `STOP_GRACE_SECONDS ==
60`, and a watchdog margin `STOP_TIMEOUT_S - STOP_GRACE_SECONDS >= 10.0` (a
bare "greater than" would not do); that the engine stop argv's `-t` value is
read from the constant rather than written as a literal, and that the
`systemctl` argv never carries it (§ 7a's honesty boundary, mechanically
pinned); a positive `MAX_PS_OUTPUT_BYTES` and
oversized-output rejection *without* decoding; frozen dataclass field sets;
identical parsing of the JSON-array and NDJSON envelopes; per-row degradation
for missing `Names`, empty `Names`, non-dict elements and missing `Id`; S1
exactness against a `-backup` name, a leading space and a case variation; S2
exactness against prefix and suffix volume collisions; S3 report-only; the
positive name allow-list (accepting a normal `my_service-01.local`, rejecting
newline/backtick/pipe/bracket/300-char names); the absence of
`--filter`/`--ignore`/Go-template argv; engine-prefix resolution through
`engine_command()` including `PARTGRAPH_CONTAINER_ENGINE`; `systemctl --user`
argv shape (no `sudo`, no `--system`); `stop` targeting the ID while the name
still surfaces for display; the end-to-end stopped-vs-survivor distinction;
`compose_down` being keyword-only with no default; and its exception propagating
unmodified while short-circuiting phases 3 and 4.

The highest-priority negative test replays the **real observed host state**
(`min-web`, `cve-ratel`, `cve-zero`, `cve-alpha`, `cve-loader`) and asserts the
selector returns **nothing**, that no cve-graph name or volume ever reaches a
subprocess argv, and that no `stop` is issued.

`tests/unit/test_lifecycle_architecture.py` mechanically enforces the leaf
rules by scanning the source text: no hard-coded `docker`/`podman` argv literal,
no import of `partgraph.cli`/`embed`/`query`/`load` (eager **or** lazy — the
grammar-level scan catches both), and no re-export from `partgraph.util`.

`tests/unit/test_cli_db_down.py` drives the **real** `stop_all()` end-to-end
through the CLI (patching only `subprocess.run`, `shutil.which` and cli.py's own
`compose_command`/`engine_command`/`probe_health`) and pins A1-A16: zero
instances issue no stop at all; a single Compose-owned instance is removed by
one `-v`-free `compose down`; an active unit is stopped by its frozen name; the
exact four-phase call order; two matching containers each stopped exactly once
by ID; the cve-graph fixture never named in any argv; no volume-destroying flag
in any scenario; survivor -> exit 1 with a path-free line naming it; a clean
sweep whose health port still answers -> exit 0 plus exactly one advisory line;
no `systemctl` on PATH -> the phase is skipped silently; a not-found unit ->
`show` but never `stop`; a failing or timing-out `systemctl stop` -> compose and
the sweep still execute; both `compose_command()` and `engine_command()` raising
`ContainerEngineError` -> one clean `Error`, exit 1, no traceback; a hostile
container name never reaching any argv; every call carrying a bounded timeout
and a hung `ps` exiting cleanly instead of hanging; and `--dry-run` mutating
nothing while printing both the would-stop set and the report-only set.

`tests/unit/test_docker_compose.py` pins the compose side of § 7a: the `dgraph`
service must declare a `stop_grace_period` that normalises to
`STOP_GRACE_SECONDS`, which it **imports** rather than re-declaring, so the
Compose budget and the engine-sweep budget cannot drift apart.

`tests/integration/test_gate_pr7.py` (marked `integration`) re-derives the
must-not-touch set from a live read-only scan and asserts a real `db down`
leaves zero PartGraph-owned instances while every foreign container keeps its
container ID.

`ruff check .` is clean and the full non-integration suite is green.
