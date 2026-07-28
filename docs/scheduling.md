# Scheduling PartGraph refreshes

PartGraph is built once and then drifts from reality: LCSC stock/price move
daily and datasheet links rot. Two one-shot commands re-sync a built graph:

- **`partgraph refresh`** — re-checks each part's `stock` / `price_usd` /
  `is_basic` against the source snapshot and stamps `stock_checked_at`.
- **`partgraph refresh-links`** — HTTP-checks datasheet links, stamps
  `verified_at` / `http_status` / `fail_count`, and auto-purges a link after
  repeated failures.

Both are **one-shot** commands, bounded per run — there is **no in-app daemon**
and no `partgraph refresh-all` subcommand. Periodic execution is left to your
host's scheduler (a **systemd timer** or **cron**), wrapping the shipped
`scripts/partgraph-refresh-all.sh`. This guide covers:

1. [What runs, and in what order](#1-what-runs-and-in-what-order)
2. [Cadence and rationale](#2-cadence-and-rationale)
3. [Option A — systemd (`--user` timer)](#3-option-a--systemd---user-timer)
4. [Option B — cron](#4-option-b--cron)
5. [Enabling the weekly `--fetch`](#5-enabling-the-weekly---fetch)
6. [Container engine is irrelevant here](#6-container-engine-is-irrelevant-here)

> [!WARNING]
> **These schedules assume the database is already running** — i.e. you have run
> **`partgraph db up`** and it stays up. This scheduling layer only runs the
> refresh commands; it **does not start, stop, or health-check the database**.
> That holds even though `partgraph refresh`/`refresh-links` are, when invoked
> interactively, autostart-capable commands (ADR-0022 Section 7): the shipped
> wrapper `scripts/partgraph-refresh-all.sh` does
> `export PARTGRAPH_AUTOSTART=0` before it invokes `partgraph`, so a scheduled
> run never implicitly starts a container — an unattended, schedule-triggered
> container start is exactly the kind of unattended resource use ADR-0022
> exists to eliminate, not reintroduce through the timer. The export lives in
> the **wrapper**, not only in the unit, on purpose: `systemd.exec(5)` states
> that `EnvironmentFile=` settings *override* those made with `Environment=`,
> unconditionally and regardless of the order the two directives appear in, so
> the unit's own `Environment=PARTGRAPH_AUTOSTART=0` would lose to any
> `PARTGRAPH_AUTOSTART=` line in your optional
> `~/.config/partgraph/refresh-all.env`. A shell `export` is applied after
> systemd has assembled the environment, so it cannot be overridden that way.
> The unit keeps its `Environment=` line as a second layer, for the case where
> the wrapper is bypassed. If the database is down when a job fires, the refresh commands exit
> non-zero with a path-free "is the database running?" hint and the wrapper
> propagates that failure to your scheduler — nothing is corrupted, the run is
> simply logged as failed.

---

## 1. What runs, and in what order

`scripts/partgraph-refresh-all.sh` runs both commands, **in order**, and always
attempts **both** even if the first fails:

```text
phase 1/2  partgraph refresh        # stock / price / basic status
phase 2/2  partgraph refresh-links  # datasheet link freshness + auto-purge
```

- **Order:** stock/price first (the heavier phase, and the one that optionally
  re-downloads the source snapshot), then the link check.
- **Both attempted:** a phase-1 failure never skips phase-2 link maintenance.
- **Aggregate exit:** the wrapper exits `0` only when **both** phases exit `0`;
  otherwise it propagates the first non-zero phase's status, so a failed run is
  visible to systemd / cron.
- **CLI resolution:** the wrapper calls `${PARTGRAPH_BIN:-partgraph}` (resolved
  via `command -v`), so no install path is hard-coded. Set `PARTGRAPH_BIN` if
  the CLI is not named `partgraph` on your `PATH`.

Each phase prints a UTC-timestamped, path-free banner (`phase 1/2 refresh —
start`, `… exit=<rc>`, and a final `overall exit=<rc>`) to stdout, which systemd
captures into the journal.

---

## 2. Cadence and rationale

The cadence is tied to the commands' shipped `--stale-days` defaults, so a run
only does work that has actually gone stale (everything fresher is skipped by a
server-side staleness filter):

| Cadence            | What runs                                   | When            | Tied to the default                       |
| ------------------ | ------------------------------------------- | --------------- | ----------------------------------------- |
| **Primary** (weekly) | the wrapper: `refresh [--fetch]` → `refresh-links` | Sunday **04:42** | `refresh` default `--stale-days 7`        |
| **Optional** (daily) | `refresh-links` only                        | daily **03:17** | `refresh-links` default `--stale-days 30` |

- **Weekly `refresh` ↔ `--stale-days 7`.** `partgraph refresh` defaults to
  `--stale-days 7`, so a weekly run re-checks exactly the parts whose
  `stock_checked_at` is a week or more old — the whole eligible catalogue every
  week, with nothing re-checked twice in the same window.
- **Daily `refresh-links` ↔ `--stale-days 30`.** `partgraph refresh-links`
  defaults to `--stale-days 30`. Running it **daily** re-checks only the links
  that crossed the 30-day mark that day (~1/30 of the catalogue), spreading
  ~475k datasheet checks across the month instead of hammering everything at
  once. The optional daily link check **requires a source snapshot to already
  exist** (from a prior `--fetch`); on its own it never downloads.

The shipped timer covers the **primary weekly** cadence (its `refresh-links`
phase also honours `--stale-days 30`). The optional daily link-only check is an
extra crontab line or a second `--user` timer if you want tighter link
freshness.

> The other flags are left at their defaults: `refresh-links --max-failures 3`
> (auto-purge after 3 consecutive failures) and `--timeout 10.0` (seconds per
> HTTP check). Both commands take `--limit` for development-only partial runs;
> a scheduled run should omit it so the full catalogue is covered across runs.

---

## 3. Option A — systemd (`--user` timer)

The units under `systemd/` are **per-user** units: installed with
`systemctl --user`, they run as **your** login user — never root (no `User=` is
set) — and use `%h` for your home directory, so nothing is hard-coded to a
specific operator. They are hardened with `NoNewPrivileges=true`,
`ProtectSystem=strict`, and `PrivateTmp=true`.

From the repository root:

```bash
# 1. Install the wrapper on your per-user bin (matches the unit's ExecStart,
#    %h/.local/bin/partgraph-refresh-all.sh).
install -m 0755 scripts/partgraph-refresh-all.sh "$HOME/.local/bin/partgraph-refresh-all.sh"

# 2. Install the unit files for your user.
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 systemd/partgraph-refresh-all.service "$HOME/.config/systemd/user/"
install -m 0644 systemd/partgraph-refresh-all.timer   "$HOME/.config/systemd/user/"

# 3. Enable and start the timer.
systemctl --user daemon-reload
systemctl --user enable --now partgraph-refresh-all.timer

# 4. Confirm the schedule.
systemctl --user list-timers partgraph-refresh-all.timer
journalctl --user -u partgraph-refresh-all.service   # after the first run
```

The timer fires **Sunday 04:42** (`OnCalendar=Sun *-*-* 04:42:00`), with
`Persistent=true` (a missed run — e.g. the machine was asleep — fires once at
the next boot) and `RandomizedDelaySec=600` (up to 10 minutes of jitter). Point
the CLI at a non-default binary, or opt into `--fetch`, via the optional
`EnvironmentFile` at `%h/.config/partgraph/refresh-all.env` (see
[section 5](#5-enabling-the-weekly---fetch)).

---

## 4. Option B — cron

If you prefer cron, schedule the same wrapper. Cron runs with a **minimal
`PATH`**, so make `partgraph` resolvable — either set `PATH` at the top of the
crontab to include the directory holding the CLI (your conda / virtualenv
`bin`), or set `PARTGRAPH_BIN` to its absolute path. Then (`crontab -e`):

```cron
# Make `partgraph` resolvable under cron's minimal PATH. Replace the placeholder
# with the directory that contains your `partgraph` CLI.
PATH=<your-partgraph-env-bin>:/usr/bin:/bin

# Primary: weekly full refresh (stock/price + datasheet links) — Sunday 04:42.
42 4 * * 0 $HOME/.local/bin/partgraph-refresh-all.sh

# Optional: daily datasheet-link check only — 03:17.
17 3 * * * partgraph refresh-links
```

`$HOME` is expanded by cron, so the wrapper line needs no absolute operator
path. The weekly line matches the systemd cadence above; the optional daily
line runs only the link checker (`--stale-days 30`).

---

## 5. Enabling the weekly `--fetch`

By default the wrapper runs `partgraph refresh` **without** `--fetch`: it
re-uses the source snapshot already on disk and never downloads. `--fetch` is
**opt-in** and is added only when `PARTGRAPH_REFRESH_FETCH` is set to a
non-empty value (both unset and empty mean "no fetch"):

```bash
# systemd: put it in the EnvironmentFile the unit already reads.
mkdir -p "$HOME/.config/partgraph"
printf 'PARTGRAPH_REFRESH_FETCH=1\n' >> "$HOME/.config/partgraph/refresh-all.env"
```

With `--fetch`, phase 1 re-downloads the **~1 GB** JLCPCB/LCSC source snapshot
into your checkout's `data/` directory before refreshing.

> [!WARNING]
> The service ships with `ProtectSystem=strict`, which mounts the filesystem
> read-only. The default no-fetch run writes nothing to disk (it only talks to
> the database over the network), so it is unaffected. **`--fetch` does write**
> (the ~1 GB snapshot), so if you enable it you must grant that one directory
> back with a drop-in — `systemctl --user edit partgraph-refresh-all.service`:
>
> ```ini
> [Service]
> ReadWritePaths=<path-to-your-partgraph-checkout>/data
> ```

---

## 6. Container engine is irrelevant here

The scheduling layer is **engine-agnostic**. The refresh commands only speak to
the local database over **`127.0.0.1:9081`** (gRPC) and to third-party datasheet
hosts over HTTPS — they never talk to the container runtime. Whether the
database is served by Podman or Docker (PartGraph auto-detects, podman-first)
makes no difference to these timers/cron jobs. The wrapper deliberately contains
no engine, compose, or database-lifecycle commands.

---

## How this was verified

- **Environment:** the units were validated with `systemd-analyze verify`
  (both `.service` and `.timer` together) and `systemd-analyze calendar`, and
  the wrapper's order / both-attempted / aggregate-exit behaviour was exercised
  with a **stub** standing in for `partgraph` across all four phase-exit
  combinations `(0,0) / (≠0,0) / (0,≠0) / (≠0,≠0)`. Every flag and default cited
  above was taken from live `partgraph refresh --help` and
  `partgraph refresh-links --help`.
- **Date:** 2026-07-04.
- **Not executed:** no real `partgraph refresh` / `refresh-links` was run
  against a live database, no ~1 GB `--fetch` download was performed, and the
  `systemctl --user enable --now` / `crontab` install steps were **not** applied
  on the authoring host — they are documented for you to run. The verification
  used the stub and the unit files only, never the real database.
