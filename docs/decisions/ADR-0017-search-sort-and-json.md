# ADR-0017: `--sort` and `--json` for `partgraph search`

- Status: Accepted
- Date: 2026-07-04

## Context

Issue #15 asked for structured search filters **and** result sorting plus a
machine-readable output mode. ADR-0016 delivered the filters as **PR 1** and
explicitly deferred `--sort`/`--json` to **PR 2**. This ADR covers PR 2: it
realizes that deferral and closes the `--sort`/`--json` half of #15.

Two consumers drive the design: a human at a terminal (who keeps the existing
Rich table) and a machine/script (which needs a stable JSON contract with no
Dgraph-internal identifiers). Both must share one read-only query path and one
error contract.

## Decision

### 1. `--sort relevance|stock|price` — an in-memory re-order, no DQL change

Sorting is a pure re-order of the already-ranked rows in `rank_results`, not a
new query:

- `relevance` (default) is today's order **unchanged** — tier, then `stock > 0`,
  then `is_basic`, then `mpn_norm`, then `uid`. A golden regression pins the
  byte-identity of this default so PR 2 cannot silently perturb it.
- `stock` orders by `stock` descending (a `None`/absent stock sorts as `0`,
  i.e. last), then `mpn_norm`, then `uid`. Match tier is **not** a key.
- `price` orders by `price_usd` ascending, with rows lacking a `price_usd`
  sorting **last** (`price_usd == 0.0` is a real, lowest price — never conflated
  with "missing"), then `mpn_norm`, then `uid`.

In nearest-match mode `--sort` is a **no-op**: the parameter-distance order
always wins, regardless of the flag.

### 2. `--sort` validation — a plain `str` with our own exit-1 error

`--sort` is a plain `str` Typer option validated by `_validate_sort_flag` in the
shared structured-filter validation block, **before** any Dgraph client is
built. A bad value emits the fixed, path-free message
`--sort must be one of: relevance, stock, price.` and exits **1**. It is
deliberately **not** a Typer `Enum`/`Literal`/`click.Choice`: those make Click
reject a bad value with its generic **exit-2** usage error, breaking both the
exit-1 and the fixed-message contract (this mirrors how `--min-stock` /
`--max-price` are already accepted as strings and parsed in our code).

### 3. `--json` envelope + row schema

`--json` prints exactly one JSON object built by the pure serializer
`render_search_results_json(results, parsed) -> dict`:

```json
{"version": 1, "query": "<raw query>", "nearest_match": false,
 "count": N, "results": [ <row>, ... ]}
```

Each row is a **hand-built allowlist dict** of exactly eleven keys —
`mpn`, `mpn_norm`, `manufacturer`, `package`, `category`, `stock`, `is_basic`,
`price_usd`, `match_type`, `datasheets`, `params` — **never** `dataclasses.asdict`
and **never** the raw Dgraph dict (both of which carry `uid`/edge data). The
strings `uid` and any `0x…` value therefore never appear in the output.

Null policy: the seven scalars (`mpn`, `manufacturer`, `package`, `category`,
`stock`, `is_basic`, `price_usd`) are present-but-`null` when absent; `mpn_norm`
is always a non-null identity string; `datasheets` is the list of raw URL
strings (`[]` when none); `params` is a **sparse** map of only the promoted
numeric predicates actually present (`{}` when none). `match_type` is the
machine-safe tier name — `exact` / `trigram` / `fulltext` / `semantic` /
`nearest` — **never** the human, bracket-carrying `_MATCH_LABELS` (`[Semantic]`,
ADR-0008).

`params` is sourced from the **public** ranker surface `RankedRow.params_dict()`,
so the renderer never imports the module-private `_PROMOTED_PREDICATES` tuple
(keeping the private name private).

### 4. `version: 1` forward-compatibility policy

`version` starts at `1`. **Additive** keys (new envelope or row keys) do **not**
bump it; **removing, renaming or retyping** an existing key does. A machine
consumer can therefore add keys defensively without a version gate but must
treat a version bump as a breaking change.

### 5. JSON output safety — stdlib `print`, never Rich

Under `--json` the envelope is emitted with the stdlib
`print(json.dumps(obj, ensure_ascii=False))` — never a Rich Console/table/banner
— so stdout carries exactly one JSON object with no markup, no `Showing N
result(s).` footer and no ANSI. `--no-truncate` is a no-op under `--json` (there
is no column-cropping decision for a machine envelope). The empty/no-match case
and the semantic `_NO_EMBEDDINGS_HINT` short-circuit are **bypassed** under
`--json`: they emit a valid `{…, "count": 0, "results": []}` and exit 0, never
the human text.

On **any** error under `--json` (DB exception, invalid filter, empty query) the
command exits **1** with the fixed error text on the error/stderr path only —
**no** JSON, no partial-JSON, no traceback on stdout — so a machine consumer
never receives a half-parsed blob.

### 6. `price_usd` is a bare selected field, not a promoted predicate

Every search/semantic block now selects a bare `price_usd` field (after
`is_basic`) so every row can carry a price, and `in_category { name }` is now an
**unconditional** selection (gaining its `@filter(allofterms(name, $cat))` only
when `--category` is active) so every row can carry a category. `price_usd` is
deliberately **not** added to `_PROMOTED_PREDICATES`: that tuple drives ADR-PARAM
`ge`/`le` bracket semantics for query-derived quantities, and `price_usd` is
never query-parsed into a bracket (the ceiling comes from `--max-price` only).
Price and category surface **only** via `--json`/`--sort`; the human Rich table
is unchanged (ADR-0016 Option B — a guard test pins that a price value never
appears in the table).

### 7. Public promotion of two `dql_builder` symbols

`_validate_package` → `validate_package` and `_MAX_FILTER_TERM_LEN` →
`MAX_FILTER_TERM_LEN` are promoted to public names (both added to `__all__`),
with the exact `^[A-Z0-9][A-Z0-9\-]{0,19}\Z` charset, the `128` cap and all logic
**unchanged**. `cli.py`'s two lazy imports now import the public names, so the
CLI no longer reaches for an underscore-private symbol across a module boundary.

> **Note on the word "exact" above.** What this section *decided* — that the
> promotion changed no logic and no charset — remains true exactly as written:
> the rename was byte-identical at the time. What it *quoted* has since moved.
> This section originally read `^[A-Z0-9][A-Z0-9\-]{0,19}$`; the end-anchor was
> later corrected to `\Z` (ADR-0021 § 8's 2026-07-28 amendment) because Python's
> `$` also matches just before a trailing newline. The pattern above is updated
> so "exact" stays a true statement about what is **enforced today**, which is
> how a reader will use it — but the correction was not part of this ADR's
> decision, and is flagged here rather than folded silently into it. The
> **charset is unchanged**; only the anchor is stricter.

## Consequences

- The human and machine consumers share one read-only query path, one filter
  contract (ADR-0016) and one error contract; only the final emission differs.
- `--sort stock`/`--sort price` never widen the query — the row cap
  (`MAX_RESULT_LIMIT`, ADR-0007) still bounds the response; sorting is a stable,
  deterministic re-order within that bounded set.
- `RankedRow` gains two optional fields (`price_usd`, `category`) and a
  `params_dict()` accessor; all existing fields are intact (a regression test
  pins them), so `ParsedQuery` and the ranker's public order are unchanged.
- **Open item (not adopted): a single fixed edge order.** A follow-up proposed
  collapsing the selection edge layout to one fixed `made_by → in_category →
  in_package` order. It is **not** adopted here: the pinned PR 1 filter-window
  tests (AC-SF-15/2/16/18) require the constrained edge to *lead* the selection
  (so an edge's `@filter` clause falls within a short window after the block's
  `@cascade`), which is incompatible with a `made_by`-first layout while
  `_cascade_clause` is untouched. The existing leading-constraints layout is
  retained; unifying it needs those PR 1 tests reconciled first and is deferred.

This ADR **realizes ADR-0016's `--sort`/`--json` deferral** and closes the
sort/JSON scope of issue #15.
