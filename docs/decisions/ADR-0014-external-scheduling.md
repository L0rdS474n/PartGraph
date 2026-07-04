# ADR-0014: external scheduling for periodic refresh (systemd/cron wrapper)

- Status: Accepted
- Date: 2026-07-04

## Context

Issue #11 added two one-shot freshness commands: `partgraph refresh-links`
(datasheet links, ADR-0012) and `partgraph refresh` (stock/price, ADR-0013).
Both are bounded per run and idempotent across runs (a client-side staleness
cutoff drives a server-side `@filter`), and both `--help` texts already tell the
operator to "schedule it via cron/systemd; see PR 3". ADR-0012 and ADR-0013 each
explicitly **deferred scheduling to PR 3**. This ADR records how that periodic
execution is wired without adding any in-app scheduler.

PR 3 is deliberately a **docs + shell + config** change: no Python source, no
schema, and no test files are touched. It ships a wrapper script
(`scripts/partgraph-refresh-all.sh`), a per-user systemd `.service` + `.timer`,
a guide (`docs/scheduling.md`), and this record. The decisions below are the
ones that shape those artifacts.

## Decision

### D1 — Scheduling is EXTERNAL; no in-app daemon; the DB is never started here

PartGraph stays a set of one-shot CLI commands. Periodicity is owned by the
host's init system (a systemd timer) or cron — there is no long-running
`partgraph` process, no in-app scheduler, and no event loop. The scheduling
layer also **never manages the database lifecycle**: it assumes `partgraph db up`
has been run and the database stays up. If the database is down when a job
fires, the refresh commands already exit non-zero with a path-free "is the
database running?" hint (ADR-0012/0013 D9), which the wrapper surfaces to the
scheduler; nothing is corrupted. Keeping start/stop out of the scheduled path
avoids a timer racing the container lifecycle or tearing down a database another
process is using.

### D2 — A bash wrapper, NOT a `partgraph refresh-all` subcommand

Composing the two refreshes is done by `scripts/partgraph-refresh-all.sh`, not
by a new CLI subcommand. Rationale: a `refresh-all` subcommand would be new
Python surface (new flags, new tests, new error paths) whose only job is to call
two commands the CLI already exposes — pure composition that belongs in the init
layer, where cadence, logging, and retry policy already live. The wrapper is
also what an
operator points a `.timer` or crontab line at, so keeping it a script keeps the
scheduling story entirely in the ops layer and leaves the frozen CLI/refresh
modules untouched (a hard constraint while sibling hardening PRs are open).

### D3 — Run `refresh` then `refresh-links`, both always attempted, aggregate exit

The wrapper runs **phase 1 `refresh`**, then **phase 2 `refresh-links`**, in that
order (stock/price first — the heavier phase and the one that optionally
re-downloads the snapshot — then the link check). Each phase is guarded
(`rc=0; "$BIN" refresh … || rc=$?`) so `set -e` cannot abort between phases:
**phase 2 is attempted even when phase 1 fails**, so a stock/price hiccup never
skips datasheet-link maintenance. The exit status is **aggregated**: `0` only
when both phases exit `0`, otherwise the first non-zero phase's status is
propagated, so a failed run is unambiguous to systemd/cron and the journal.
Per-phase UTC-timestamped, path-free banners (`phase 1/2 refresh — start / …
exit=<rc>`, and a final `overall exit=<rc>`) go to stdout for the journal.

### D4 — CLI resolved via `${PARTGRAPH_BIN:-partgraph}`; no committed operator path

The wrapper resolves the CLI through `${PARTGRAPH_BIN:-partgraph}` and guards it
with `command -v "$PARTGRAPH_BIN"` before doing anything, exiting non-zero with a
path-free message if it is missing. This bakes **no install path** into any
committed file: the default is the bare name `partgraph` (found on `PATH`), and
an operator with a non-standard install overrides `PARTGRAPH_BIN` at runtime
(env or the systemd `EnvironmentFile`). Every expansion is quoted; there is no
`eval`, no `bash -c`, and no unquoted word-splitting. The wrapper contains no
container-engine, compose, or database-lifecycle command, and no committed
absolute operator home path — the repository's operator-path guard test
(`tests/unit/test_repo_skeleton.py::test_no_operator_home_paths_in_tracked_files`)
scans these new files and must stay green.

### D5 — Cadence tied to the shipped `--stale-days` defaults

The documented cadence is derived from the commands' own defaults, not chosen
arbitrarily:

- **Weekly `refresh` ↔ default `--stale-days 7`.** `partgraph refresh` defaults
  to a 7-day staleness window, so a **weekly** primary run (the shipped timer:
  `OnCalendar=Sun *-*-* 04:42:00`) re-checks exactly the parts a week or more
  stale — the whole eligible catalogue each week, nothing re-checked twice in a
  window.
- **Daily `refresh-links` ↔ default `--stale-days 30`.** `partgraph
  refresh-links` defaults to a 30-day window, so an **optional daily** link-only
  check re-checks only the ~1/30 of links that crossed 30 days that day,
  amortising ~475k HTTP checks across the month. The link check needs a source
  snapshot only transitively (it checks DB-sourced URLs), and the daily variant
  never fetches.

The shipped timer implements the weekly primary cadence; the daily link-only
check is documented as an extra crontab line / second `--user` timer.

### D6 — systemd hardening posture: per-user, `%h`, optional EnvironmentFile

The unit is a **per-user** unit (`systemctl --user`): it runs as the operator's
own login user, so it sets **no `User=`** (in particular never `User=root`).
Paths use the `%h` specifier (`ExecStart=%h/.local/bin/partgraph-refresh-all.sh`,
`EnvironmentFile=-%h/.config/partgraph/refresh-all.env`, the leading `-` making
the env file optional), so the committed unit hard-codes no operator home. It is
hardened with `NoNewPrivileges=true`, `ProtectSystem=strict`, and
`PrivateTmp=true`. The **default no-fetch run writes nothing to the filesystem**
(it only talks to the DB over the network), so `ProtectSystem=strict` is
transparent to it. **`--fetch` is the one exception**: it writes the ~1 GB
snapshot into the checkout's `data/` directory, which `ProtectSystem=strict`
would block, so enabling `--fetch` is documented to also need a
`ReadWritePaths=<checkout>/data` drop-in — a deliberate, documented seam rather
than a weaker global posture. `--fetch` itself is opt-in via a non-empty
`PARTGRAPH_REFRESH_FETCH`.

### D7 — Verification is a shell checklist plus a committed regression test

Correctness is validated two ways. First, a reproducible **shell checklist**:
`shellcheck` on the wrapper **when present** (it is absent on the authoring host,
so that step degrades gracefully and is recorded — shellcheck is **not** added as
a dependency), `systemd-analyze verify` on both units (which must exit `0` with
empty stdout **and** stderr — an unknown/misspelt directive still exits `0` but
warns on stderr, so empty stderr is the real gate), and `systemd-analyze
calendar` to confirm the `OnCalendar` normalises. Second — and the durable part —
a **committed, subprocess-based regression test**,
`tests/unit/test_scheduling_wrapper.py`, drives the real wrapper against a
throwaway fake `partgraph` stub (no real database, no network, no container, and
no ~1 GB download — the stub's exit is driven entirely by environment variables)
and locks its security-relevant contract against future edits: both phases
(`refresh` then `refresh-links`) are always attempted, in that order, and each
invoked exactly once; the wrapper's own exit is the **first** non-zero phase
status — the `(≠0,≠0)` case pins first-not-last, so a blanket `exit 1` or a
last-wins aggregation regresses; `--fetch` is gated strictly on a non-empty
`PARTGRAPH_REFRESH_FETCH` (unset and empty both opt out, any non-empty value opts
in); and a `${PARTGRAPH_BIN:-partgraph}` that does not resolve via `command -v`
exits `127` with a path-free message rather than succeeding silently. This closes
Gate 3's **M-1** finding — that nothing committed pinned the wrapper's contract,
so a stray `|| true` around a phase call could otherwise slip past CI — and
brings the unit tree to 765 passing.

## Consequences

- Periodic freshness is achieved with the platform's own scheduler; PartGraph
  gains no daemon, no new CLI surface, and no new Python/schema/test code, so it
  cannot conflict with the concurrently-open refresh/embed hardening PRs.
- A scheduled run always attempts both refreshes and reports a single aggregate
  exit, so a partial failure is visible to systemd/cron without masking the
  other phase's work.
- No committed artifact hard-codes an operator path: the wrapper resolves the
  CLI by name and the units use `%h`, so the repository's operator-path guard
  test covers them and the files are portable across installs.
- The units are hardened and non-root by construction; the single write-path
  exception (`--fetch`'s ~1 GB snapshot under `ProtectSystem=strict`) is called
  out with an explicit `ReadWritePaths` drop-in rather than silently weakening
  the sandbox.
- Scheduling correctness is validated by a documented shell checklist
  (shellcheck-when-present, `systemd-analyze verify`/`calendar`) plus a committed
  subprocess regression test (`tests/unit/test_scheduling_wrapper.py`), keeping
  the "no real DB / no download" constraint intact while still exercising the
  wrapper's real branching.
