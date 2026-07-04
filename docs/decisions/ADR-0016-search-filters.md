# ADR-0016: Structured search filters for `partgraph search`

- Status: Accepted
- Date: 2026-07-04

## Context

Issue #15 asks for structured filter flags on `partgraph search` so a user can
narrow results by manufacturer, package, category, stock, JLCPCB basic/extended
tier and price, and have those filters compose with the existing MPN/parametric
search and with `--semantic`. This ADR covers **PR 1 — the filters themselves**.
`--sort`/`--json` are deferred to **PR 2** (see *PR slicing* below).

The new flags are: `--manufacturer`, `--package`, `--category`, `--in-stock`,
`--min-stock N`, `--basic`, `--extended`, `--max-price <USD>`.

Every flag turns untrusted CLI input into a fragment of a DQL query executed
against Dgraph, so each decision below is anchored to a security property and to
facts verified read-only against the live catalogue (127.0.0.1:9081):

- The manufacturer name exists in **three differently-cased forms** in the
  catalogue — `"Texas Instruments"`, `"TEXAS INSTRUMENTS"`, `"texas instruments"`
  — attached to MAX232-family parts. A case-sensitive exact match would silently
  miss two thirds of them.
- `ge(stock, 5.0)` **errors** in Dgraph v25 (the `stock` predicate is an
  integer); `ge(stock, 5)` works. A float stock literal is not merely ugly, it
  breaks the query.
- Category `"RS232 ICs"` is the real category attached to MAX232-family parts;
  packages `"SOIC-16"` / `"PDIP-16"` are non-empty for TI-made MAX232 parts,
  while `"0000"` is charset-valid but matches none of them.

## Decision

### 1. Manufacturer / category — `allofterms` on a bound variable

`--manufacturer` renders `made_by @filter(allofterms(name, $mfr)) { name }` and
`--category` renders `in_category @filter(allofterms(name, $cat)) { name }`. The
user value is bound as the Dgraph `$mfr`/`$cat` variable and **never** inlined,
so hostile characters stay inside the variable value (ADR-INJECT). `allofterms`
gives case-insensitive, all-token recall, which is required to match all three
cased `"Texas Instruments"` nodes from one query. We never use `regexp(` on
user input (it would be both an injection and a ReDoS surface).

Trade-off: `allofterms` matches a **superset** of an exact name — `"Texas"`
alone would match `"Texas Instruments"`. That is the right default for a recall
oriented CLI filter, and it is why package uses exact `eq` instead (next point).

### 2. Package — exact `eq`, one rendering path, collision rejected

`--package` upper-cases its value, re-validates it against the package charset
`^[A-Z0-9][A-Z0-9\-]{0,19}$`, and renders the **exact** `in_package
@filter(eq(name, $pkg))` — byte-identical to the existing query-derived
`parsed.package` path (the builder treats the `package=` kwarg and
`parsed.package` as one "effective package"). A package code is a precise,
finite token, so an exact match is correct and avoids the superset caveat above.

A package may arrive from the query text *or* `--package`, never both: the CLI
rejects the collision with a fixed `package given twice … use only one` error,
after parsing and **before** the Dgraph client is built.

### 3. Stock — validated integer literal via `_fmt_int`

`--min-stock N` renders `ge(stock, <int>)` and `--in-stock` is exactly
`--min-stock 1` (`ge(stock, 1)`). A new `_fmt_int` helper (sibling of
`_fmt_float`) runs `int(value)`, rejects bool / fractional-float / non-integer /
negative input, and re-validates the emitted text against a strict digit charset
before it can reach the query (validate-before-emit). This guarantees a stock
literal is **never** a float — because `ge(stock, 5.0)` is LIVE-CONFIRMED to
error in Dgraph.

### 4. Basic / extended — fixed boolean literal

`--basic`/`--extended` are a tri-state (`None` = no filter) rendering the fixed
literal `eq(is_basic, true)` / `eq(is_basic, false)`. The boolean is a compile
time constant of the flag, never derived from or bound to user text, so there is
nothing to bind and nothing to injection-guard. The two flags are mutually
exclusive (`--basic and --extended … use only one`).

### 5. Price — `le(price_usd, <float>)` via `_fmt_float`

`--max-price <USD>` renders `le(price_usd, <float>)` using the existing,
locale-invariant, charset-validated `_fmt_float`. The CLI parses the value as a
non-negative finite float and rejects anything else.

### 6. Permissive validator + named length cap for free-text terms

Manufacturer/category names legitimately contain spaces and exceed 20 characters
(`"STMicroelectronics"`, `"RS-232 Interface IC"`), so the strict package charset
cannot be reused. A separate permissive validator (`_validate_filter_term`)
rejects empty/whitespace-only terms and enforces a **named** length cap,
`_MAX_FILTER_TERM_LEN = 128` — comfortably above real names yet well under the
pinned 500-char rejection, an ADR-0007-style DoS defence-in-depth bound. The
value is bound as a `$`-variable regardless, so this is a usability + resource
guard, not the injection guard.

### 7. Filters compose on BOTH the lexical and semantic paths — no asymmetry

All filters are threaded as keyword arguments into `build_search_dql` **and**
`build_semantic_dql`; on the semantic side they extend the same
`similar_to(...)` block's `@filter`/nested-edge/`@cascade` clause, never a Python
post-filter. Manufacturer/category/stock/basic/price compose identically on both
paths; package composes on the semantic path via `parsed.package`. The two-pass
lexical search threads the filters into **both** the hard pass and the relaxed
(nearest-match) pass: the structured filters are HARD user constraints, so only
the query-derived parametric quantities relax.

`@cascade` is extended to include `made_by`/`in_category` when those filters are
active (mirroring the existing `in_package` cascade), so a part whose filtered
edge prunes to empty is dropped. `in_category` is selected *conditionally* (only
when `--category` is given, mirroring `in_package`), and `made_by` stays selected
unconditionally, gaining its `@filter` only when `--manufacturer` is given.

Layout note: when a manufacturer/category filter is active, the constrained
edges lead the selection (grouped at the front); the no-filter and package-only
paths keep the historical trailing layout, so their output stays **byte-identical**
to the pre-filter builder (three guard tests pin this, and a fourth pins that a
no-filter call is unchanged). `ParsedQuery` is **not** extended — the filters are
builder keyword arguments, not parser fields (a guard test pins ParsedQuery's
four existing fields).

### 8. Validate before the client is built; fixed, path-free errors

Every new validation runs at the CLI boundary **before** `_build_dgraph_client`
is ever constructed and ahead of the `--semantic` vs non-semantic branch split,
so both paths share one contract and a bad value never opens a connection. Each
error is a **fixed** string that interpolates no exception, path or user value
(so nothing leaks to the terminal), and exits with code 1. The pinned substrings
are: `--package must be`, `package given twice`/`only one`, `--in-stock and
--min-stock`, `--min-stock must be`, `--basic and --extended`, `--max-price must
be`, `--manufacturer must be`, `--category must be`. `--min-stock`/`--max-price`
are accepted as strings and parsed in our code (not by Typer's native
`int`/`float`) so a bad value hits our exit-1 message rather than Click's exit-2
usage error.

### 9. Read-only live gate

The behaviour is proven against the real catalogue by a pure read-only gate,
`tests/integration/test_gate_pr5.py`: it only issues `client.txn(read_only=True)`
queries (never `mutate`), asserts structural/subset properties (case-insensitive
manufacturer recall; a manufacturer+package combo is a non-empty uid-subset of
the manufacturer-only result; a contradictory combo returns empty without
error), and checks the Part count is unchanged before/after.

## Consequences

- One filter contract covers the lexical and semantic paths, so a user gets the
  same narrowing behaviour with or without `--semantic`; no post-filtering means
  the row cap (`MAX_RESULT_LIMIT`, ADR-0007) still bounds the response.
- Manufacturer/category recall is case-insensitive and space-tolerant by design;
  users who need an exact manufacturer match narrow further with `--package` or
  by adding tokens. Package stays exact.
- No untrusted string ever reaches the query text; the only user-controlled
  values are bound `$`-variables, and the stock/price/is_basic literals are
  produced by validate-before-emit helpers. Errors never leak paths.
- `partgraph search` gains eight options and one command surface grows past the
  linter's argument/statement thresholds; these are annotated (`# noqa`) as a
  cohesive, keyword-only command surface rather than split artificially.
- **PR slicing.** This PR delivers the filters only (its body reads *Part of
  #15*). `--sort` and `--json` land in **PR 2**, which will *Close #15*. Keeping
  them separate keeps each PR single-objective and reviewable.
