# ADR-0020: Hybrid semantic search robustness

- Status: Accepted
- Date: 2026-07-07

## Context

PR D hardens the `partgraph search --semantic` path (introduced in ADR-0008).
Gates 1-3 identified three concrete defects, each with the same underlying
root cause — the semantic path treated Dgraph's `similar_to(embedding, k, …)`
neighbour set as if it were both the final ranking *and* the final page:

1. **No true similarity ranking.** `build_semantic_dql` never selected the raw
   `embedding`, so `rank_results` ordered semantic rows by the *lexical*
   relevance key (tier → `stock>0` → `is_basic` → `mpn_norm` → `uid`). The most
   embedding-similar part was therefore **not** guaranteed to appear first — the
   "semantic" order was really an alphabetic/stock order over an unordered
   neighbour set.

2. **Silent underfill.** The builder asked Dgraph for only `k = --limit`
   neighbours (clamped to `MAX_RESULT_LIMIT`). But the block's server-side
   `@filter(has(datasheet))` (and any `--category`/`--manufacturer`/… filter)
   prunes the neighbour set *after* `similar_to` has already fixed its `k`
   candidates. A `--limit 5` could thus return **fewer than 5** datasheet-backed
   rows even when hundreds exist slightly deeper in the neighbour list — the
   pool was never large enough to refill after pruning.

3. **Misleading empty-result hint.** *Every* empty semantic result printed
   "run `partgraph embed` first", even when the index was fully populated and
   the emptiness was caused purely by an over-narrow filter/limit. The CLI never
   distinguished "no embeddings exist at all" from "filters starved the result".

The shared root cause: the candidate pool, the ranking, and the displayed page
were conflated into one `k`. The fix separates them — a large **candidate**
pool, a client-side **cosine re-rank**, and a small displayed **result** page —
and teaches the empty path to probe before it advises.

## Decision

### 1. Oversample the candidate pool (security F1 — the ADR-0007 follow-up)

The CLI sizes the semantic neighbour count independently of the display limit:

```
candidate_k = min(max(limit * SEMANTIC_OVERSAMPLE_FACTOR,
                      SEMANTIC_CANDIDATE_FLOOR),
                  SEMANTIC_CANDIDATE_CAP)
```

with three new **public** `dql_builder` constants (added to `__all__`):

- `SEMANTIC_OVERSAMPLE_FACTOR = 20` — ask for 20× the display limit so the
  cosine re-rank has a real pool to choose from after filter pruning.
- `SEMANTIC_CANDIDATE_FLOOR = 200` — even a tiny `--limit` fetches a meaningful
  pool.
- `SEMANTIC_CANDIDATE_CAP = 1500` — the hard ceiling on `candidate_k`.

Worked examples: `--limit 5 → 200` (floor); `20 → 400`; `50 → 1000`;
`200 → 1500` (cap); `99999 → 1500` (cap).

**DoS envelope (extends ADR-0007's bounded-constant precedent to the vector
payload).** The worst case is `SEMANTIC_CANDIDATE_CAP` rows each carrying a
384-dim embedding. Raw `float32` that is `1500 × 384 × 4 B ≈ 2.3 MB`; as the
JSON-serialized text Dgraph actually returns over gRPC (each float as a decimal
string) it is `≈ 6 MB`. Both are two orders of magnitude below the finite
`256 MiB` per-message gRPC ceiling (`cli.py` `_GRPC_MAX_MESSAGE_BYTES`, line 78,
ADR-0010). `1500` is thus a deliberately safe cap, not an arbitrary one: a single
request can never stream an unbounded neighbour set or trip `RESOURCE_EXHAUSTED`.

### 2. `SEMANTIC_CANDIDATE_CAP` is the CANDIDATE bound; `MAX_RESULT_LIMIT` stays the RESULT bound

`build_semantic_dql` clamps `k` to `max(1, min(k, SEMANTIC_CANDIDATE_CAP))`
(1500), **not** to `MAX_RESULT_LIMIT`. The builder remains the DoS backstop and
never trusts its caller — even a hostile or un-oversampled `k` is clamped. The
smaller `MAX_RESULT_LIMIT` (200) is unchanged and is still the **RESULT** bound:
the ranker truncates the cosine-reranked result back down to it (§4). The two
bounds are now distinct roles:

| Constant                 | Role            | Value | Enforced by            |
| ------------------------ | --------------- | ----- | ---------------------- |
| `SEMANTIC_CANDIDATE_CAP` | candidate pool  | 1500  | `build_semantic_dql`   |
| `MAX_RESULT_LIMIT`       | displayed page  | 200   | `rank_results` (§4)    |

**Supersedes ADR-0008.** ADR-0008 (§"Inline vector literal, never a
`$`-variable") stated: *"`k` is clamped to `MAX_RESULT_LIMIT` (ADR-0007)."* That
clause is **retired**. It is replaced by: `k` is clamped to
`SEMANTIC_CANDIDATE_CAP`; `MAX_RESULT_LIMIT` continues to bound the *result*,
now applied by the ranker after the client-side re-rank. Everything else in
ADR-0008 (the inline literal, the per-element `_fmt_float` validation, hybrid
filters) is unchanged and still in force.

### 3. Client-side cosine re-rank — stdlib `math` only

`build_semantic_dql` now additionally selects a **bare `embedding` field** (a
hardcoded selection, never a `$`-variable — so it adds no injection surface; the
per-element `_fmt_float` validation of the inline vector literal is untouched).
The lexical `build_search_dql` **never** selects `embedding`, so its response
stays byte-identical (the 384-float vector never travels the lexical path).

`rank_results` gains `query_vector=` and `result_limit=` keyword arguments. When
`query_vector` is given, each semantic-tier row's scalar `RankedRow.similarity`
is set to `cosine(query_vector, raw["embedding"])`; semantic rows are ordered by
cosine **descending** (tie-break `mpn_norm` then `uid`). The raw 384-float vector
is read from the DQL dict, reduced to that one scalar, and **discarded** — it is
never stored on `RankedRow` (a defence-in-depth test asserts no `RankedRow`
field ever holds a length-384 list).

Cosine is computed with **stdlib `math` only** (`math.fsum` + `math.sqrt`). The
ranker sits on the *lexical* search path and must stay importable with the
optional `[embed]` extra (numpy/torch, pulled in transitively by
sentence-transformers) **absent**; a numpy/torch import — at module scope *or*
lazily — would break every lexical `partgraph search` on an install without
`[embed]`. Degenerate cases are total, never raising: a zero-norm query or row
vector yields cosine `0.0` (no `ZeroDivisionError`); a missing/malformed raw
embedding yields `similarity = None` (distinct from a genuine `0.0`) and sorts
last.

`query_vector=None` (the lexical default) leaves the ranker byte-identical to
the pre-semantic contract, and every `similarity` stays `None`. A golden
regression pins that byte-identity.

### 4. Result truncation, and the ranker's private result ceiling

On the semantic path the cosine-ordered rows are truncated to
`_resolve_result_limit(result_limit)` **before** any `--sort stock`/`price`
re-order, so the survivors are always the top-cosine rows and a later
stock/price sort only re-orders *that* set (it never changes *which* rows
survive). The clamp is `max(0, min(result_limit, _MAX_RESULT_ROWS))`: `None` →
the ceiling; a **negative or zero** `--limit` → a clean empty result, never a
negative-index Python slice (`rows[:-5]` would silently keep the first N−5 rows —
security F3).

`ranker._MAX_RESULT_ROWS = 200` is a deliberate **duplicate** of
`dql_builder.MAX_RESULT_LIMIT`, **not** an import of it: the ranker importing its
sibling `dql_builder` would introduce a module-import cycle on the hot lexical
path. An explicit **drift-guard test** asserts
`ranker._MAX_RESULT_ROWS == dql_builder.MAX_RESULT_LIMIT`, so the duplicate can
never silently diverge from the canonical value.

### 5. The empty-result probe — index-populated vs filter-starved

When a semantic search returns **zero rows on the human (non-`--json`) path**,
the CLI issues exactly **one** probe on the **same** client:

```
{ probe(func: has(embedding), first: 1) { uid } }
```

`first: 1` makes it a single-row existence check, never a scan. The decision is
three-way:

- probe finds **≥ 1** embedded part → the index is populated, so the empty
  result is filter/limit **starvation**: print `_FILTER_STARVATION_HINT`
  ("…Try loosening --category, --manufacturer, or raising --limit.") — a
  single-line, **path-free** hint that never mentions `partgraph embed`.
- probe finds **0** → genuinely no embeddings yet: print the existing
  `_NO_EMBEDDINGS_HINT` ("run `partgraph embed` first…").
- probe **raises** → fall back to `_NO_EMBEDDINGS_HINT`, still **exit 0**.

The probe lives inline in `_run_semantic_search` and is wrapped in **its own**
`try/except` so a probe failure can never fall through to the primary query's
`_DB_QUERY_ERROR`/exit-1 handler (security F2). It reuses the search's client and
**never** builds a second one — `_build_dgraph_client` is called exactly once
across the whole search+probe round-trip (security F4). Probe-derived text is
printed with `markup=False` (untrusted for Rich markup).

**`--json` bypasses the probe entirely.** A non-empty result issues no probe; a
`--json` result — empty or not — issues no probe and prints no hint, emitting the
valid empty envelope (`count: 0, results: []`) and exiting 0. This preserves
ADR-0017's contract that `--json` never pays for or prints the human hint
(AC-SF-27).

### 6. Additive JSON `similarity` key — envelope `version` stays 1

`_json_row` gains a 12th key, `similarity`: the scalar cosine on a semantic-tier
row and JSON `null` on every other (lexical) row — present-but-null, so **every**
row carries the same 12-key set. Per ADR-0017's forward-compatibility policy this
is an **additive** key, so the envelope `version` stays **1**. `similarity` is
**JSON-only**: the human Rich table is unchanged and never shows it.

### 7. Out of scope: mixed lexical + semantic-with-`query_vector`

`query_vector` is only ever supplied on the dedicated `--semantic` path, whose
DQL yields a single `semantic` block (no `exact`/`trig`/`fts` rows). A blended
result set that carries *both* lexical-tier rows *and* a `query_vector` cosine
re-rank is therefore **not** a reachable state and is explicitly out of scope;
the cosine re-rank only ever scores semantic-tier rows.

## Consequences

- Semantic results are now genuinely ordered by embedding similarity, and a
  `--limit N` reliably returns N datasheet-backed rows when N exist (the
  oversampled pool refills after filter pruning) — the two functional defects
  are closed and unit-pinned (AC-HY-4/6/7/11/12).
- The empty-result message is now accurate: a populated index that is merely
  filter-starved is told to loosen filters, not to re-run an embed it already
  ran (AC-HY-13/14).
- The lexical path is provably untouched: `build_search_dql` never selects
  `embedding`, `rank_results` with `query_vector=None` is byte-identical, and
  `ranker.py` carries no numpy/torch import (drift/regression/stdlib guards).
- The DoS surface is bounded and documented: `candidate_k ≤ 1500`, worst-case
  `≈ 6 MB` response, far under the `256 MiB` gRPC ceiling — the ADR-0007
  follow-up the vector payload required.
- Trade-off: the empty human path now costs one extra small round-trip (the
  probe). It is bounded (`first: 1`), best-effort (its failure is non-fatal), and
  skipped entirely on the non-empty and `--json` paths.

## ADR numbering

`ADR-0019` is claimed by the in-flight PR #23; this PR is therefore correctly
numbered **ADR-0020**.
