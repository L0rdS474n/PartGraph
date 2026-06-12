# ADR-0007: Denial-of-service bounds for the search path

- Status: Accepted
- Date: 2026-06-12

## Context

PR3 adds the `partgraph search` and `partgraph show` commands, which accept
arbitrary free-text input from the user and translate it into DQL executed
against Dgraph. Three points on this path can be driven by attacker- or
mistake-controlled input and, if left unbounded, become denial-of-service
(CPU / memory / database-load) vectors:

1. **Query length.** A multi-kilobyte single token (e.g. `"x" * 10000`) fed to
   the tokeniser and to per-token regular expressions risks pathological
   allocation and, with some patterns, super-linear backtracking.

2. **Token count.** A long whitespace-separated stream (e.g. `"10k " * 200`)
   would otherwise produce one parsed token per word, each potentially minting a
   `Quantity` or text variable, inflating the generated DQL and the work done by
   both the builder and Dgraph.

3. **Result limit.** The CLI exposes `--limit`. Without a server-side clamp a
   single request such as `--limit 99999` could stream a large fraction of the
   database in one round-trip, exhausting memory and bandwidth.

These are the "Concern 4" failures identified by the test engineer; the test
suite pins concrete constants that the implementation must satisfy.

## Decision

Three constants bound the search path. They are enforced at the layer that first
sees untrusted input, so callers cannot bypass them:

- **`MAX_QUERY_LEN = 500`** — in `partgraph.query.parser`. The raw query string
  is truncated to 500 characters *before* tokenising. `parse_query` remains a
  total function (never raises) for any input, including arbitrary unicode.

- **`MAX_TOKENS = 10`** — in `partgraph.query.parser`. The parser emits at most
  10 classified tokens in total (`quantities` + `package` + `text_tokens`);
  additional tokens are dropped. This caps the size of the generated query and
  the per-token work.

- **`MAX_RESULT_LIMIT = 200`** — in `partgraph.query.dql_builder`. Every block's
  `first:` clause is `min(caller_limit, 200)`. The caller-supplied `--limit`
  value can never appear verbatim in the query when it exceeds 200; the clamp is
  applied unconditionally at the builder, independent of the CLI default.

500 characters comfortably covers realistic component queries (an MPN plus a
handful of parameter and package tokens) while removing the long-input attack
surface. 10 tokens exceeds the number of independent constraints a meaningful
component search expresses. 200 results is a generous page size for an
interactive CLI and bounds the largest single response.

## Consequences

- The search path is robust against the three identified DoS vectors regardless
  of input size, and the bounds are unit-tested (`test_query_parser.py` A1,
  `test_dql_builder.py` A2).
- Pathological inputs degrade gracefully: oversized queries are truncated and
  over-long token streams are capped, rather than erroring or hanging.
- A user who genuinely needs more than 200 rows must page or narrow the query;
  this is an accepted trade-off for the safety guarantee. The constants live in
  one place each and can be revisited if real usage demands it, via a follow-up
  ADR.
