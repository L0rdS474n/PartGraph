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

### 2. Selector policy S1 / S2 / S3 (locked)

Ownership is decided **in Python, by exact string comparison, over a full
enumeration**:

| Tag    | Selector                                                                | Action                      |
| ------ | ----------------------------------------------------------------------- | --------------------------- |
| **S1** | container name is EXACTLY `partgraph-dgraph`                            | stop                        |
| **S2** | container mounts the named volume EXACTLY `partgraph_dgraph_data`        | stop                        |
| **S3** | holds host port `8081`/`9081`/`8001` but matches neither S1 nor S2       | **REPORT ONLY, never stop** |

Anything matching none of the three is not represented at all — it never appears
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
**both**. The **row shape** differs too — Podman reports `Id` and a `Names`
list, Docker reports `ID` and a single comma-joined `Names` string — so both
spellings are accepted, and the docker-shaped name string is split **before**
the allow-list runs, never after, so a separator can never smuggle a second
value past validation. Accepting both is what makes
`PARTGRAPH_CONTAINER_ENGINE=docker` route through this module genuinely rather
than nominally.

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

### 8. Untrusted engine output is validated at the boundary

- Container names **and** IDs must match a **positive allow-list** — the
  Docker/podman grammar `^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`, plus a finite length
  bound — before they are used for anything. A deny-list of hostile characters
  could never be exhaustive. A rejected row is excluded before classification,
  so its raw text reaches neither a subprocess argv nor a rendered message.
  (This is also why no separate "Rich `markup=False` for a `[`-bearing name"
  test is needed: a name containing brackets is rejected earlier, at the
  allow-list.)
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
  sweep**, else **1** — a container that Compose was perfectly happy about but
  that is still running now exits non-zero. `--dry-run` always exits 0.
- **(c) new processes may be invoked.** `db down` may now run
  `systemctl --user show`/`stop` and a direct engine `stop`, in addition to the
  Compose call. On a host with no `systemd --user` session, `systemctl` is
  simply never invoked (no error, the sweep continues).

Also new: the `--dry-run` flag, which performs **only** the read-only unit query
and the two enumerations, mutates nothing, and always exits 0.

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
- **Scripts that ignored `db down`'s exit code** should now check it: a non-zero
  exit means a PartGraph instance is *still running*, which previously went
  unreported.
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
their compose-file agreement; finite positive timeouts and
`STOP_TIMEOUT_S > STOP_GRACE_SECONDS`; a positive `MAX_PS_OUTPUT_BYTES` and
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

`tests/integration/test_gate_pr7.py` (marked `integration`) re-derives the
must-not-touch set from a live read-only scan and asserts a real `db down`
leaves zero PartGraph-owned instances while every foreign container keeps its
container ID.

`ruff check .` is clean and the full non-integration suite is green.
