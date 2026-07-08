# ADR-0019: HNSW `exponent: "6"` and the `db check-index` index canary

- Status: Accepted
- Date: 2026-07-08

## Context

The `embedding` predicate declared an HNSW index with only a metric:

```dql
embedding: float32vector @index(hnsw(metric: "cosine")) .
```

With no `exponent` key, the Dgraph HNSW driver falls back to its **default
`exponent: "3"`**. The `exponent` sizes the graph the index builds: Dgraph
derives the per-node level/neighbour budget from it (`maxLevels = exponent`,
`efConstruction = 50 * exponent`, `efSearch = 30 * exponent`), so `"3"` sizes the
structure for roughly `10^3` vectors. The live catalogue holds **613,396**
embedded parts — about `10^5.8` vectors, nearly three orders of magnitude beyond
what the default graph is built to navigate.

The consequence was a **measured recall collapse**: a self-similarity replay
(re-issuing an already-embedded part's OWN stored vector through `similar_to` and
asking whether the part finds itself) succeeded for only **4 of 1000** sampled
parts — the index was too sparse to route a query back to the vector it was built
from. `partgraph search --semantic` degraded accordingly, and nothing in the
stack surfaced it: a bare HTTP `/health` 200 (ADR-0018) reports "Dgraph is alive"
while the vector index is effectively broken.

This is also a **silent regression vector**: `partgraph db apply-schema` posts
`schema/partgraph.dql` verbatim over `/alter`. If the file and the live index
ever disagree on the HNSW options — because the file was fixed but `apply-schema`
was never re-run, or the live index was tuned via `/alter` but the file was not
updated — no existing command notices. A schema that only ever asserted the
driver default could never even distinguish "correctly configured" from "never
configured".

## Decision

### 1. Set the HNSW `exponent` to `"6"`

`schema/partgraph.dql` now declares:

```dql
embedding: float32vector @index(hnsw(metric: "cosine", exponent: "6")) .
```

`"6"` sizes the HNSW graph for the `~10^6` scale the ingested catalogue actually
occupies (613,396 embedded parts ≈ `10^5.8`). Dgraph derives the graph budget
from it (`maxLevels = 6`, `efConstruction = 300`, `efSearch = 180`), restoring
the neighbour connectivity ANN search needs. The embedding **model, dimension
(384) and metric (cosine) are unchanged** — this ADR only sizes the index
structure that ADR-0008 declared; it does not change what is embedded or how.

### 2. Safe index-rebuild procedure (as performed live, 2026-07-07)

Changing an HNSW index spec is not free. The rebuild was performed live in this
sequence, which is the recommended procedure for any future change:

1. **Drop the index spec** (keep the data): `apply-schema` with
   `embedding: float32vector .` (no `@index`). This removes the HNSW structure
   without touching the stored vectors.
2. **Verify no data loss**: `count(has(embedding))` was unchanged at
   **613,396** across the drop.
3. **Re-add the sized index**: `apply-schema` with the
   `hnsw(metric: "cosine", exponent: "6")` spec, triggering a full rebuild.

Observed rebuild at `exponent: "6"`: **15m58s**, **~23 GB peak RAM**, **~5–17
cores busy** on a 32-core / 64 GB host.

**RESOLVED FACT (closes the architecture gate's open question):** changing the
HNSW index spec via `/alter` **DOES trigger a full rebuild** of the existing
index over the already-stored vectors — observed live **twice**: 5m34s at
`exponent: "3"` and 15m58s at `exponent: "6"`. Corollary: re-applying the
corrected schema file against the **already-fixed** live DB is a **no-op** (the
spec already matches), so no rebuild happens on a redundant `apply-schema`.

> **Downtime warning.** A future `partgraph db apply-schema` whose `embedding`
> index spec **differs** from the live one triggers this multi-minute,
> multi-GB rebuild. Plan for the downtime window; it is not an incremental
> operation.

### 3. The `partgraph db check-index` canary

A new leaf, `partgraph.util.index_health.check_index_integrity()`, and a new CLI
command, `partgraph db check-index`, detect exactly the drift and corruption a
`/health` 200 hides. Like `db status` (ADR-0018) it is **engine-independent** —
it never calls a container engine — and talks only to Dgraph's own HTTP `/query`
endpoint (`DGRAPH_QUERY_URL = http://127.0.0.1:8081/query`, `Content-Type:
application/dql`; docs/connecting.md §2.2). Its two checks are computed
**independently** (a schema drift never masks a self-similarity result, and vice
versa — they are different problems):

- **File-vs-live option drift, both directions.** The live HNSW options
  (`schema(pred: [embedding]) {}`) are compared with the schema-file options,
  with a shared **missing-`exponent` → `"3"` normalization** applied to BOTH
  sides. So "file wants `"6"`, live has the default" and "live was bumped to
  `"6"`, file still defaults" are both flagged, while "neither sets it, both
  normalize to `"3"`" correctly compares equal.
- **A read-only self-similarity probe needing no ML model.** The probe fetches
  one already-embedded part (`has(embedding), first: 1`) and replays its OWN
  stored vector through `similar_to(embedding, 5, "[...]")`, asserting the
  part's own uid comes back. This reproduces the recall-collapse symptom
  directly, with **no embedding model loaded** and **no write** — it is purely
  read-only (schema introspection, one selection, one `similar_to`), so it needs
  no teardown.
- **SECURITY (Finding 1): the stored vector is validated before it is inlined.**
  `similar_to` requires a literal vector (it cannot be a bound `$`-variable), so
  the stored value is inlined into the query text. Before that, EVERY element is
  validated with a local `repr(float(x))` + strict `[0-9.eE+-]` `fullmatch`
  formatter (the same validate-before-emit discipline as
  `partgraph.query.dql_builder`, re-implemented locally so the leaf imports no
  builder). A stored value that is not a list, or that carries any non-float
  element (a poisoned or corrupt vector), is reported as a **handled integrity
  failure** — the third HTTP call is **not** issued and the raw value never
  reaches query bytes. The result `message` is always fixed, single-line and
  path-free (no `/`, no raw exception or response-body text), safe to print
  verbatim with Rich `markup=False`.

**SECURITY (Finding 2): defense-in-depth at the CLI.** The leaf deliberately lets
non-`requests` exceptions propagate (no blind `except Exception`; ruff BLE001).
`db check-index` therefore wraps the call in a guard that turns any unexpected
exception into a fixed, path-free error and a clean exit 1 — no traceback reaches
the terminal (mirrors the `db status` guard). The command exits **0 iff**
`reachable and schema_ok and self_similarity_ok in (True, None)` — "nothing
embedded yet" (`None`) counts as passing, not failing — else **1**.

## Relationship to other ADRs

- **ADR-0008 (semantic embeddings)** declared the model, the 384-dim vector, and
  the cosine metric. This ADR only **sizes** the HNSW index those choices feed;
  the model/dim/metric are untouched.
- **ADR-0018 (`db status` health probe)** is untouched. `check-index` is a
  **separate, additive** sibling command: `db status` answers "is Dgraph alive?",
  `db check-index` answers "is the vector index correctly configured and
  functional?". They share the engine-independence and path-free-message
  discipline but no code path.

## Migration

- **In-repo:** none. The schema-file fix plus the new command are additive; no
  data migration is required.
- **Live DB:** already fixed via `/alter` on 2026-07-07 (procedure above), so the
  corrected schema file now **matches** the live index — re-running
  `apply-schema` against it is a no-op. On any environment where `apply-schema`
  has NOT yet been run with the corrected file, `db check-index` will report
  drift until it is (or until the live index is fixed via `/alter`, as was done
  here); running it then closes the drift with the multi-minute rebuild described
  in Decision 2.

## Known residual (measured 2026-07-08, live DB)

The exponent-6 rebuild did **not** yield 100% index membership: a read-only
self-similarity sweep measured **18/300** of the earliest-uid embedded parts and
**10/100** of a spread sample (offsets 10k/100k/300k/600k) missing from the live
HNSW graph — roughly **6-10%** of the catalogue is unreachable via `similar_to`
despite holding valid stored vectors. `db check-index` correctly exits 1 on this
database (its deterministic `first: 1` probe lands on uid `0x1`, which is in the
affected set) — that is the canary doing its job, not a false positive. Live
remediation (targeted re-insertion or a further rebuild) is tracked as follow-up
operations work, outside this ADR's repo-only scope.

## Addendum (2026-07-08): the `first: 1`, `k=5` canary was mostly a false positive

The "Known residual" section above concluded that `db check-index` exiting 1 on the live DB was "the canary doing its job, not a false positive." **A follow-up measurement on 2026-07-08 shows that conclusion was mostly wrong**; this addendum corrects it without rewriting the original record.

Re-measuring the same live 613,396-vector index while varying only the `similar_to` neighbour count `k` isolated a **Dgraph v25.3.4 HNSW early-termination effect**: a part that does not find its own stored vector at `k=5` is routinely reachable at `k=1000`.

- On the earliest-uid band the old probe deterministically selects (`first: 1` → uid `0x1`), self-match was **~3% at k=5** but **~85% at k=1000**.
- Across the whole catalogue only **~4%** of parts are genuinely unreachable even at `k=1000`; that residual concentrates in the same early band, where it shows as the ~15% shortfall below 100% at k=1000.

So the old probe's exit 1 was **dominated by the k=5 early-termination artifact** on the worst-case uid it happens to select — a **false positive** — not by the ~4% genuine residual. The genuine residual and its live remediation remain tracked as follow-up operations work, unchanged and still outside this ADR's repo-only scope; real `partgraph search --semantic` relies on candidate oversampling (ADR-0020), not on k=5.

## Amendment (2026-07-08): multi-sample self-similarity canary

The single-part, `k=5` design in Decision 3 is replaced by a **multi-sample + rate-threshold** canary:

- **Sample `N = _SELF_SIMILARITY_SAMPLE = 30`** parts via one deterministic `has(embedding), first: 30` selection — the conservative earliest-uid band: if the hardest band clears the bar, the rest of the catalogue is better.
- **Replay each at `k = _SELF_SIMILARITY_K = 1000`** (raised from 5): the measured navigable horizon at which the worst band recovers to its ~85% plateau, and below the search path's own `SEMANTIC_CANDIDATE_CAP = 1500` neighbour bound (ADR-0007 / ADR-0020), so it adds no new upper bound.
- **Pass when the self-match rate ≥ `_SELF_SIMILARITY_THRESHOLD = 0.5`** (inclusive). The threshold sits in the wide gap between the healthy worst-band baseline (~85% at k=1000) and the recall-collapse regime this canary exists to catch (the exponent-3 disaster, **4 / 1000 ≈ 0.4%**). At N=30, 0.5 clears the 85% baseline by roughly 5σ (a healthy DB does not false-fail on sampling noise) yet still fires decisively on a real collapse. A threshold near the healthy baseline (e.g. 0.9) is **rejected**: because the selection deterministically samples the worst band, 0.9 would false-fail a healthy DB whose worst band tops out at ~85% — reintroducing the very false positive this amendment removes.
- The result DTO gains **`self_similarity_rate: float | None`** (the measured rate; `None` when unreachable or when nothing is embedded). A per-sample invalid/corrupt stored vector still issues **no** `similar_to` call (the Finding 1 security gate is unchanged) and now counts as one **miss** while probing continues over the remaining samples; a network failure on any call aborts the probe (`reachable = False`) and issues no further call.

The CLI exit formula is unchanged (`0 iff reachable and schema_ok and self_similarity_ok in (True, None)`); `self_similarity_ok = True` now means "rate ≥ threshold".

### DoS bound of the multi-sample canary (ADR-0007 discipline)

The canary is read-only and operator-invoked (`partgraph db check-index`), never on a hot path, but its cost is bounded explicitly, mirroring ADR-0007:

- **Sequential HTTP calls ≤ `N + 2`** (= 32 for N=30): 1 schema introspection + 1 selection + at most one `similar_to` per sampled part. An invalid sample issues no call (fewer, never more); a network timeout/connection error on any call aborts the remainder (`reachable = False`) — the probe never re-hammers a failing endpoint.
- **uid volume ≤ `N × k`** (= 30 × 1000 = 30,000 uid rows) across all `similar_to` responses — bounded because `N` and `k` are fixed module constants and `k` stays under `SEMANTIC_CANDIDATE_CAP`.
- **Worst-case wall-clock ≈ `(N + 2) × INDEX_PROBE_TIMEOUT_S`** (≈ 64 s at N=30, 2.0 s), reached only by a DB that answers every one of the ≤ N+2 calls slowly-but-successfully; each individual call stays bounded by `INDEX_PROBE_TIMEOUT_S = 2.0 s` (never an unbounded wait), and a real timeout aborts early. This up-to-~64 s worst case (vs the old ~6 s) is an accepted trade-off for a statistically robust verdict that no longer false-fails on the k=5 early-termination artifact; revisit via a follow-up ADR if real usage demands it.

Verification for this amendment: `tests/unit/test_index_health.py` AC-IDX-28..36 (multi-sample rate, inclusive threshold boundary, per-sample-invalid-continue, variable denominator, `self_similarity_rate` per branch, mid-loop network abort, pinned message wording, all-invalid edge, and the recall-collapse regression), plus the amended DTO-shape and single-part (M=1) ACs — all hermetic.

## Non-goals

- **`apply_schema` diffing / auto-rebuild.** `apply-schema` still posts the file
  verbatim; it does not diff the live index or gate the rebuild. `db check-index`
  is the out-of-band detector, not an automatic remediator.
- **PR D scope.** Ranker / `dql_builder` / `search` changes are out of scope here;
  this ADR covers only the `exponent` fix and the read-only canary command.

## Verification

Specified test-first in `tests/unit/test_index_health.py` (AC-IDX-4..22 plus the
poisoned/non-list security negatives) and `tests/unit/test_cli_check_index.py`
(AC-IDX-23..27 plus the unexpected-exception guard), all hermetic — an injected,
order-scripted `http_post` fake; no real socket, sleep, or wall-clock read. The
schema-file fix is pinned by `tests/unit/test_schema_file.py::AC-IDX-3`. Two
read-only integration checks (`tests/integration/test_index_health_live.py` and
the reworked `tests/integration/test_gate_pr4.py`) exercise the real HTTP wiring
and semantic recall against production vectors. `ruff check .` is clean and the
full non-integration suite is green.
