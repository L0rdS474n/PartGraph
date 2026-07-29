# ADR-0024: `search --limit 0` is rejected, not silently rewritten to 1

- Status: Accepted
- Date: 2026-07-29

## Context

Five commands take `--limit`: `ingest jlcparts`, `embed`, `refresh-links`,
`refresh` and `search`. Four of them agreed on what the flag means. The fifth
did not, and the disagreement was invisible from the outside.

The four declare the option as `str | None` and hand the raw text to
`_validate_limit` (`src/partgraph/cli.py`), which parses it and exits 1 with a
fixed `--limit must be a positive integer.` on anything that is not a positive
integer.

`search` declared the option as a Typer `int`. Three facts then compose into a
silent bug:

1. **Click's coercion runs first.** Typer's `int` type means Click parses and
   converts the value before the command body executes. By the time any repo
   code runs, `--limit 0` is already the Python `int` `0`, and `--limit -5` is
   already `-5`. The sign was absorbed before validation could see it.
2. **`_validate_limit` could not apply.** It takes `str | None`. There was no
   string left to give it, so `search` never called it. The validator existed,
   was correct, and was simply unreachable from this one command.
3. **`dql_builder`'s floor masked the result.** `build_search_dql` computes
   `first = max(1, min(int(limit), MAX_RESULT_LIMIT))`. A `0` or a negative
   became `1`. Not an error, not a warning — a full query with `first: 1`,
   exit 0, results printed.

So the same flag, on the same CLI, rejected the same input in four commands and
quietly rewrote it in the fifth. The `max(1, ...)` floor is what turned a
missing check into a *silent* one: without it, `--limit 0` would have produced a
visibly odd query and the divergence would have been found years earlier.

The semantic path had the same hole by a different route:
`_run_semantic_search`'s `candidate_k = min(max(limit * 20, 200), 1500)` turns
`limit=0` into a 200-candidate pool, so `--semantic ... --limit 0` also ran a
full search, encoder invocation included.

## Decision

### 1. `search` calls `_validate_limit`, early, before the branch split

`search` calls `_validate_limit(str(limit))` at the top of its body, in the same
block as `_validate_filter_text_flag(...)` and the other AC-SF validators —
**before** the `if semantic is not None:` split, so the lexical and semantic
paths share one contract, and before any Dgraph client is built, so a rejected
`--limit` never opens a connection or starts a database.

The validator is **called**, not copied. A hand-written local `if limit <= 0:`
with the same literal message would be indistinguishable in output today and
would desync the moment the shared message is edited. One message, one
definition, one place to change it.

The return value is discarded. `limit` is already the parsed `int` from Click;
rebinding it to the validator's result would make this call load-bearing for
more than validation, which is not what it is here for.

### 2. The option stays a Typer `int`. Re-typing it to `str` was measured and rejected

The symmetric-looking fix is to declare `--limit` as `str` like its four
siblings and route everything through `_validate_limit`. **Do not do this.** It
breaks a deliberately pinned behaviour — measured, not reasoned about:

`--limit abc` is rejected by Click's own `int` coercion with **exit 2** and
Click's native `Invalid value for '--limit': 'abc' is not a valid integer.`
Retyping the option to `str` moves that case into `_validate_limit`, which
answers with **exit 1** and *our* message instead. That is a different exit code
and a different string for an input whose existing error was already clear and
correct — and it is pinned by a test that documents non-integer text as out of
scope precisely so this cannot happen quietly.

The `str` shape was applied to `search` as a throwaway edit and the AC-LM suite
run against it, to confirm this rather than assume it. Result: `1 failed, 9
passed` — the one failure being `AC-LM-4 … keeps_clicks_own_error_out_of_scope`,
with `assert 1 == 2` and `Error: --limit must be a positive integer.` where
Click's usage error belonged. The edit was reverted; the reverted file is
byte-identical to the committed one.

The two shapes are genuinely incompatible: you cannot both let Click own
non-integer text and route all of `--limit` through `_validate_limit`. The
hybrid is the deliberate choice — Click owns the *type*, `_validate_limit` owns
the *sign*. This is written down so nobody re-derives the trap by trying the
"obvious" unification a second time.

### 3. What stays untouched

- **The 200 cap.** `MAX_RESULT_LIMIT = 200` (ADR-0007) and `dql_builder`'s
  `min(int(limit), MAX_RESULT_LIMIT)` are unchanged. `--limit 5000` stays exit
  0, silently clamped. `search --help` promises results "stay capped at 200
  regardless of --limit"; a documented cap must never become a rejection, and a
  test pins that it does not.
- **`dql_builder`'s `max(1, ...)` floor.** Kept — and the honest reason matters,
  because the tempting justification is false. `build_search_dql` has **no
  second caller**. Its only two call sites in `src/` are `search` itself and
  `_relaxed_search_pass`, and `_relaxed_search_pass`'s only call site is
  `search` — verified by grep, not assumed. The only other thing that calls the
  builder with a non-positive limit is the test that pins this very floor. After
  this change, therefore, **no input the CLI can produce reaches the floor at
  all**; it is dormant, not load-bearing. It stays for forward safety —
  `build_search_dql` is a public name in `partgraph.query.dql_builder.__all__`
  and must not emit `first: 0` or `first: -5` if a non-CLI caller ever appears —
  not because an existing dependency needs it. This ADR does not claim such a
  caller exists. Removing the floor would be a change to a public module's
  robustness contract, which is a separate objective from making the CLI reject
  the input, so it is not made here.

`search --help` gains the same `Must be a positive integer.` sentence the other
four `--limit` help texts already carry, so the enforced contract and the
documented one match.

## Consequences

### This is a breaking change for `search --limit 0` and negatives

| Invocation | Before | After |
| --- | --- | --- |
| `search MAX232 --limit 0` | exit 0, silently clamped to 1, results printed | exit 1, `--limit must be a positive integer.`, no client built |
| `search MAX232 --limit -5` | exit 0, silently clamped to 1, results printed | exit 1, same message, no client built |
| `search --semantic "..." --limit 0` | exit 0, full search on a 200-candidate pool, encoder invoked | exit 1, same message, encoder never invoked |
| `search --json --limit 0` | exit 0, full JSON envelope | exit 1, no JSON on stdout |
| `search MAX232 --limit 5000` | exit 0, clamped to 200 | **unchanged** |
| `search MAX232 --limit abc` | exit 2, Click's message | **unchanged** |
| `search MAX232 --limit 1` | exit 0 | **unchanged** |

**Who this affects.** Anyone whose script passes `--limit 0` — whether
deliberately, relying on the accidental clamp, or by passing through an
unvalidated variable that happens to be `0` or empty-then-zeroed — gets results
today and will get an exit 1 and no output after this change. There is no
deprecation window; the old behaviour was never specified and was
indistinguishable from `--limit 1`, so there is nothing to keep working. A
caller that wants one result should say `--limit 1`.

`--json` consumers get the existing error contract, unchanged in shape: exit
non-zero, nothing parseable on stdout, matching what `--package` / `--min-stock`
/ an empty query already do.

### Migration

**None required.** No data, schema, config or on-disk format changes; the DQL
emitted for every previously-accepted `--limit` is byte-identical. The only
action anyone needs to take is in *their own* scripts, and only if such a script
passes a non-positive `--limit` to `search` — replace `--limit 0` with the
number of results actually wanted (`--limit 1` reproduces the old behaviour
exactly). This section exists to say that explicitly rather than leave its
absence to be read as an oversight.

### Residual

The five `--limit` options still have two different Typer types, so a reader
comparing declarations sees an asymmetry. That asymmetry is now the *decision*
(§ 2), documented at the validator's docstring and here, rather than an
accident. The behavioural contract — non-positive is rejected with one shared
message — is uniform across all five.
