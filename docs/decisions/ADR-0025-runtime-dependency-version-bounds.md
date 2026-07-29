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

### 1. `psutil>=7.1.0,<8` — [CONFIRMED], and the floor is the point

`db idle-stop` (ADR-0023) decides whether to stop a running database. It must
not stop one that is in use, so it establishes whether a lease's owning process
is genuinely alive from `(pid, create_time)` — psutil's own documented technique
for defeating PID recycling, and named as such in `Lease`'s docstring
(`src/partgraph/util/activity.py`). A lease is cleaned only on a *confirmed*
dead process: a clean `NoSuchProcess`, or a live PID whose `create_time` differs
from the recorded one.

The whole scheme rests on `create_time()` being a stable identifier for the
lifetime of a process. psutil's published changelog says it was not, on this
repository's actual target platform, until very recently. The fix shipped in
**one release**, `7.1.0` (2025-09-17), whose section carries both of these
entries:

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

Before `7.1.0`, an ordinary NTP correction could shift `create_time()` for a
process that is still running. The recorded value and the reported value then
disagree, and the code reads that disagreement as *"this PID was recycled; the
original process is dead."* That is precisely the direction
`src/partgraph/util/activity.py`'s own tolerance comment names as unsafe:

> a false "dead" would let a stop through while real work is in flight, while a
> false "live" only postpones a stop.

The failure mode is therefore: the machine's clock is corrected, and a database
is stopped underneath a running ingest. No exception, no red gate, no log line
saying anything was wrong — the stop looks exactly like a correct one. This is
the class of change the `ruff` incident was, minus the part where anyone finds
out.

psutil's own FAQ corroborates the fragility from a second, independent angle:
the `create_time`-based identity check is disabled outright on several BSDs
because `create_time` is not NTP-stable there. The technique is known by its
authors to be clock-dependent, and the Linux fix is recent.

`7.1.0` is the floor: the release the fix actually shipped in, no later and no
earlier.

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
not an inference — but **nothing listed there touches `create_time`,
`is_running` or `NoSuchProcess`** (re-confirmed during the correction above).
The ceiling is not a claim that 8.0 breaks this repository. It is a refusal to
let a major version whose author says "breaking" resolve implicitly into a code
path that decides whether to kill a running database. It should be lifted by
reading the migration guide, which is a small, bounded piece of work, not by
deleting the line.

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
a materially weaker case than psutil's, and the difference is recorded rather
than smoothed over, because the two bounds should be maintained differently: the
psutil floor is defending a known-wrong behaviour and should be treated as
load-bearing; the pydgraph ceiling is a pairing constraint and should simply move
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
in this repository controls, and the § 1 correction happened because one such
claim was repeated confidently without being re-fetched. So the sourcing is
stated explicitly rather than left uniform, and a reader should weight the
sentences accordingly:

| Claim | How it was established |
| --- | --- |
| psutil changelog: all four issues inside the single `7.1.0` section; `7.1.1`–`7.1.3` contain no `create_time` entry | Fetched from psutil's published changelog and re-checked **twice independently** — once per-issue by number, once by reading section boundaries line by line — after the first reading proved wrong (§ 1) |
| psutil changelog: `8.0.0` declares breaking API changes, none touching `create_time` / `is_running` / `NoSuchProcess` | Fetched, and re-confirmed during the same correction pass |
| psutil FAQ: the `create_time` identity check is disabled on several BSDs because it is not NTP-stable there | Reported upstream text, **not re-fetched** during the correction pass |
| pydgraph CHANGELOG: `25.0.0` "v25 API" protos; `25.1.0` deprecates `from_cloud()`/`parse_host()`, "removal planned for 26.0.0" | Reported upstream text, **not independently re-fetched** |
| `requests`' float-timeout split traces to 2.4.0 (2014) without regression | Reported upstream history, **not independently re-fetched** — but the behaviour itself is pinned executably against the installed library in `tests/unit/test_requests_timeout_semantics_real.py` |
| Compose pins `dgraph/standalone:v25.3.4` | Read directly from `docker/docker-compose.yml` in this repository |
| `pyyaml` has zero `src/` usage; two test consumers | `grep -rn "yaml" src/ --include=*.py` (empty) plus the two importing test files, run in this repository |
| Both `httpx.Client` call sites pass `follow_redirects` explicitly | Read directly: `src/partgraph/ingest/fetch.py:144`, `src/partgraph/cli.py:3017` |
| gRPC message-size options set explicitly | Read directly: `src/partgraph/cli.py:1287-1288` |
| psutil 7.2.2 / pydgraph 25.2.0 installed and satisfying these bounds | Resolved in the real environment via `importlib.metadata` against a real `SpecifierSet` |

The rows marked **not re-fetched** are the ones to check first if any of this
stops adding up. Nothing in the bottom half of that table depends on a document
outside this repository; the rows above it do, and only the psutil changelog
rows have survived deliberate re-verification.

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
  confirming `create_time()`'s contract survives. Do not widen the floor.
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
declared, not about its version, and moving it to the `dev` extra is a different
objective with a different blast radius (CI install commands, packaging
metadata, anyone depending on the current surface). It is deliberately **not**
acted on in this change, and is left as the next dependency-hygiene item.

### What this change does not claim

No behaviour of the shipped product changes. No code path, CLI output, schema or
on-disk format is touched. This ADR constrains what may be *installed*; it does
not assert that any currently installed version was wrong, and no runtime
misbehaviour attributable to an unbounded dependency has been observed in this
repository. The psutil floor is preventive, argued from the dependency's own
changelog, not from an incident here.
