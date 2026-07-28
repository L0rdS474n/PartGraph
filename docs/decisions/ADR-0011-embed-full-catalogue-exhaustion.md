# ADR-0011: A no-`--limit` embed run drives to exhaustion over the whole catalogue

- Status: Accepted
- Date: 2026-07-03
- Supersedes: ADR-0010 (in part — Repeated-runs model)

## Context

`partgraph embed` (PR4, resource-bounded by ADR-0010) selects Part nodes missing
an embedding, encodes them, and writes an `uid+embedding` payload back by uid. It
exposes one knob, `--limit N` ("limit embedding to the first N parts").

ADR-0010 also carried a second, invisible bound: a no-argument run was capped at a
finite `_EMBED_SELECT_DEFAULT = 200_000` default, and the full catalogue
(~800k parts) was expected to be covered by *re-running* the command
("Repeated-runs model"). That default was a poor fit for the command's actual
job. A one-off catalogue embed that silently stops at 200k — printing the same
success line as a complete run — looks finished while leaving the majority of the
catalogue unembedded, and `partgraph search --semantic` then silently misses
those parts. The operator gets no signal that a second, third, ... run is still
required.

Crucially, that default **never bounded any caller-supplied magnitude**. `embed()`
computed `remaining = parsed_limit if parsed_limit is not None else
_EMBED_SELECT_DEFAULT`: an explicit `--limit` was always honoured verbatim and the
default only ever filled in the *no-argument* case. An explicit `--limit 500000`
was already driven through as exactly 500000 — well past the 200_000 default —
never silently downgraded (pinned by AC-EC-9c). The default was therefore not a
safety clamp on untrusted or oversized input; it was only a default magnitude for
the no-argument path, and a misleading one.

## Decision

A no-`--limit` run is **unbounded**: it drives `_embed_all_pages` with
`remaining=None` and pages to exhaustion over every eligible Part. `embed()` now
passes `remaining = parsed_limit` directly (no default substitution), and
`_EMBED_SELECT_DEFAULT` is removed entirely. `--limit N` is unchanged and remains
the only way to bound a run: `remaining == N` exactly, proven up to `N = 500_000`
(AC-EC-9c). `_embed_all_pages`'s `remaining` parameter widens to `int | None`,
where `None` means "no countdown — terminate on data exhaustion alone".

This supersedes ADR-0010's "Repeated-runs model": the full catalogue is covered by
a single no-`--limit` invocation rather than by manually re-running the command
until it stops finding work.

### The resource envelope is unchanged and still finite

Removing the default removes **no** resource bound; every other bound ADR-0010
established still holds, and they are what keep an unbounded run safe:

- **Bounded working set per page.** Selection still fetches at most
  `_EMBED_SELECT_PAGE_SIZE = 10_000` parts per query — never the whole catalogue
  in one response. Each page is selected, embedded, and released before the next
  is fetched, so the per-page memory profile is identical whether `remaining` is a
  finite `int` or `None`; nothing accumulates across pages and an unbounded run's
  resident memory is flat, not growing.
- **Finite transport ceiling.** The shared client stub still carries the
  `_GRPC_MAX_MESSAGE_BYTES = 256 MiB` send/receive ceiling (ADR-0010 Fix A), so no
  single read page or write batch can produce an unbounded gRPC frame.
- **Short-lived per-batch commits.** `embed_write` writes each batch in its own
  `client.txn()` and commits it immediately; no long-running transaction spans the
  run, so an unbounded run holds no growing transaction state.
- **Stateless, load-adaptive pacing.** `ResourceController.regulate` is a pure
  function of `(prev_batch_size, current system snapshot)` — it carries no per-run
  accumulation, so pacing behaves identically on the first batch and the
  ten-thousandth.
- **Deterministic termination is unchanged.** The loop still stops on any of
  (a) a zero-row page, (b) a short page (fewer rows than requested), or (c) a
  cursor that fails to strictly advance (the defensive stall guard, which prints
  the path-free `_EMBED_CURSOR_STALL` notice and breaks). Only condition (d),
  `remaining` reaching 0, is now scoped to bounded runs; an unbounded run has no
  countdown to exhaust and relies on (a)/(b)/(c) — the same conditions that
  already terminated every finite run.

### Trust boundary and residual risk (accepted)

Dgraph is a **same-host, loopback-only** dependency: the gRPC address is the
hardcoded module constant `DGRAPH_GRPC_ADDR = "127.0.0.1:9081"`, with no
environment-variable override, so the client only ever talks to a local Alpha the
same operator started. It is inside the trust boundary, not an untrusted network
peer. Forward progress across pages is enforced by the server-side `after:` keyset
cursor (each page starts strictly past the prior page's max uid), and every uid is
validated against `^0x[0-9a-fA-F]+\Z` before it can reach query text
(validate-before-interpolate; ADR-0010 Fix B), so the cursor is never an injection
vector. (This ADR originally recorded that anchor as `$`; the charset is
unchanged, and only the end-anchor is now stricter — see ADR-0021 § 8's
2026-07-28 amendment.)

We explicitly accept one residual **Low** risk: a compromised or buggy local
Dgraph could keep serving fresh, strictly-advancing, always-eligible pages and so
keep an unbounded client looping. The cost of that pathological case is bounded to
**CPU and wall-clock only** — the per-page memory profile is flat (no
accumulation), the transport frame is capped at 256 MiB, commits are short-lived
per batch, and the loop takes no locks and holds no long-lived transaction. The
non-advancing-cursor stall guard (condition (c)) is the sole backstop for the
narrower "same rows re-served" variant; the "endless distinct eligible rows"
variant is not separately defended, because it requires a subverted in-boundary
dependency and can only waste local compute — never exhaust memory or wedge shared
state. An operator who wants a hard ceiling in an untrusted or experimental
setting still has `--limit N`.

## Consequences

- A single `partgraph embed` (no `--limit`) now embeds the **entire** eligible
  catalogue in one invocation and reports the true total, instead of silently
  stopping at 200,000 and requiring the operator to know to re-run. This closes
  the "looks finished but isn't" gap and keeps `search --semantic` from silently
  missing the unembedded tail.
- `--limit N` behaviour is unchanged and is now the *only* bound on a run; it is
  honoured exactly, uncapped, up to at least 500,000 (AC-EC-9c). The removed
  default never clamped an explicit `--limit`, so no caller-visible bound is lost.
- `_embed_all_pages` accepts `remaining: int | None`; `None` is a first-class
  "unbounded" value that terminates purely on the data-exhaustion conditions
  (a)/(b)/(c). This is unit-pinned by AC-EC-10 (direct-call exhaustion, short
  page, and stall guard under `remaining=None`) and AC-EC-11 (end-to-end CLI
  no-`--limit` exhaustion through a finite mocked catalogue).
- The resource envelope (10k-row pages, 256 MiB transport ceiling, per-batch
  short-lived commits, stateless load-adaptive pacing, flat per-page memory) is
  unchanged; the only bound removed was a magnitude default that never protected
  against oversized input.
- ADR-0010 stays the reference for the gRPC ceiling and the keyset-cursor design;
  only its "Repeated-runs model" section is superseded here, and a forward pointer
  is added there so the history remains traceable.
