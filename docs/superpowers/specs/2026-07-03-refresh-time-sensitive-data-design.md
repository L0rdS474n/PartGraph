# Design — Scheduled refresh of time-sensitive PartGraph data (issue #11)

- Date: 2026-07-03
- Status: Approved (design); implementation driven via /pipeline, PR by PR
- Related: issue #11; reuses ADR-0008 (adaptive pacing), ADR-0010/0011 (embed
  uid-cursor + staleness filter + narrow uid write-back), ADR-0009 (engine detection)

## Problem

PartGraph is built once and then drifts from reality: **stock** and **price_usd**
change continuously, **datasheet/product links** rot, and there is **no freshness
timestamp** on any node (the `jlcparts@…` string is a hard-coded `SOURCE_REF`
constant, not a real time). The upstream jlcparts source is a **full-snapshot
~1 GB SQLite with no incremental feed**, so "refresh volatile fields" ultimately
means "re-download the snapshot", while "check datasheet links" is independent of
it (plain HTTP against existing URLs).

## Architecture (whole feature)

Every refresh command reuses the embed pattern established in ADR-0010/0011:
**uid keyset cursor (intra-run progress) + a staleness `@filter` (cross-run
idempotency) + `ResourceController` pacing + a narrow uid-only write-back**. The
only change from embed is the selection predicate: `NOT has(embedding)` becomes a
time-based staleness filter (`NOT has(verified_at) OR lt(verified_at, T)`).

Freshness timestamps are stamped at **load/refresh time**, never in `normalize`
(which stays clock-free / byte-reproducible). Scheduling is **external**
(host cron / systemd timer) around one-shot `partgraph refresh*` subcommands —
there is no in-app daemon; the only long-running component stays the Dgraph
container.

## Decomposition (one PR per sub-project)

| PR | Sub-project | Delivers | Depends on |
|----|-------------|----------|------------|
| **1** | Freshness foundation + datasheet link-rot checker | `Datasheet.fail_count` predicate; `partgraph refresh-links` (HTTP-checks datasheet URLs, writes `verified_at`/`http_status`/`fail_count`, auto-purges a link after N consecutive failures) | — |
| **2** | Stock/price refresh | `Part.stock_checked_at` predicate; `partgraph refresh` (reload SQLite, narrow uid mutation of stock/price_usd/is_basic + stamp) | PR 1 |
| **3** | Scheduling | engine-agnostic cron/systemd timer + wrapper docs driving the refresh commands periodically | PR 1–2 |
| **4** *(optional, later)* | Incremental re-embedding | re-embed Parts whose embedding text changed | #10/#11 pattern |

## PR 1 — detail (this cycle)

**Schema (`schema/partgraph.dql`).** `Datasheet.verified_at: datetime` and
`Datasheet.http_status: int` already exist (unused) — start writing them. Add
`Datasheet.fail_count: int` (consecutive failures; 0 = healthy). `db apply-schema`
re-alters idempotently. (`Part.stock_checked_at` is deferred to PR 2.)

**`partgraph refresh-links` (new one-shot command).**
- Flags: `--stale-days N` (default 30 — only check links whose `verified_at` is
  missing or older than N days), `--limit N` (dev cap), `--max-failures N`
  (default 3 — auto-purge after N consecutive failures), `--timeout S`.
- Selection (embed pattern, on **Datasheet** nodes, its own loop — must NOT modify
  the embed functions, to avoid conflict with the open embed PRs):
  `q(func: type(Datasheet), first: P, after: <uid>) @filter(NOT has(verified_at) OR lt(verified_at, T)) { uid url http_status fail_count }` + uid cursor.
- Per URL: HTTP **HEAD** (GET fallback on 405), with timeout. Classify 2xx/3xx =
  alive; 4xx/5xx/timeout/TLS/connect = dead.
- Narrow uid write-back: alive → `{verified_at: now, http_status: code,
  fail_count: 0}`; dead → `{…, fail_count: prev+1}`.
- **Auto-purge:** when the new `fail_count ≥ max_failures`, drop the `datasheet`
  edge from the referencing Part(s) and the Datasheet node, in a separate mutation
  with an explicit path-free log line (destructive; the multi-valued `datasheet`
  edge "survives link rot").

**Concurrency & politeness.** Most URLs are `lcsc.com` → bounded concurrency with a
**per-host rate limit** (semaphore/thread-pool + delay), a dedicated `User-Agent`.
`ResourceController` paces local resources between pages (as embed). Bounded per run
(e.g. 200k) → the full catalogue is covered across runs / cron.

**Idempotency & errors.** uid cursor = one check per Datasheet per run; staleness
filter = a re-run skips freshly-checked nodes (no double-count of `fail_count`).
`fail_count` is read in the selection, incremented client-side, written back.
Path-free errors (embed pattern); the 256 MiB gRPC ceiling is already set.

**Testing.** Unit only: mock the HTTP client (scripted status codes) + mock the
Dgraph txn — alive→0, dead→+1, threshold→edge purged, cursor pagination, staleness
filter, path-free errors. No real HTTP, no container, no cve-graph.

**Out of scope for PR 1:** stock/price refresh, scheduling, re-embedding, and any
modification to the embed functions (`_embed_all_pages`/`_select_parts_for_embed`).
