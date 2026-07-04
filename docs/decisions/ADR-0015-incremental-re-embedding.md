# ADR-0015: Incremental re-embedding (issue #11 PR 4) — content-hash reconcile for `partgraph embed --changed`

- Status: Accepted
- Date: 2026-07-04
- Relates to: ADR-0008 (semantic embeddings), ADR-0010 (embed resource bounds),
  ADR-0011 (embed full-catalogue exhaustion)

> Note on naming: the original semantic-embedding work was labelled "PR4" inside
> ADR-0008/0010/0011. This ADR is the *fourth PR of issue #11* (the refresh
> family), a distinct thread. "PR 4" here always means **issue #11 PR 4**.

## Context

`partgraph embed` (ADR-0008/0010/0011) selects Part nodes *missing* an embedding
(`@filter(NOT has(embedding))`), builds a deterministic embed text per part
(`description + category + package + sorted tags`; `build_embed_text`), encodes it,
and writes an embedding back by uid. It is a one-shot pass: once a part has an
embedding it is never revisited, even if its source text later changes (an ingest
correction, a re-categorisation, a new tag). Over time the semantic index drifts
away from the catalogue it is supposed to describe, and there is no way to refresh
only the parts that actually changed short of wiping and re-embedding everything.

Issue #11 (the refresh family) closes freshness gaps predicate-by-predicate:
datasheet links (PR 1, ADR-0012), stock/price (PR 2, ADR-0013), external
scheduling (PR 3, ADR-0014). PR 4 is the embedding leg: re-embed a part **only
when its embed text changed**, cheaply and idempotently, without a full re-embed of
the catalogue.

Detecting "changed" requires remembering what a part's embedding was computed
*from*. The embed text itself is derived and not stored; storing the whole text per
part is wasteful and still requires a full-text compare. A fixed-width content hash
of the embed text is the minimal fingerprint that answers "did the input change?"
with a single equality check.

## Decision

Stamp a `sha256` content hash of the embed text alongside every embedding, and add
a reconcile pass (`partgraph embed --changed`) that compares a freshly computed
hash against the stored one — client-side — to re-embed only drifted parts.

### D1 — Atomic payload extension: `{uid, embedding}` → `{uid, embedding, embed_text_hash}`

`embed_write`'s per-part mutation payload is extended from exactly
`{"uid", "embedding"}` to exactly `{"uid", "embedding", "embed_text_hash"}`. The
hash is computed in `_build_batch_payload` from the **same `text` object that
produced the vector** for that row (`window` is a list of `(part, text)` pairs
zipped with the aligned `vectors`), so the stamped fingerprint is atomic with the
vector it describes and cannot drift between them.

We deliberately did **not** add a separate "stamp the hashes afterwards" step for
the genuine-embed path (case i). A second pass would re-derive the text (a second
`build_embed_text` call whose result could, in principle, diverge from the one that
was encoded), issue extra writes, and open a window in which a part has an
embedding but no hash for reasons other than "predates this feature". Folding the
hash into the one payload that already carries the vector keeps the invariant
"embedding ⇒ its hash was written from the identical text" true by construction on
the write path.

`compute_embed_text_hash(text) -> sha256 hexdigest` is the **single source** of the
fingerprint. `embed_write` (via `_build_batch_payload`), the reconcile writers
(`stamp_hashes`, `reembed_write`), and the reconcile *comparison* all call it;
`sha256` is never re-inlined at a call site, so the write side and the read side can
never disagree on how the fingerprint is derived.

### D2 — Backfill 4-case reconcile; first run is all-backfill, zero-encode

The reconcile pass classifies each already-embedded part by an
**empty-text precondition first, then a hash-state split** (`_reconcile_page`):

- Precondition: `build_embed_text(part) == ""` → **skip entirely**, in any hash
  state (no encoder call, no write). Mirrors the missing pass's empty-text skip.
- (i)  no embedding → **out of scope here** (the reconcile selection roots on
  `has(embedding)`; the existing missing pass owns case i and now stamps the hash
  atomically per D1).
- (ii) embedding, **no stored hash** → **backfill** `{uid, embed_text_hash}` only
  (`stamp_hashes`; encoder NOT called, embedding untouched).
- (iii) embedding, stored hash **== fresh** → **skip** (no mutate, no encoder).
- (iv) embedding, stored hash **!= fresh** → **re-embed + re-stamp**
  `{uid, embedding, embed_text_hash}` (`reembed_write`; encoder called, new vector
  and new hash).

**First-run guarantee (zero encode):** every part embedded before this feature has
an embedding but no hash, so it is case (ii). The first `embed --changed` over such
a catalogue therefore performs a pure **backfill** — it stamps every part's hash
and calls the encoder **zero** times. Only genuine text drift (case iv) on a
*subsequent* run spends encode budget. A run is **idempotent/self-healing**: once
backfilled, an unchanged part is case (iii) forever after and issues no mutation
(pinned by AC-RE-14: backfill-then-idempotent-rerun).

The two write paths live in `partgraph.embed` (`stamp_hashes`, `reembed_write`),
not inlined in the CLI: the CLI reconcile loop never mutates directly. Both write
by the **already-resolved uid** the reconcile selection read back, with **no
xid round-trip** — reconcile rows are not routed through `embed_write`'s
xid-resolution/eligibility path (which would skip xid-less rows the reconcile pass
must still handle). This keeps the architecture boundary "cli.py orchestrates;
embed.py owns the write contract" intact and gives both writers the same
single-source hash (D1).

### D3 — `--changed` flag: reconcile-then-missing over disjoint partitions

`partgraph embed` gains a `--changed` flag. Without it, behaviour is
**byte-identical to today**: the missing-only pass (`NOT has(embedding)` +
`_embed_all_pages`) alone (pinned by AC-RE-13: plain embed never calls the
reconcile pass). With it, the reconcile pass (`_select_parts_for_reembed` +
`_reembed_all_pages`) runs **first**, then the unchanged missing pass.

The two passes cover the **disjoint partition** of the catalogue:
`has(embedding)` (reconcile) ⊎ `NOT has(embedding)` (missing) = all parts. Running
reconcile first means a part's drift is repaired before the missing pass would even
consider it, and no part is visited by both passes in one invocation. The reconcile
pass reuses the missing pass's uid-keyset-cursor primitives (`_UID_RE`,
`_page_max_uid`, `_EMBED_SELECT_PAGE_SIZE`, the `_EMBED_CURSOR_STALL` notice) — same
pipeline, so it inherits ADR-0010's forward-progress and injection guarantees and
ADR-0011's `remaining: int | None` bounded/unbounded contract rather than copying
them. `--limit N` bounds each pass to N rows; no `--limit` drives both to
exhaustion.

Termination note: the reconcile short-page guard compares a page's yield against
the **next** page's target (`min(remaining, PAGE_SIZE)` after the countdown), not
the current page's request, so a bounded run whose page exactly satisfies the
shrinking budget is not mistaken for an end-of-data short page; for an unbounded run
this reduces to the plain "fewer than a full page" test.

### D4 — `sha256`, index-free predicate, client-side comparison

`embed_text_hash` is declared `string` with **neither `@index` nor `@upsert`**. The
reconcile pass reads the stored value back with the rest of the row and compares it
to a freshly computed hash **in Python**; it never filters or looks up by hash
*value* in DQL (no `eq(embed_text_hash, ...)`, no 64-hex literal in query text —
pinned by AC-RE-7). An index on a high-cardinality content hash that is only ever
read, never queried-by-value, would cost write amplification for no read benefit.
This mirrors the index-free precedent already set by `stock_checked_at`
(ADR-0013) and `verified_at` (ADR-0012), and the schema test ties the invariant to
the schema itself (`embed_text_hash` must carry no `@index`/`@upsert`). `sha256` is
chosen as a ubiquitous, collision-resistant, fixed-64-hex-char digest from the
Python standard library (`hashlib`) — **no new dependency**.

## Consequences

- **AC-EW blast radius (bounded).** Extending the write payload touches exactly
  **three test assertions** (AC-EW-1, AC-EW-3, AC-EC-6 — each now asserts the
  3-key set and the hash oracle) and **three docstrings** (the `embed.py` module
  write-contract, `_build_batch_payload`, and `_write_payload`). The genuine-embed
  path (case i) is otherwise unchanged; AC-RE-2 pins that a no-prior-embedding part
  still calls the encoder exactly once and carries the correct hash.
- **Migration = the backfill.** There is no separate data migration. Applying the
  schema is idempotent (`partgraph db apply-schema` re-runs the same `alter`; a
  new no-index scalar predicate adds nothing to reindex), and the *first*
  `embed --changed` run **is** the migration: it backfills every pre-existing
  embedding's hash with zero encode cost (D2). Until a part is backfilled it simply
  looks like case (ii) again on the next run — self-healing, never wrong.
- **New public surface in `partgraph.embed`.** `compute_embed_text_hash`,
  `stamp_hashes`, `reembed_write` are added to `__all__`; `embed.py` stays
  import-light (stdlib `hashlib` only; `sentence_transformers` remains lazy inside
  `get_encoder`).
- **Cost profile.** A `--changed` run's encode cost is proportional to **drift**,
  not catalogue size: case (ii)/(iii) are pure read/compare/stamp; only case (iv)
  encodes. Backfill and re-stamp reuse the same per-page, per-batch, short-lived-
  commit, load-adaptive-pacing envelope as the missing pass (ADR-0010/0011), so an
  unbounded reconcile has the same flat per-page memory profile.
- **Composition with scheduling (future work).** ADR-0014's external scheduler runs
  the refresh family periodically. Wiring `partgraph embed --changed` into that
  cadence — so drift is reconciled on a schedule alongside links/stock — is a
  natural next step but is **out of scope** here: this PR delivers the on-demand
  reconcile command only, and does not add or modify any scheduler wiring.
