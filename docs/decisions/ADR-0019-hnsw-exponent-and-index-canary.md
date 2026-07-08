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
