# ADR-0008: Semantic embeddings, optional model stack, and adaptive pacing

- Status: Accepted
- Date: 2026-06-12

## Context

PR4 adds semantic search: `partgraph search --semantic "<description>"` embeds a
free-text description and ranks parts by vector similarity against the
`embedding` predicate (declared in `schema/partgraph.dql` as
`float32vector @index(hnsw(metric: "cosine"))`). Making this real raises four
decisions that the frozen PR4 tests pin and that need recording:

1. **Which embedding model**, and therefore which vector dimension and distance
   metric the whole pipeline must agree on.
2. **How the heavy model dependency is delivered.** `sentence-transformers`
   pulls in `torch`, which is large and slow to install. CI and the unit suite
   must run without it, yet the production embed path needs it.
3. **Where embeddings are generated.** Embedding the full ~800k-part catalogue on
   CPU is a long, resource-intensive job that does not belong in CI or the
   per-PR pipeline.
4. **How a long embed run paces itself** on a small, shared box (this project is
   developed on a 16 GB / 8-core machine that also hosts the Dgraph container),
   without hard-coding any machine-specific numbers.

## Decision

### Model: all-MiniLM-L6-v2 (384-dim, cosine, CPU)

The embedding model is `all-MiniLM-L6-v2`: 384-dimensional sentence embeddings,
run on CPU, compared with cosine similarity (matching the schema's HNSW index).
384 is pinned as `EMBED_DIM` in both `partgraph.embed` and
`partgraph.query.dql_builder`; every embedding path validates this width and
raises a `ValueError` naming 384 if a vector has the wrong size, so a
mis-configured model fails fast instead of silently producing unusable vectors.

The embedding **text** for a part is `description + category + package + tags`
(tags sorted for determinism). Tags supplement the descriptive fields but never
stand alone: a part with no description/category/package yields an empty embed
text and is skipped, because tags alone carry too little context to be a useful
similarity key.

### `sentence-transformers` is an optional `[embed]` extra; `psutil` is a runtime dep

- `sentence-transformers` lives **only** in the `[embed]` optional extra in
  `pyproject.toml`, never in `[dev]`. A plain `pip install -e ".[dev]"` (and
  therefore CI) never downloads `torch`. It is imported **lazily, inside
  `partgraph.embed.get_encoder()` only** — importing `partgraph.embed`, or
  running the unit suite (which mocks the encoder), never loads `torch`. When the
  extra is absent, `get_encoder()` raises an `ImportError` that names the package
  and the `pip install -e ".[embed]"` command, and is path-free (no filesystem
  path is ever interpolated into the message).

- `psutil` is a **runtime** dependency in `[project.dependencies]`, not optional.
  The adaptive `ResourceController` is active during every embed run — including
  when `sentence-transformers` is absent in tests — and the system reader uses
  `psutil` to read available-RAM. It degrades gracefully: with `psutil` missing,
  `get_system_reader()` still returns a working reader that reports
  `ram_available_fraction=None` (CPU info from `os.cpu_count()` /
  `os.getloadavg()`), and the controller treats an unknown RAM fraction
  conservatively.

### Embeddings are a separate, heavy, one-off run — not in CI/pipeline

Embedding the catalogue is a deliberate, manually-invoked job (`partgraph
embed`), not a step in CI or the per-PR gate pipeline. The embed run is
resumable in spirit (it writes only `uid+embedding`, so re-running is
idempotent), the embeddings persist in the graph, and the wall-clock time is
measured and reported (`Embedded N parts in X.Xs`). The GATE-PR4 integration
tests that exercise the real model are marked `@pytest.mark.integration` and
`pytest.importorskip("sentence_transformers")`, so they skip cleanly wherever the
extra or a running Dgraph is absent.

### Adaptive `ResourceController` with relative thresholds, shared by embed + load

`partgraph.util.resources` is a leaf module (stdlib + optional `psutil` only; it
imports none of `embed`/`query`/`load`/`cli`) providing a `ResourceController`
whose thresholds are **relative**: it decides from the load ratio
`load_avg_1m / cpu_count` and the available-RAM fraction, never from an absolute
core count or byte size. A 4-core and a 64-core box at the same utilisation get
the same decision class. `regulate()` is a pure, deterministic function; readings
and sleeping are injected so tests stay hermetic and never touch the real clock.

The controller is wired into both heavy paths:
- `partgraph.embed.embed_write(..., controller=...)` paces batches during the
  embed run;
- `partgraph.load.loader.Loader(..., controller=...)` gains an **optional,
  additive** `controller` parameter that paces inter-batch work. `controller`
  defaults to `None`, in which case the loader behaves byte-for-byte as before —
  the existing ingest call site and every existing test are unaffected. Pacing
  only ever inserts a pause between batches; it never resizes the deterministic
  batch slices the resume/checkpoint logic depends on.

### Inline vector literal, never a `$`-variable

Dgraph's `similar_to(embedding, k, "[...]")` requires a literal vector, so
`build_semantic_dql` inlines it. To keep that injection-safe, **every** vector
element is forced through `_fmt_float` (`repr(float(x))` validated against a
strict `[0-9.eE+-]` charset) before it reaches the query string; a hostile
non-float element raises `ValueError`/`TypeError`. The human semantic query text
is never part of the DQL — only the validated float vector is inlined — and `k`
is clamped to `MAX_RESULT_LIMIT` (ADR-0007). Hybrid parametric/package filters
reuse PR3's existing injection-safe helpers.

## Consequences

- CI and `pip install -e ".[dev]"` stay light: no `torch`, no model download.
  Unit tests mock the encoder and pass with the extra absent; the no-import
  contract is verified (neither `torch` nor `sentence_transformers` enters
  `sys.modules` during the unit run).
- Semantic results are honestly labelled `[Semantic]` in the results table so a
  fuzzy embedding hit can never be mistaken for an exact part-number match,
  consistent with the PR3 nearest-match banner.
- The embed run is safe to start on a shared, memory-constrained box: it paces
  itself to system pressure using only relative thresholds, so the behaviour
  carries over to any machine size without re-tuning.
- A consumer who genuinely needs semantic search must opt into the heavy extra
  (`pip install -e ".[embed]"`) and run `partgraph embed` once; this is the
  accepted trade-off for keeping the default install and CI fast. The model
  choice can be revisited via a follow-up ADR if real similarity results call for
  it (the dimension is centralised in one constant per module).
