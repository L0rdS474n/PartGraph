# ADR-0025: Two runtime dependencies are bounded; the other five stay bare

- Status: Accepted
- Date: 2026-07-29

## Context

All seven entries in `[project.dependencies]` were unbounded. The one bound
anywhere in `pyproject.toml` was `ruff==0.16.0`, in the dev extra, and it was
not put there as policy — it was put there after the fact:

> A lint gate whose ruleset moves under it is not a gate: ruff 0.16.0 promoted
> `PLR0917` out of preview and CI began failing on code that had been on main,
> unchanged and green, for months.

That is the whole argument this ADR generalises, and it is worth stating in its
sharpest form: **the dependency did not break. It changed, correctly, and the
repository's behaviour changed with it, without anyone deciding that.** The cost
was a red CI on untouched code — loud, cheap, and paid once. The question this
ADR answers is which of the seven runtime dependencies could do the same thing
*quietly*, where the output is a wrong answer rather than a red gate.

The wrong framing — the one deliberately rejected here — is "runtime dependency,
therefore pin it." That produces seven bounds, six of which nobody can justify
when they later block an upgrade, and all of which will eventually be deleted in
a single frustrated commit by whoever is fighting the resolver at the time. A
bound is a claim about a specific semantic. It earns its place by naming that
semantic, or it does not belong in the file.

So the sorting question is not "does the code import this?" but "does the code
**decide** something using a behaviour of this library that a version change
could alter without failing loudly?" Two of the seven do.

## Decision

### 1. `psutil>=7.1.0,<8` — kept, but NOT for the reason first given

> **The floor does not protect `db idle-stop`.** Three earlier drafts of this
> ADR said it did. It does not. The property they claimed — that flooring at
> 7.1.0 makes the lease's `create_time` comparison stable across a system clock
> step — is **false**, and the risk it was supposed to close is still open. See
> "Open risk: a clock step can still make a live lease read as dead" below,
> which is now the operative section for this failure mode. The floor is
> retained on a much narrower argument, given at the end of this section.

`db idle-stop` (ADR-0023) decides whether to stop a running database. It must
not stop one that is in use, so it establishes whether a lease's owning process
is genuinely alive from `(pid, create_time)` — the technique psutil documents
for defeating PID recycling. A lease is cleaned only on a *confirmed* dead
process: a clean `NoSuchProcess`, or a live PID whose `create_time` differs from
the recorded one.

**The comparison is ours, not psutil's.** psutil does perform a reuse check of
its own, but only in `ppid()`, `children()`, `connections()`, `nice()` (with the
other set methods) and the signal methods `send_signal()`, `suspend()`,
`resume()`, `terminate()`, `kill()` — thirteen call sites of
`_raise_if_pid_reused()` in the installed 7.2.2, enumerated directly from its
source. `create_time()` is **not** among them and performs no reuse check. This
repository calls none of the checking methods either: a grep of `src/` for all
of them returns nothing, because a stop always routes through the container
engine, never through a signal.

(An earlier draft said the check runs "only inside signal and set methods".
`ppid()` and `children()` also check. The conclusion is unchanged — we call
neither — but the statement was inaccurate.)

What `src/partgraph/util/activity.py` actually does is call `create_time()` and
compare the float itself:

```python
create_time = float(psutil_module.Process(lease.pid).create_time())
...
if math.isclose(create_time, lease.create_time, rel_tol=0.0,
                abs_tol=_CREATE_TIME_TOLERANCE_S):
    return _LEASE_LIVE
return _LEASE_DEAD
```

(`activity.py:665-674`; the `math.isclose` block is `670-673`.) The mismatch
branch is ours. We are not inheriting psutil's guarded behaviour; we are
re-implementing the guard on top of a primitive that does not have the property
psutil's own guard relies on.

#### Why 7.1.0 does not buy what it appeared to

psutil's 7.1.0 fix made the identity monotonic. It did so on `Process._ident`,
whose second element on Linux/macOS/NetBSD is the process start time **in
seconds since boot** — a value that does not move when the wall clock does.
That is what `is_running()` and the reuse check compare.

The **public** `create_time()`, the one `activity.py` calls, is a different
value. From the installed psutil 7.2.2's own Linux backend:

```python
def create_time(self, monotonic=False):
    # The 'starttime' field in /proc/[pid]/stat is expressed in jiffies ...
    # It never changes and is unaffected by system clock updates.
    if self._ctime is None:
        self._ctime = float(self._parse_stat_file()['create_time']) / CLOCK_TICKS
    if monotonic:
        return self._ctime
    # Add the boot time, returning time expressed in seconds since
    # the epoch. This is subject to system clock updates.
    return self._ctime + boot_time()
```

`boot_time()` re-reads `btime` from `/proc/stat` on every call, and psutil
documents both halves explicitly — `boot_time()`'s docstring and
`Process.create_time()`'s both say the value "is based on the system clock,
which means it may be affected by changes such as manual adjustments or time
synchronization (e.g. NTP)". That text is in the **installed 7.2.2**, after the
"fix".

Measured on this machine, against the installed 7.2.2:

```
psutil 7.2.2
public create_time()  : 1785321851.65
_ident                : (726997, 13539.65)
_ident[1] == public   : False
public - _ident[1]    = 1785308312.0
boot_time()           = 1785308312.0
match boot_time       : True
```

The public value is exactly the monotonic value plus `boot_time()`. So the
monotonic fix never reaches the number this repository persists and compares.
A clock step moves `boot_time()`, moves `create_time()`, and breaks the
stored-versus-live comparison — by whole seconds, against a 1 ms tolerance.

#### Why the floor is kept anyway

Not for `db idle-stop`. It is kept because:

- it is **harmless** — 7.1.0 is nearly two years of releases below the installed
  7.2.2, and nothing in the repository needs an older psutil;
- it keeps the project off releases with a **known identity bug on adjacent
  paths**: `is_running()` and the signal-path reuse check were genuinely wrong
  before 7.1.0, and while nothing here calls them today, a future caller
  reaching for `is_running()` on a pre-7.1.0 psutil would inherit that bug
  silently;
- removing it now would be a second change riding along in a correction.

That is a real but modest justification, and it is deliberately weaker than the
one this section used to make. **[PROBABLE]**, not [CONFIRMED] — and it is not
load-bearing for the stop decision. Anyone who needs the stop decision to be
clock-safe must read the open-risk section, not this bound.

#### What the changelog entries actually say, and what they do not

The whole scheme rests on `create_time()` being a stable identifier for the
lifetime of a process. The changelog entries below are the ones this ADR was
built on; they are quoted accurately, and the error was in what was inferred
from them — read closely, the first is explicitly about `is_running()`. The fix
shipped in **one release**, `7.1.0` (2025-09-17), whose section carries both of
these entries:

- "[Linux]: `Process.create_time()` now uses a monotonic clock, preventing
  `Process.is_running()` from returning wrong results after system clock
  updates." (#2526)
- "[Linux], [macOS], [NetBSD]: `Process.create_time()` does not reflect system
  clock updates." (#2541, #2570, #2578)

All four issue numbers fall between the `7.1.0` and `7.0.0` section headers.
The three releases that follow contain no `create_time` entry at all: `7.1.1`
(2025-10-19) is SunOS-only; `7.1.2` (2025-10-25) and `7.1.3` (2025-11-02) carry
macOS/BSD `ZombieProcess`/`NoSuchProcess` fixes (#2650, #2672) — neither on
Linux, this repository's actual target platform, nor about `create_time`.

**The second entry is the one that misled three drafts.** "`Process.create_time()`
does not reflect system clock updates" reads, in isolation, as a statement about
the public method this repository calls. It is not: the change is to the
monotonic value behind `_ident`, and the public method still adds `boot_time()`
on top, as the source quoted above shows. The entry is not wrong — it is
describing the fix's subject, not its full public surface — but it cannot carry
the weight this ADR put on it. Reading a changelog entry and reading the code it
refers to are different acts, and only the second one settles a question like
this.

The failure mode the drafts described was real and correctly characterised: the
machine's clock is corrected, and a database is stopped underneath a running
ingest, with no exception, no red gate and no log line — the stop looks exactly
like a correct one. What was wrong was the claim that a version floor closes it.
It does not. That failure mode is still live, and is documented as an open risk
below rather than as history.

psutil's own FAQ corroborates the fragility from a second, independent angle,
verbatim:

> On FreeBSD, OpenBSD, SunOS and AIX the PID reuse check is disabled, and
> process identity is based on the PID alone. That's because on these platforms
> the process creation time is not stable across system clock updates (e.g.
> NTP), which previously caused false `NoSuchProcess` exceptions for processes
> which were still alive.

psutil's authors know the primitive is clock-dependent, disable their own check
where it cannot be trusted, and name the resulting failure as a **false**
`NoSuchProcess` for a live process — the same false-dead direction, reached the
same way. That is why this passage is quoted here: not as evidence about our
own code path, but as upstream confirming that a clock-derived creation time is
not a safe identity.

**This passage describes `8.0.0`, not the pinned `7.x` range.** Under the
installed 7.2.2 the reuse check is *not* disabled on those platforms. So it is
corroboration of the underlying fragility and a description of where upstream is
going — not a statement about the version this project resolves today.

**Two different platform lists appear in this ADR and they must not be merged.**
The FAQ's disabled-check list is FreeBSD, OpenBSD, SunOS and AIX — **NetBSD is
not in it**. NetBSD appears in the `7.1.0` changelog entry above, which is a
different statement about a different thing (which platforms received the fix,
not which platforms have the check switched off). An earlier draft of this ADR
blurred them into "several BSDs"; that phrasing was wrong twice over, since
SunOS and AIX are not BSDs and NetBSD was not on the list.

**Scope, stated plainly:** on those four platforms `create_time` is documented
as not NTP-stable, so the manual comparison in `activity.py` would be
unreliable there *at any psutil version* — no floor fixes that. This repository
targets Linux and has never claimed otherwise, so this is a limit on where the
lease check is meaningful, not a defect in it. It is written down because
"psutil handles it" would be the wrong thing for a future reader to assume on a
BSD host. Note that the open risk below shows the comparison is not fully
reliable on Linux either; the difference is one of degree — on those four
platforms it is unreliable by upstream's own account, here it is exposed only
to an actual clock step.

`7.1.0` is the floor rather than a later patch — the release the fix shipped in,
no later and no earlier. Given the section above, the choice of floor matters
much less than it appeared to: the floor is not load-bearing for the stop
decision at any value.

#### Correction: this floor was first set at `7.1.3`, on a misreading

The first version of this bound said `psutil>=7.1.3`, and this ADR argued for it
as "the last release in a fix cluster spanning 7.1.0–7.1.3". **There is no such
cluster.** The `7.1.0` section's bug-fix list is long and densely
cross-referenced, and #2541/#2570/#2578 were misattributed to the three
following patch releases purely because their issue numbers sit far down that
list. That claim was wrong, and it was wrong in a specific, checkable way: it
rejected psutil 7.1.0, 7.1.1 and 7.1.2 for carrying a bug they do not carry.

It was caught in review before merge, and re-verified twice independently
against fresh fetches of the changelog — once by asking for each of the four
issue numbers directly, once by reading the section boundaries line by line and
confirming all four fall strictly inside `7.1.0`'s own section. `7.1.1` through
`7.1.3` were then each re-read in full looking for *any* independent reason to
floor higher. None was found, so none is claimed.

This is recorded rather than silently amended for two reasons. First, a bound is
only as good as the citation under it, and a reader who later checks this ADR
against the changelog and finds a discrepancy should be able to tell the
difference between "the ADR is stale" and "the ADR was never right" — here, the
answer is written down. Second, the failure mode is worth naming: the error was
not sloppiness about *whether* a fix existed, but about *where the section ended*
— a class of misreading that produces confident, well-formed, specific citations
that happen to be false. Over-flooring is the benign direction (it excludes
working releases rather than admitting broken ones), which is exactly why it can
survive review unless someone re-fetches the source. Re-fetch the source.

The ceiling, `<8`, is weaker evidence and is recorded as such: psutil's changelog
opens its `8.0.0` section with "psutil 8.0 introduces breaking API changes. See
the migration guide if upgrading from 7.x." That is a direct textual statement,
not an inference, and it is the **entire** basis for the ceiling. Nothing
narrower supports it.

Specifically — and this is the second correction in this ADR, see § 6 — what can
be said about `8.0.0`'s contents is only this: **on Linux, and outside the
section's Compatibility notes, it touches none of `create_time`, `is_running` or
`NoSuchProcess`.** `create_time` and `is_running` do not appear in the section at
all. `NoSuchProcess` appears **eight times**, in BSD and Windows bug fixes, two
of which are directly about the mechanism the lease check depends on:

> #2888 [FreeBSD], [OpenBSD]: `Process` methods could wrongly raise
> `NoSuchProcess` ("PID has been reused") for a process still alive, after a
> system clock update (e.g. NTP).

> #2895: `Process` methods could wrongly raise `NoSuchProcess` ("PID has been
> reused") when the process creation time could not be determined, e.g. for
> zombies on NetBSD / OpenBSD or on `AccessDenied` on Windows.

**The two are about different things, and only #2888 is the clock case.**
#2888 is the clock-step failure (FreeBSD/OpenBSD, wrongly raising for a process
still alive after a clock update). #2895 is a different failure — an
*undeterminable* creation time (zombies on NetBSD/OpenBSD, `AccessDenied` on
Windows) — and has nothing to do with a clock update. An earlier draft of this
ADR described both as the clock case.

**Both nonetheless point our way.** They make psutil raise `NoSuchProcess`
*less* often on a false positive — and a spurious `NoSuchProcess` is exactly a
false "dead", the direction `activity.py` calls unsafe because it lets a stop
through while work is in flight. So these entries are an argument *for* 8.0, not
against it. No listed platform is this repository's target, so neither changes
anything today; but they must not be cited as ceiling evidence, because they are
the opposite.

The ceiling is therefore not a claim that 8.0 breaks this repository — it is
narrower than it may look. It is a refusal to let a major version whose author
says "breaking" resolve implicitly into a code path that decides whether to kill
a running database. It should be lifted by reading the migration guide, which is
a small, bounded piece of work, not by deleting the line — and § 6's row on this
claim should be read first by whoever does.

#### Open risk: a clock step can still make a live lease read as dead

This is an **open gap in merged code**, not a limitation, not a known-issue
footnote, and not something the psutil bound closes. It is written here as a
defect awaiting a fix.

**The failure.** `db idle-stop` can decide a lease's owning process is dead
while that process is alive and working, and stop the database underneath it.

**The trigger.** A system clock step — an NTP correction, a manual `date` set,
a resume from suspend on a host whose clock drifted — occurring between the
moment a lease is written and the moment it is evaluated. No unusual
configuration is required; NTP corrections are routine.

**The mechanism.** `acquire_lease` persists the *epoch-form* `create_time()`.
`_lease_status` later re-reads `create_time()` and compares. Both values are
`monotonic_start + boot_time()`, and `boot_time()` is re-derived from the wall
clock on every call, so a clock step of Δ seconds moves the second reading by Δ
while the stored one stays put. `_CREATE_TIME_TOLERANCE_S` is `1e-3`. Any step
larger than a millisecond — every real one — makes `math.isclose` fail, and the
mismatch branch returns `_LEASE_DEAD`.

**Blast radius.** Bounded but real:

- Only `db idle-stop` consults leases, so only the idle-stop path can act on the
  wrong answer. Ordinary commands are unaffected.
- The wrong answer is in the **unsafe** direction by `activity.py`'s own
  framing: a false "dead" lets a stop through while work is in flight, whereas a
  false "live" would only postpone a stop.
- The observable consequence is a database stopped under a running ingest,
  embed or refresh — with no exception, no log line and no distinguishing
  signal, because a spurious mismatch is indistinguishable from a genuine
  recycled PID.
- A single unlucky lease is enough; there is no quorum or retry that would mask
  it.
- Frequency is low: it needs a clock step to land inside a specific window. Low
  frequency is not the same as low severity, and "rare and silent" is the
  combination that survives longest in production.

**The shape of the fix — in our code, not in a bound.** Persist and compare
`create_time() - boot_time()`: the seconds-since-boot form, which is the same
quantity psutil's own `_ident` uses and the reason `_ident` is clock-stable. On
Linux this recovers `_ctime` exactly, so the comparison becomes immune to wall
clock movement. Sketch only:

```python
# NOT the current code — the shape of the intended fix.
start_since_boot = psutil.Process(pid).create_time() - psutil.boot_time()
```

Points a real change must settle, none of which are decided here: existing
persisted leases carry epoch-form values and would have to be migrated or
invalidated; `boot_time()` itself is not free of edge cases across suspend on
some kernels; the tolerance may want revisiting once the quantity being compared
changes; and the private `_ident` must not be reached into, since it is not
public API. A behavioural test that steps a simulated clock and asserts the
lease still reads live is the acceptance criterion.

**Why it is not fixed here.** This change's objective is dependency bounds. The
fix touches merged runtime behaviour on the stop path, needs its own tests and
its own migration decision for existing lease files, and would land unreviewed
as a rider on a manifest change. It is deferred deliberately and visibly — with
the gap named in `activity.py`'s own `Lease` docstring, where the next reader of
that field will meet it — rather than left implied.

### 2. `pydgraph>=25.2.0,<26` — [PROBABLE], structural coupling, no observed break

pydgraph's major version tracks the Dgraph **server's** release line. Its
CHANGELOG shows `v25.0.0` shipping "Updated proto definitions to support Dgraph
v25 API", and `v25.1.0` marking `DgraphClientStub.from_cloud()` /
`.parse_host()` deprecated with "removal planned for 26.0.0".

`docker/docker-compose.yml` pins the server image to `dgraph/standalone:v25.3.4`
(verified). An unbounded client is therefore free to resolve a major line ahead
of the server it must talk to, and nothing in this repository would notice at
install time.

**The evidence here is a deprecation notice and a numbering convention, not an
incident.** No shipped pydgraph release is documented as having broken this
repository's usage, and none was observed doing so during this analysis. This is
weaker than the case for psutil's *ceiling*, and — after § 1's open-risk
correction — comparable to the case for psutil's floor, which turned out to
defend much less than it claimed. The two bounds are still maintained
differently: the pydgraph ceiling is a pairing constraint and should simply move
when `docker/docker-compose.yml` moves to a v26 server.

pydgraph's *other* documented risk — the default gRPC message-size ceiling
(ADR-0010) — needs no version bound, because it is already neutralised at the
call site: `src/partgraph/cli.py` builds the stub with explicit
`grpc.max_receive_message_length` / `grpc.max_send_message_length` options
(verified) and never relies on pydgraph's default. Only the server-generation
coupling remains live.

### 3. The five that stay bare, each for a stated reason

Leaving a dependency unbounded is a decision too, and it is made per dependency
here rather than by default.

- **`requests`** — bare, and deliberately so, on two counts. First, the
  behaviour actually relied upon (a float `timeout` splitting into connect and
  read, which ADR-0022 depends on; and the `Timeout`-before-`RequestException`
  catch order in `src/partgraph/util/health.py` and
  `src/partgraph/util/index_health.py`) traces to 2.4.0 in 2014 and has not
  regressed since. Second, `requests` ships CVE fixes on a cadence this
  repository wants uninterrupted. Pinning it would trade a real, recurring
  security benefit for a risk never observed. The behaviour is pinned the
  stronger way instead — as a property, against the real installed library, in
  `tests/unit/test_requests_timeout_semantics_real.py`, driven through the real
  adapter over a genuine loopback socket. A test that fails when the semantic
  changes beats a bound that blocks upgrades in the hope it might.
- **`httpx`** — its one famously version-sensitive default (the
  `allow_redirects` / `follow_redirects` flip) is already neutralised by never
  relying on it: both call sites pass the flag explicitly
  (`src/partgraph/ingest/fetch.py:144` `follow_redirects=True`;
  `src/partgraph/cli.py:3017` `follow_redirects=False`, both verified). No code
  catches a specific `httpx.*` exception class, so the exception hierarchy is
  not load-bearing either. Nothing left to bind.
- **`typer`** — the used surface (`typer.Typer`, `typer.Option`, `typer.Exit`)
  is exercised by the entire CLI suite on every run. A breaking change fails at
  collection or invocation. Loud, not silent.
- **`rich`** — presentation only: progress bars and tables. A break here is
  cosmetic and visible, never a wrong answer.
- **`pyyaml`** — see the open item below. Bare here because its version is not
  the interesting question about it.

These five are held bare by a test
(`tests/unit/test_pyproject_dependency_pins.py`), which makes the absence of a
bound a ratchet rather than an oversight: adding one turns the suite red and
forces whoever adds it to write down why, here or in a follow-up ADR.

### 4. No lockfile

The tempting next step is a lockfile, and it is the wrong one for this
repository right now. The real surface this analysis found is **two**
dependencies with version-sensitive semantics. A lockfile does not express that;
it blanket-pins all seven, including `requests` — cutting off exactly the CVE
patches § 3 keeps deliberately flowing — and it moves the reasoning out of the
file a human reads and into a generated artefact nobody reviews.

Two bounds with an argument attached to each are more honest, and more
maintainable, than seven with none. This is a scope decision, not a claim that
lockfiles are wrong: if this repository ever ships a reproducible deployment
artefact, a lockfile becomes the right tool and this section should be revisited
rather than cited.

### 5. The reason lives at the bound

Each bound carries its evidence in a comment on the line itself in
`pyproject.toml`, in the same shape as the `ruff` pin's. This is not decoration.
A bound with no reason at the bound is indistinguishable from cargo-cult, and
the next person to hit a resolver conflict will delete it in thirty seconds —
correctly, on the information available to them. The ADR is the long form; the
comment is what is actually present at the moment someone is deciding whether
the line may go.

### 6. Provenance: which sentences here carry weight, and why

Every bound in this ADR rests on a claim about an **upstream** document nobody
in this repository controls. **Four** of those claims have now been corrected
after the fact — two from psutil's changelog, one from its FAQ, and one that
voided the psutil floor's entire justification (§ 1). The first three were
caught by re-fetching the source. The fourth could not have been: every quote
was accurate, and only reading the installed library's source and measuring it
settled the question. So the sourcing is stated explicitly rather than left
uniform, and a reader should weight the sentences accordingly:

| Claim | How it was established |
| --- | --- |
| psutil changelog: all four issues inside the single `7.1.0` section; `7.1.1`–`7.1.3` contain no `create_time` entry | Fetched from psutil's published changelog and re-checked **twice independently** — once per-issue by number, once by reading section boundaries line by line — after the first reading proved wrong (§ 1) |
| psutil changelog: `7.1.2`/`7.1.3`'s zombie fixes are #2650 and #2672 | **Double-sourced** — re-fetched raw by the reviewer and confirmed. This row is NOT status-quo-safe, contrary to an earlier note here: it is what selects 7.1.0 over 7.1.3, so an error in it ships a floor that *admits* the releases the floor exists to exclude — the unsafe direction. Re-check it before any floor change |
| psutil source (installed 7.2.2): public `create_time()` returns `_ctime + boot_time()`; `_ident` uses the monotonic `_ctime`; `_raise_if_pid_reused()` has 13 call sites and `create_time()` is not one | **Read directly from the installed package** and **measured on this machine**: `create_time() - _ident[1] == boot_time()` exactly. This is the row that voided § 1's original floor argument |
| psutil changelog: `8.0.0` declares "breaking API changes" | Fetched, and re-confirmed twice |
| psutil changelog: `8.0.0` touches none of `create_time` / `is_running` / `NoSuchProcess` — **FALSE as originally written; now narrowed to "on Linux, and outside Compatibility notes"** | **Re-fetched and CORRECTED.** The original claim came from a *summarised* fetch, whose answer was "none of the listed changes mention create_time, NoSuchProcess or is_running". A **raw** fetch of the same page finds `NoSuchProcess` eight times, including #2888 and #2895 (§ 1). `create_time` and `is_running` are genuinely absent, so two-thirds of the claim held — which is why it read as plausible on review |
| psutil FAQ: the PID-reuse check is disabled on FreeBSD, OpenBSD, SunOS and AIX because `create_time` is not NTP-stable there | **Re-fetched and quoted verbatim.** The pass produced a CORRECTION — the earlier "several BSDs" wrongly included NetBSD (that list is the `7.1.0` changelog's, a different statement) and wrongly called SunOS/AIX BSDs |
| psutil FAQ: PID-reuse checking happens in the checking methods, not in read-only ones like `create_time()` | **Re-fetched**, then **corrected against the installed source**: the check also runs in `ppid()`, `children()` and `connections()`, not "only signal and set methods" as first written. The conclusion is unchanged — `src/` calls none of the thirteen. `activity.py:665-674` does the `math.isclose` comparison itself (`670-673`) |
| pydgraph CHANGELOG: `25.0.0` "v25 API" protos; `25.1.0` deprecates `from_cloud()`/`parse_host()`, "removal planned for 26.0.0" | **Re-fetched and confirmed.** Both lines hold verbatim, including the "(removal planned for 26.0.0)" parenthetical on both `from_cloud()` and `parse_host()`, sync and async |
| `requests`' float-timeout split traces to 2.4.0 (2014) without regression | **Single-sourced, and deliberately left so.** Never independently re-fetched. Being wrong here costs nothing a bound would have caught, because this claim argues for leaving the dependency *bare* — the failure mode of a bad "leave it unpinned" argument is the status quo. The behaviour itself is pinned executably against the real installed library in `tests/unit/test_requests_timeout_semantics_real.py`, driven through the real adapter over a loopback socket, so the property this row is about is verified even though the history behind it is not |
| Compose pins `dgraph/standalone:v25.3.4` | Read directly from `docker/docker-compose.yml` in this repository |
| `pyyaml` has zero `src/` usage; two test consumers | `grep -rn "yaml" src/ --include=*.py` (empty) plus the two importing test files, run in this repository |
| Both `httpx.Client` call sites pass `follow_redirects` explicitly | Read directly: `src/partgraph/ingest/fetch.py:144`, `src/partgraph/cli.py:3017` |
| gRPC message-size options set explicitly | Read directly: `src/partgraph/cli.py:1287-1288` |
| psutil 7.2.2 / pydgraph 25.2.0 installed and satisfying these bounds | Resolved in the real environment via `importlib.metadata` against a real `SpecifierSet` |

**One row remains single-sourced**: `requests`' 2.4.0 history. It genuinely is
status-quo-safe — it argues for leaving a dependency *bare*, so an error in it
leaves the manifest exactly where it already is, and the behaviour itself is
pinned executably regardless.

An earlier version of this paragraph extended that same symmetry argument to the
`#2650`/`#2672` row. **That was unsound.** Those issue numbers are what select
7.1.0 over 7.1.3; an error there does not leave the manifest unchanged, it ships
a *lower* floor than the evidence would support — admitting the very releases the
floor exists to exclude. That is the unsafe direction, not the neutral one. The
row has since been re-fetched raw and confirmed, so the conclusion stands, but
the reasoning that excused it from checking was wrong and is corrected here.

The lesson generalises: "an error here would only leave things as they are" is
sound for a claim that argues *against* a constraint, and unsound for one that
argues for a *weaker* constraint. The two look alike and are not.

A further caveat about this table itself: the re-fetches recorded here were
performed by the reviewer, not by the author of the surrounding prose, who
transcribed them. That hand-off is precisely the step that produced two of the
three corrections above. The table is written per-claim rather than per-source
so that a reader who doubts a specific quote knows which one to re-pull, and
the repository-local rows at the bottom — the greps, the line references, the
installed-version resolution — are the only ones that were established and
written by the same pass.

One pattern produced the first three corrections: **a summarising layer between
the source and the claim reads as a fetch but is not one.** All three were that
shape. The first flattened a long section's internal structure and scattered
four issue numbers across three releases that never carried them. The second
answered a "does X appear?" question with "no" about a page where X appears
eight times. The third merged two adjacent platform lists into one that matched
neither. None looked like a guess; all three produced specific, well-formed,
checkable citations, which is precisely what let them survive to a bound. A raw
fetch, or a grep of the raw page, caught all three in seconds.

**The fourth was worse, and had a different cause.** The floor's entire
justification — that `psutil>=7.1.0` makes the lease comparison clock-stable —
came from reading a changelog entry correctly and inferring the wrong thing from
it. Every quotation was accurate. "`Process.create_time()` does not reflect
system clock updates" is exactly what upstream wrote. It simply does not mean
what it appears to mean about the public method, and no amount of re-fetching
*the changelog* would have revealed that: the correction came from reading the
installed library's source and measuring its behaviour on this machine. A
citation can be verbatim, re-fetched, double-sourced — and still not support the
claim built on top of it. **For a dependency claim about behaviour, the source of
truth is the installed code and an experiment, not the release notes.** That is
the single most useful sentence in this ADR.

The re-reads also paid for themselves in a way that has nothing to do with
error-correction: the FAQ pass is what surfaced that psutil's PID-reuse check
never runs on our code path — which is what made the fourth error findable at
all, since it is the question that leads directly to "then what exactly are we
comparing?". Re-reading a source you have already cited is not just auditing —
prefer the raw read for anything that ends up as a
version constraint, and read the whole section, not the part you came for.

None of the upstream claims is load-bearing for *correctness today* — the
installed versions are what they are, and their behaviour is pinned by tests
that execute against them. The upstream claims are load-bearing for the
**bounds**, i.e. for which future versions this project will refuse. That is the
right place to be sceptical.

## Consequences

### Install-time

`pip install -e .` now refuses `psutil < 7.1.0` or `>= 8`, and `pydgraph
< 25.2.0` or `>= 26`. The currently installed versions — psutil 7.2.2, pydgraph
25.2.0 — satisfy both bounds and were verified against the real environment via
`importlib.metadata`, not assumed; nothing was installed, upgraded or resolved
to make this change pass.

### Maintenance

- Raising psutil past 8.0 requires reading psutil's migration guide and
  confirming `create_time()`'s contract survives — which now means reading the
  installed source, not the release notes (§ 6).
- The psutil **floor** is not load-bearing for the stop decision (§ 1, "Open
  risk"). Do not treat it as the thing keeping `db idle-stop` correct, and do
  not close the open risk by moving it. Fix the comparison in `activity.py`.
- Raising pydgraph past 26 goes together with the server image in
  `docker/docker-compose.yml`. They move as a pair or not at all.
- Adding a bound to any of the five bare dependencies turns
  `tests/unit/test_pyproject_dependency_pins.py` red on purpose. Argue it there.

### Open item: `pyyaml` is declared in the wrong place

`pyyaml` is declared as a **runtime** dependency and is imported nowhere in
`src/` — `grep -rn "yaml" src/ --include=*.py` returns nothing (verified). Its
only consumers are two test files: `tests/unit/test_ci_workflow.py` and
`tests/unit/test_docker_compose.py`.

So every user installing PartGraph downloads a parser the product never calls,
to satisfy two tests. That is a real finding, and it is recorded rather than
quietly left in place — but it is a question about **where** a dependency is
declared, not about its version.

**Deferred deliberately, and the reason is not squeamishness about scope.**
Removing a declared runtime dependency is a change with a blast radius outside
this repository: anyone installing PartGraph *without* the `dev` extra and
importing `yaml` transitively — which the current manifest entitles them to do —
would break. That is a compatibility decision with its own migration question,
and it deserves its own change and its own tests rather than riding along in a
PR about version bounds. It is not an oversight, and it is not being left
because nobody noticed.

### What this change does not claim

No behaviour of the shipped product changes: no code path, CLI output, schema or
on-disk format is touched, and the only `src/` edit is a docstring. This ADR
constrains what may be *installed*.

It specifically does **not** claim that the psutil floor makes `db idle-stop`
safe against a system clock step. It does not — see "Open risk" in § 1. Three
earlier drafts of this ADR did make that claim, in this section and elsewhere,
and it was wrong. The false-DEAD failure it described is present in merged code
today and is not closed by anything in this change.
