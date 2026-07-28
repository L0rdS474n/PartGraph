# ADR-0010: Resource bounds for the embed path (gRPC message ceiling + keyset pagination)

- Status: Accepted
- Date: 2026-07-03

## Context

The `partgraph embed` command (PR4) reads Part nodes from Dgraph, encodes them,
and writes an `uid+embedding` payload back by uid. Two resource behaviours on
this path can make a full-catalogue run either crash or silently waste its
entire budget, and the test engineer pinned both as hardening gaps (AC-EC-7,
AC-EC-8):

1. **gRPC message size.** pydgraph's default gRPC channel caps a single message
   at 4 MiB. A selection page of thousands of parts, or a batch of 384-dim
   vector-literal writes, can exceed 4 MiB and fail mid-run with gRPC
   `RESOURCE_EXHAUSTED`. The shared client factory `_build_dgraph_client()`
   built the stub with pydgraph's defaults, so every caller (embed, stats,
   search, show, load) inherited the 4 MiB ceiling.

2. **Pagination progress.** The original selection query carried neither a filter
   nor a cursor — just `q(func: type(Part), first: N)`. This change introduces
   BOTH a `@filter(NOT has(embedding))` clause and an `after:` keyset cursor,
   because a filter *on its own* is a trap. `partgraph.embed.embed_write` skips
   any part with no `xid` or no embed-text (counted as skipped, never mutated),
   so such a skip-only part never gains an embedding and keeps matching
   `NOT has(embedding)` forever. A "filter, fetch the first N, repeat" loop (the
   filter *without* the cursor) would therefore re-fetch the same skip-only block
   on every page and never advance — the **sticky-skip** failure; `remaining`
   still counts down, so the run terminates having embedded nothing past the
   first skip block. The filter is necessary for *cross-run* idempotency but,
   alone, cannot provide *intra-run* forward progress — hence the cursor is added
   with it, not instead of it.

## Decision

### Fix A — a finite, named, symmetric gRPC message ceiling

`_GRPC_MAX_MESSAGE_BYTES = 256 * 1024 * 1024` (256 MiB) is defined in
`partgraph.cli`, and `_build_dgraph_client()` builds the stub with

```
options=[
    ("grpc.max_receive_message_length", _GRPC_MAX_MESSAGE_BYTES),
    ("grpc.max_send_message_length", _GRPC_MAX_MESSAGE_BYTES),
]
```

- **Finite and named, not `-1`.** grpc accepts `-1` for "unlimited", but an
  unbounded ceiling turns a malformed or hostile response into an out-of-memory
  vector. 256 MiB is a deliberate bound: it comfortably clears the largest
  legitimate embed page / write batch (thousands of 384-float vectors) while
  still capping a single message. This **extends ADR-0007's bounded-constant
  precedent** (`MAX_QUERY_LEN` / `MAX_TOKENS` / `MAX_RESULT_LIMIT`) from the
  query layer to the transport layer — every limit on the path is an explicit
  constant, never "unlimited".
- **Symmetric.** Send and receive share the one ceiling: large reads (selection
  pages) and large writes (vector-literal mutations) both need the headroom, so
  splitting them would only invite an asymmetric surprise.
- **Orthogonal to `MAX_RESULT_LIMIT = 200`.** That DoS bound (ADR-0007) clamps
  how many rows a single interactive *search* block returns. The gRPC ceiling is
  a transport-frame limit on the *embed/load* bulk paths, which legitimately
  move far more than 200 rows per message. They bound different things at
  different layers and do not interact; raising one does not weaken the other.
- **`options` must be a list of 2-tuples.** Real
  `grpc.insecure_channel(addr, options=...)` unpacks each element as
  `(key, value)`; handing it a *dict* iterates the dict's keys (strings) and
  raises `ValueError: too many values to unpack (expected 2)` the moment a
  channel is built. The list-of-2-tuples shape is the only correct one; it is
  pinned both by a shape assertion and by a no-network smoke test that feeds the
  impl's actual `options` into a real `grpc.insecure_channel(...)` (channel
  construction is lazy, so no server is contacted).

### Fix B — a uid keyset cursor for guaranteed forward progress

`_select_parts_for_embed(client, limit, *, after=None)` now issues
`q(func: type(Part), first: N) @filter(NOT has(embedding))` and gains a
keyword-only `after` cursor — the query previously carried neither the filter
nor the cursor. Page 1 (`after is None`) **omits the `after:` clause entirely**,
so the first fetch carries only the filter and is gated by no cursor; later pages
append `after: <uid>`, so Dgraph returns only Part nodes whose uid sorts strictly
after the previous page's max uid. The embed loop:

- tracks the max uid **selected** per page, compared **numerically** via
  `int(uid, 16)` — never a lexicographic string max, because
  `max("0x9", "0x10")` on raw strings wrongly yields `"0x9"` (the character
  `'9'` sorts after `'1'`). The winning uid's original `0x...` string is kept as
  the next cursor;
- **validates every uid** against `^0x[0-9a-fA-F]+\Z` before it can reach query
  text (validate-before-interpolate, mirroring
  `partgraph.query.dql_builder`'s ADR-INJECT convention) — a missing or
  malformed uid is excluded from cursor computation and is never inlined raw
  into DQL (no injection vector via the cursor, no literal `None` cursor)
  *(this ADR originally recorded the anchor as `$`, which in Python also matches
  just before a trailing newline; the charset is unchanged and only the anchor
  is now stricter — see ADR-0021 § 8's 2026-07-28 amendment)*;
- **terminates** on any of: (a) a zero-row page, (b) a short page (fewer rows
  than requested — Dgraph has no more matches), (c) a cursor that fails to
  strictly advance (a defensive guard against a stale/misbehaving server
  re-serving the same rows; it prints an explicit, path-free stall notice before
  breaking), or (d) `remaining` reaching 0.

The filter and the cursor are introduced together and divide the labour:
`@filter(NOT has(embedding))` provides **cross-run idempotency** (a re-run skips
parts a previous run already embedded), while the cursor provides **intra-run
forward progress** (a single run advances past skip-only blocks). Neither
substitutes for the other — the filter cannot skip *within* a run (skip-only
parts keep matching it), and the cursor alone would re-embed, on every run, the
parts an earlier run already finished.

Within a single run the **cursor — not the filter — is what guarantees forward
progress**: it advances past skip-only blocks precisely because it tracks the
max uid *selected*, not the count *embedded*. A full page of skip-only parts
embeds 0 rows yet still moves the cursor past the block, so the next page reaches
the eligible parts behind it.

**Why not a "zero net-new embeddings → break" rule.** A tempting shortcut is to
stop when a page produces zero new embeddings. That is wrong: a full page of
legitimately skip-only parts yields zero embeds while the cursor must still
advance past them to reach the eligible parts further on. "Zero net-new →
break" would stop at the first skip-only block — reintroducing the sticky-skip
failure in a new disguise. It was explicitly rejected in planning.

**Repeated-runs model.** A single `partgraph embed` run bounds its work to
`_EMBED_SELECT_DEFAULT = 200_000` parts (or `--limit N`). The cursor guarantees
each page makes progress, so one invocation cleanly embeds up to that budget and
stops; the full catalogue is covered by **re-running** `partgraph embed`. Each
run naturally resumes at the parts still missing an embedding, because writes
are idempotent `uid+embedding` mutations and the newly-added
`@filter(NOT has(embedding))` clause excludes already-embedded parts. This keeps
a single invocation bounded and
interruptible rather than an unbounded march over the whole ~800k-part
catalogue.

> **Superseded in part by ADR-0011 (2026-07-03).** A no-`--limit` run now drives
> to exhaustion over the whole eligible catalogue in a single invocation
> (`_embed_all_pages` is called with `remaining=None`), rather than stopping at a
> finite default and relying on repeated runs. The `_EMBED_SELECT_DEFAULT =
> 200_000` figure named in this section is **historical**: that constant has been
> removed from `partgraph.cli`, and `--limit N` is now the only bound on a run.
> The gRPC message ceiling (Fix A) and the keyset-cursor design (Fix B) in this
> ADR are unchanged and remain current; only this "Repeated-runs model" is
> superseded. See ADR-0011 for the resource-envelope and trust-boundary analysis.

### Out of scope (documented follow-ups)

Two other pydgraph stubs still use the 4 MiB default and should adopt
`_GRPC_MAX_MESSAGE_BYTES` (or a shared equivalent) in a focused follow-up:

- **`partgraph.schema.apply_schema`** constructs its own
  `pydgraph.DgraphClientStub(grpc_addr)` independently of
  `_build_dgraph_client()`. Schema application moves little data today, so it is
  not urgent, but it should share the ceiling for consistency.
- **the integration fixture `dgraph_pydgraph_client`** in `tests/conftest.py`
  constructs a stub without the ceiling; raising it there would let the
  GATE-PR4 integration tests exercise the same transport bound the CLI uses.

Both are deliberately left out so this change stays single-objective (the embed
command's own resource bounds), rather than silently skipped.

## Consequences

- A full embed run no longer dies with gRPC `RESOURCE_EXHAUSTED` on a large read
  page or write batch, and no longer wastes its budget re-fetching permanently
  skip-only parts; it makes monotonic forward progress and terminates
  deterministically on one of four explicit conditions.
- The 256 MiB ceiling is a single named constant, unit-tested for its value
  (256 MiB), for exceeding the 4 MiB default, and for not being the `-1`
  "unlimited" sentinel; the `options` container shape is smoke-tested against
  the real grpc library, catching the dict-vs-list bug class directly.
- The keyset cursor is injection-safe: uids are validated against a strict
  pattern before interpolation, consistent with the repo's ADR-INJECT
  convention, so no untrusted value ever reaches DQL text.
- Because the ceiling is generous-but-finite rather than unlimited, a
  pathological response is still bounded — the safety property ADR-0007
  established for the query path now holds on the transport path too, and is
  orthogonal to the unchanged `MAX_RESULT_LIMIT = 200`.
- The two remaining 4 MiB stubs (schema apply, integration fixture) are a
  documented, deliberate scope limit to be closed in a follow-up ADR/PR, not an
  oversight.
