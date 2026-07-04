# ADR-0013: stock/price refresh — narrow write-back, lcsc_id join, freshness stamping

- Status: Accepted
- Date: 2026-07-04

## Context

PartGraph is built once and then drifts from reality: a part's LCSC stock,
unit price and basic/extended status are the fastest-moving facts in the whole
graph, yet nothing re-checked them after the initial ingest and no Part carried
a freshness timestamp for them. Issue #11 PR 1 added `partgraph refresh-links`
(datasheet freshness + link-rot auto-purge, ADR-0012). PR 2 adds the sibling
`partgraph refresh`: it pages Part nodes, looks each part up in the current
JLC/LCSC source snapshot, writes back the fresh `stock`/`price_usd`/`is_basic`
by `uid`, and stamps a new `stock_checked_at` predicate.

This reuses the pattern established by ADR-0010/0012 (uid keyset cursor for
intra-run progress + a staleness `@filter` for cross-run idempotency +
`ResourceController` pacing + a narrow uid-only write-back), but the moving data
here is commercial and lives in the ~1 GB source snapshot rather than behind an
HTTP probe. That difference drives the decisions the frozen PR 2 tests pin and
that need recording. The open embed-hardening / refresh-links work forbids
modifying those pipelines, so the new path is fully decoupled.

## Decision

### D1 — Narrow uid write-back against a source join, NOT a full re-load

`refresh` never re-runs `normalize`/load. It selects only the fields it will
change (`uid` + `lcsc_id`) from the graph, joins against the source snapshot,
and writes back the minimal `{uid, stock?, price_usd?, is_basic,
stock_checked_at}` payload by `uid` (never a new node, never a blank node). A
full re-ingest would churn the entire graph (every node, edge and embedding) to
refresh three volatile scalars; the narrow write-back touches only the commerce
fields and the freshness stamp, so it is cheap, idempotent and safe to schedule
often. This mirrors the embed/refresh-links narrow-write discipline.

### D2 — Join on `lcsc_id`; only parts that carry one are eligible

LCSC stock/price is keyed by the LCSC id, so the graph↔source join is on
`lcsc_id`. The selection filter therefore requires `has(lcsc_id)` — a part with
no LCSC identity can never be matched and is excluded up front. The source side
is pre-joined once into an in-memory dict (see D3), so each page is a set of
O(1) dict lookups rather than a per-part source query. A part whose `lcsc_id` is
present in the graph but ABSENT from this run's snapshot is still stamped (D5),
so it is not re-selected every run.

### D3 — Source index is a dict, built ONCE via the already-shipped adapter

The snapshot is parsed exactly once per run, up front, by reusing the existing
`open_jlcparts_db`/`JlcpartsAdapter` (mirroring `_stage_normalize`) and folding
its row stream into a `lcsc_id -> (stock, price_usd, is_basic)` dict
(`build_stock_index`). The SAME dict object is threaded by identity into every
page's write-back — never rebuilt per page. Duplicate `lcsc_id` rows are
last-wins (a later source row supersedes an earlier one); a row with no
`lcsc_id` is skipped. `is_basic` is coerced to a strict `bool` by identity so a
raw `1`/`"true"`/`0` from a non-adapter caller still normalises to the canonical
singleton.

### D4 — Value sanity before write-back (omit, never null, never a bad number)

A source `stock`/`price_usd` is written through only when it is a genuine,
in-range quantity. A value that is `None`, negative, non-finite (`NaN`/`inf`,
via `math.isfinite`) or absurdly large — strictly above the named leaf ceilings
`_MAX_SANE_STOCK` (10^9) / `_MAX_SANE_PRICE_USD` (10^6 USD) — is OMITTED from the
payload entirely, exactly as if the field were absent. It is never written as a
JSON `null` (which would blank a previously-good value) and never as a
non-finite float (which is not valid JSON and would corrupt the mutation). The
ceilings are inclusive (`>` semantics): a value exactly AT a ceiling is still
valid. `is_basic` is always written (a strict `bool`); the freshness stamp is
always written.

### D5 — Freshness is stamped at refresh time; absent parts are stamp-only

`stock_checked_at` is written by `refresh` at check time from an injected wall
clock (`_utcnow` in the CLI, `clock=` in the leaf), formatted as a deterministic
RFC-3339 UTC `…Z` string. `normalize` stays clock-free and byte-reproducible —
freshness is a refresh-time fact, not a normalization fact. A MATCHED part gets
the full volatile write plus the stamp; an ABSENT part (its `lcsc_id` is not in
this run's snapshot) gets a stamp-ONLY write that leaves `stock`/`price_usd`/
`is_basic` untouched, so a temporary source gap never blanks good data and the
part is still marked checked (not re-selected until it goes stale again).

The formatter is COPIED into the new leaf rather than imported from the
link-refresh leaf, keeping the two leaves fully decoupled (a whole-module import
scan enforces this). The default staleness window is `--stale-days 7`
(commercial data moves fast); `--stale-days 0` re-stamps everything not already
checked at this exact instant, and a negative value is rejected path-free.

### D6 — No index on `stock_checked_at`

`stock_checked_at: datetime` is declared WITHOUT an `@index`, mirroring the
index-free `verified_at` precedent: the staleness `@filter` uses only plain
`has()`/`lt()` against a `type(Part)`-rooted selection, which Dgraph v25.3.4
evaluates without a secondary index. Adding one would cost write amplification
on every stamp for no query benefit. `db apply-schema` re-alters idempotently,
so the added predicate and Part-type field apply cleanly to an existing graph.

### D7 — Selection filter parenthesisation binds `has(lcsc_id)` to the whole OR

The selection `@filter` is exactly
`@filter(has(lcsc_id) AND (NOT has(stock_checked_at) OR lt(stock_checked_at, T)))`.
The inner parentheses are load-bearing: without them, `A AND B OR C` would parse
as `(A AND B) OR C`, wrongly selecting `lcsc_id`-less parts whenever the
staleness arm's `lt(...)` is true. The frozen tests pin the exact string, so the
grouping cannot silently regress.

### D8 — Decoupled from embed / refresh-links (own constants, copied helpers)

The refresh path reuses the *pattern* but never the embed or refresh-links
*functions* (which the open PRs forbid modifying). It has its own
`_REFRESH_STOCK_*` constants (`_REFRESH_STOCK_SELECT_DEFAULT`/`_SELECT_PAGE_SIZE`/
`_CURSOR_STALL`/`_DB_ERROR`, plus a textually-distinct source/fetch error), its
own copied `_REFRESH_STOCK_UID_RE` (never imported/aliased) and its own
`_refresh_stock_page_max_uid`/`_select_parts_for_refresh`/`_refresh_stock_all_pages`.
The leaf lives in a new module, `partgraph.refresh.stock`, that imports only the
standard library — no gRPC/HTTP client library and no reach into a sibling
pipeline (both enforced by a whole-module source scan). `_build_dgraph_client`'s
gRPC ceiling, `_validate_limit`, `_utcnow`, `_stage_fetch` (`--fetch`/`--force`)
and `_require_source_file`/`RAW_DB_RELPATH` are reused unchanged.

One difference from embed/refresh-links termination: a FULL page whose rows
cannot form a keyset cursor (only reachable with malformed uids — a real Dgraph
page always yields a valid cursor) continues to the next page rather than ending
the run early; the previous good cursor is preserved and the non-advancement
stall guard still fires if progress genuinely stops.

### D9 — Errors are path-free and role-specific

Three distinct, path-free error constants render the three failure classes: a
missing source file (`_require_source_file`, names only the RELATIVE
`RAW_DB_RELPATH` and hints `--fetch`), an unreadable/corrupt source snapshot
(caught BROADLY — `except Exception`, so a `sqlite3.DatabaseError` on a corrupt
cache is handled the same as an adapter `ValueError`, never a leaking
traceback), and a Dgraph selection/write-back failure (`_REFRESH_STOCK_DB_ERROR`,
textually distinct from `_EMBED_DB_ERROR` and `_REFRESH_DB_ERROR`, hinting
`partgraph db up`). A `--fetch` download failure at this new call site is caught
path-free too. No raw exception text and no absolute filesystem path is ever
surfaced. Leaf write-back mutation/commit failures PROPAGATE unchanged so the
CLI converts them into exactly one such path-free error.

## Consequences

- Every part with an LCSC identity gains a fresh `stock`/`price_usd`/`is_basic`
  and a real `stock_checked_at` from a reproducible refresh-time stamp;
  `normalize` stays byte-reproducible.
- A corrupt, non-finite, negative or absurd source quantity is omitted rather
  than written through, so the graph never carries a `null`, a `NaN`/`inf`, or a
  garbage number in a commerce field.
- A temporary source gap is non-destructive: an absent part is stamp-only, so
  good data is never blanked and the part is not re-checked until it goes stale.
- The refresh path introduces its own constants/helpers and a new stdlib-only
  leaf, and never references the embed or refresh-links functions, so it cannot
  conflict with the concurrently-open hardening PRs.
- Cross-run idempotency rests on the client-side deterministic staleness cutoff
  driving Dgraph's server-side `@filter`; a live end-to-end skip guarantee would
  need an integration test against a running Dgraph and is out of unit scope.
- Scheduling (cron/systemd) for periodic refresh is deferred to PR 3, alongside
  the datasheet-refresh scheduling recorded in ADR-0012.
