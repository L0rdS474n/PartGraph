# ADR-0012: refresh-links freshness stamping, fail-closed URL policy, and auto-purge

- Status: Accepted
- Date: 2026-07-03

## Context

PartGraph is built once and then drifts from reality: datasheet/product links
rot, and no node carried a real freshness timestamp (the `jlcparts@…`
`SOURCE_REF` is a hard-coded provenance constant, not a time). Issue #11 PR 1
adds `partgraph refresh-links`: it pages Datasheet nodes, HTTP-checks each URL,
stamps `verified_at`/`http_status`, tracks consecutive failures in a new
`fail_count` predicate, and auto-purges a link after N consecutive failures.

This reuses the embed pattern established in ADR-0010 (uid keyset cursor for
intra-run progress + a staleness `@filter` for cross-run idempotency +
`ResourceController` pacing + a narrow uid-only write-back), but is a link
checker that talks to arbitrary third-party hosts and performs a *destructive*
graph mutation (edge + node delete). That raises four decisions the frozen PR 1
tests pin and that need recording, plus one deliberate deferral.

## Decision

### D1 — Freshness is stamped at refresh time, never in `normalize`

`verified_at` is written by `refresh-links` at check time (the injected wall
clock), formatted as a deterministic RFC-3339 UTC `…Z` string
(`format_verified_at`). `normalize` stays clock-free and byte-reproducible: it
must never stamp a timestamp, or the same source snapshot would produce
different staged bytes on every run, breaking the reproducibility property the
normalize stage is built around. Freshness is a *load/refresh-time* fact, not a
*normalization* fact. The clock is an injected seam (`_utcnow` in the CLI,
`clock=` in the leaf) so tests are deterministic and no real wall clock leaks.

### D2 — Fail-closed URL / SSRF policy (validate before any I/O)

`is_checkable_url` gates every URL before the HTTP client is touched:

- **Scheme allow-list of exactly `{http, https}`.** Not a deny-list — a
  deny-list of a few schemes (`file`/`ftp`/`gopher`/`data`) misses the long tail
  (`sftp`/`ws`/`jar`/`dict`/…). Only `http`/`https` proceed.
- **Literal non-public IPs are rejected** via stdlib `ipaddress`: any
  loopback / link-local / private / unspecified / reserved / multicast address,
  IPv4 or IPv6, including the IPv4-mapped IPv6 loopback `::ffff:127.0.0.1`
  (which `ipaddress` correctly classifies `is_loopback`).
- **Numeric/alt-encoding IPv4 bypasses fail closed.** Decimal `2130706433`,
  hex `0x7f000001`, single-token octal `017700000001`, per-octet octal
  `0177.0.0.1`, BSD shorthand `127.1` and bare `0` all make
  `ipaddress.ip_address()` raise `ValueError` (verified empirically) even though
  a permissive OS resolver / HTTP library can still resolve them to 127.0.0.1.
  The naive fallback "ValueError from ipaddress ⇒ treat as a plain hostname ⇒
  allow" is the exact bypass, so we invert it: a host that is not a parseable
  public-IP literal is allowed **only** when it is a genuine dotted DNS hostname
  ending in an alphabetic TLD. Every obfuscated numeric form has no such TLD
  (no dot, or a numeric last label), so it fails closed.
- **Outbound headers carry only the dedicated `USER_AGENT`.** No
  Authorization/Cookie/Proxy-Authorization (or any other) header is ever sent to
  a third-party datasheet host — a link checker has no business forwarding a
  credential anywhere.

A policy rejection is itself a definitive **dead** result (status 0, zero HTTP
calls), so an unsafe URL still counts as checked and increments `fail_count`.

### D2a — DNS-rebind / resolve-time SSRF is a documented DEFERRAL

`is_checkable_url` validates the *literal host string*. It does **not** resolve
DNS and re-check the resolved address, so a hostname that resolves to a private
address (DNS rebinding / an attacker-controlled `A` record → 127.0.0.1) is not
caught at policy time, and httpx's own connect-time `getaddrinfo` could still
reach an internal address. This is deferred deliberately, not overlooked:

- the URLs are **DB-sourced**, ingested from the curated jlcparts/LCSC snapshot
  — not attacker-submitted at check time — so the resolve-time threat is low for
  the actual input;
- closing it properly needs a pinned-resolver / connect-time IP re-validation
  (resolve, re-run the D2 IP checks on every resolved address, then connect to
  that vetted IP), which is a focused change best made where the HTTP client is
  built. It is recorded here as a follow-up (a PR 2/3-era hardening), so the
  literal-host fail-closed policy above is the *floor*, not the ceiling.

### D3 — `fail_count` auto-purge: `>=` threshold, separate txn, multi-Part

- **New predicate `Datasheet.fail_count: int`** (0 = healthy). It is the only
  schema change PR 1 makes; no `verified_at` index is added (a dispatcher-verified
  probe showed `lt(verified_at, T)` inside an `@filter` with a
  `func: type(Datasheet)` root already works without one). A pre-existing row has
  `fail_count` unset on the first run ever, so the leaf coerces missing/`None` to
  0 before incrementing (`None + 1` must never crash).
- **Threshold uses `>=`, not `==`.** A node stranded by a crash between the
  write-back commit and the purge txn can already carry a `fail_count` at/above
  the threshold; this run's dead check makes the new value strictly greater. A
  `== max_failures` comparison would leave such a node linked forever, so the
  purge fires on `new_fail_count >= max_failures`.
- **Purge is a SEPARATE transaction, AFTER the write-back commit.** The
  `fail_count` write-back for the whole page commits in one transaction first;
  only then does each crossed-threshold link purge in its own transaction, so a
  purge failure never rolls back the freshness bookkeeping (and both propagate to
  the CLI as one path-free error rather than being swallowed).
- **Multi-Part delete via the reverse `~datasheet` edge.** `datasheet` is a
  multi-valued `[uid] @reverse` edge, so a Datasheet can be shared by many Parts.
  The purge looks up every referencing Part via `~datasheet` and deletes, in one
  `del_nquads` mutation, one `<part_uid> <datasheet> <ds_uid> .` triple per Part
  plus one `<ds_uid> * * .` node-delete triple (the RDF `<uid> * * .` node delete
  is the only in-repo node-deletion precedent — `tests/conftest.py`). Every uid
  interpolated into raw n-quad/query text is shape-validated first, so no
  untrusted value can break out of `<…>` or a `uid(…)` clause. The event is
  reported through an `on_purge(uid, fail_count, parts_unlinked)` callback of
  plain path-free primitives, which the CLI renders as the destructive notice.

### D4 — Purge durability across a re-ingest: self-healing, not durable

The loader upserts Datasheets **by url** (`eq(url) @filter(type(Datasheet))`,
loader.py) with no `fail_count` field. A purge deletes the Datasheet node, so a
subsequent full `partgraph ingest` re-run — finding the same url in the ~1 GB
snapshot — finds no existing node and **re-creates** the Datasheet (a fresh uid,
`fail_count` unset = healthy). The purge is therefore **not durable** across a
re-ingest.

This is accepted with rationale rather than fixed in PR 1:

- it is **self-healing**: a link that was purged on a transient outage (or a
  false-positive purge) is transparently restored and simply re-checked next
  run;
- the cost is that a genuinely-dead link the upstream snapshot keeps listing is
  re-checked from `fail_count = 0` after each full re-ingest, i.e. re-purged
  after N failures again — bounded, idempotent waste, not corruption;
- making purges durable (e.g. a tombstone predicate the loader honours) couples
  the refresh feature to the load path and is out of scope for the link checker.
  It is recorded as a **PR 2/3 follow-up** to decide alongside the stock/price
  refresh and scheduling work.

### D5 — Per-host rate-limit politeness

Most datasheet URLs share a handful of hosts (largely `lcsc.com`), so an
unbounded serial check would hammer one host. `refresh-links` constructs a single
`HostRateLimiter` per run (its per-host timing persists across pages) and
acquires it strictly before each URL's HTTP check, pausing only when two checks
against the SAME host fall within `_REFRESH_HOST_MIN_INTERVAL`; different hosts
never rate-limit each other and the first check of any host never waits. The
limiter's `clock`/`sleep` are an injected monotonic pair (distinct from the
wall-clock used for `verified_at`) so the unit suite drives it deterministically
and real `time.sleep` is never called in the leaf. `ResourceController` +
`get_system_reader` additionally pace *local* CPU/RAM load between pages, exactly
as embed does — bounded, and a no-op on a healthy box.

### D6 — Embed-pattern reuse WITHOUT importing the frozen embed functions

`refresh-links` reuses the embed *pattern* (uid keyset cursor, terminate-on
empty/short/non-advancing page, numeric `int(uid, 16)` max-uid, narrow uid
write-back, path-free errors) but the open embed-hardening PRs forbid modifying
the embed functions. So the refresh path is **decoupled**: it has its own
`_REFRESH_SELECT_DEFAULT`/`_REFRESH_SELECT_PAGE_SIZE`/`_REFRESH_CURSOR_STALL`
constants, its own copied `_REFRESH_UID_RE` (never imported/aliased from the
embed section), its own `_refresh_page_max_uid`, and its own
`_select_datasheets_for_refresh`/`_refresh_all_pages`. It never calls
`_select_parts_for_embed`, `_embed_all_pages`, `_page_max_uid` or `embed_write`
(asserted structurally by the tests). The only selection difference from embed is
the predicate: `NOT has(embedding)` becomes
`NOT has(verified_at) OR lt(verified_at, T)`, and the target type is `Datasheet`,
not `Part`. `_build_dgraph_client`'s 256 MiB gRPC ceiling and `_validate_limit`
are reused unchanged. `httpx` is imported lazily via `_build_http_client`
(mirroring `partgraph.ingest.fetch`) behind an injectable seam, so the leaf and
CLI import stays light and no real socket is opened in the unit suite.

## Consequences

- Every Datasheet gains a real `verified_at`/`http_status`/`fail_count` from a
  reproducible refresh-time stamp; `normalize` stays byte-reproducible.
- The link checker cannot be steered at internal infrastructure via the literal
  host of a datasheet URL — the scheme allow-list plus the fail-closed literal-IP
  and numeric-encoding checks reject the whole known SSRF-bypass class before any
  socket is opened, and no credential-like header is ever forwarded.
- The resolve-time / DNS-rebinding SSRF surface remains open by design and is an
  explicit, low-severity (DB-sourced input) follow-up — the literal-host policy
  is a documented floor, not a claimed ceiling.
- A rotted datasheet link is auto-removed from every referencing Part after N
  consecutive failures, in a separate transaction with a path-free destructive
  notice; the `>=` threshold makes the decision robust to a crash-stranded,
  already-over-threshold node.
- A purge is not durable across a full re-ingest (self-healing by url upsert);
  this is bounded, idempotent, and left as a PR 2/3 decision rather than coupling
  the checker to the load path.
- The embed pipeline is provably untouched: the refresh path introduces its own
  constants/helpers and never references the embed functions, so it cannot
  conflict with the concurrently-open embed-hardening PRs.
