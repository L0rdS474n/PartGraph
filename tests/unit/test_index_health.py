"""
Tests: AC-IDX-4..36 — partgraph.util.index_health (leaf module) +
`partgraph db check-index` vector-index integrity gate (ADR-0019 and the
multi-sample-probe rewrite that hardens it).

Specifies the behaviour of the leaf module ``partgraph.util.index_health``,
which lets `partgraph db check-index` answer a question `db status` cannot:
not just "is Dgraph alive" but "does the LIVE hnsw vector-index configuration
on the `embedding` predicate actually match what schema/partgraph.dql
declares, and does a SAMPLE of already-embedded parts' own stored vectors,
replayed through `similar_to`, still find themselves" — catching a
schema/live drift (e.g. `apply-schema` never re-run after an exponent bump)
or a corrupted/rebuilding vector index that a bare `/health` 200 would never
reveal.

MULTI-SAMPLE-PROBE REWRITE (this file, AC-IDX-28..36 plus amendments to
AC-IDX-12/14/15/16/17 and the two SECURITY tests): the ORIGINAL ADR-0019
single-sample probe (`first: 1`, `similar_to(embedding, 5, ...)`) measured
only 4-of-1000 self-similarity recall in production — a single sample was too
weak a canary; one lucky/unlucky draw could flip the whole verdict either
way. The probe now samples UP TO 30 embedded parts (`first: 30`) and replays
EACH through a much wider `similar_to(embedding, 1000, ...)` (K raised from 5
to 1000 — K=5 was itself too narrow to reliably re-find a part in a large
HNSW graph even when the index is healthy), reporting a PASS RATE across the
sample rather than one hit/miss.

Ratified contract (leaf module ``partgraph.util.index_health`` — the module
ALREADY EXISTS from the earlier ADR-0019 single-sample PR, so importing it
succeeds; this file's NEW/AMENDED assertions below are the correct
test-first RED state for an EXISTING module gaining new behaviour — expect
runtime failures, not a collection-time ModuleNotFoundError: AssertionError
where a query still says "first: 1" / "similar_to(embedding, 5," instead of
"first: 30" / "similar_to(embedding, 1000,"; TypeError where
``IndexIntegrityResult`` does not yet accept a ``self_similarity_rate=``
keyword; AttributeError where a result built by the not-yet-rewritten
``check_index_integrity`` has no ``.self_similarity_rate`` attribute — until
Gate 3 implements it):
  - ``DGRAPH_QUERY_URL: str`` = "http://127.0.0.1:8081/query" — Dgraph's HTTP
    DQL query endpoint (documented in docs/connecting.md section 2.2:
    ``POST``, ``Content-Type: application/dql``), distinct from
    ``partgraph.util.health.DGRAPH_HTTP_HEALTH_URL``. UNCHANGED by this
    rewrite.
  - ``INDEX_PROBE_TIMEOUT_S: float`` = 2.0 — a finite, named, bounded
    timeout (mirrors ``HEALTH_PROBE_TIMEOUT_S`` / ADR-0007's bounded-constant
    precedent). UNCHANGED.
  - ``DEFAULT_EXPONENT: str`` = "3" — the Dgraph hnsw driver default applied
    when neither the schema file nor a live predicate's index_specs carries
    an explicit ``exponent`` key, so "not configured" and "configured to the
    documented default" compare equal rather than spuriously drifting.
    UNCHANGED.
  - ``parse_file_hnsw_options(schema_text: str) -> tuple[tuple[str, str],
    ...]`` — PURE: extracts the ``hnsw(...)`` options for the ``embedding:``
    predicate line out of schema-FILE text, normalizes a missing
    ``exponent`` to ``("exponent", DEFAULT_EXPONENT)``, and returns a SORTED
    tuple of ``(key, value)`` pairs. UNCHANGED (AC-IDX-4/5).
  - ``parse_live_hnsw_options(schema_json: dict) -> tuple[tuple[str, str],
    ...] | None`` — PURE: takes the FULL parsed JSON body of a live DQL
    ``schema(pred: [embedding]) {}`` response (i.e. exactly what
    ``response.json()`` returns — ``{"data": {"schema": [...]}}``), applies
    the SAME default-exponent normalization, and returns ``None`` when the
    ``embedding`` predicate is absent (including an empty ``schema`` array)
    OR its ``index_specs`` carries no ``hnsw`` entry. UNCHANGED (AC-IDX-6/7).
  - Three internal, NON-EXPORTED constants size the probe. They are never
    imported by this file (mirrors this file's existing convention of
    asserting the literal query text rather than importing a private
    constant); their effect is asserted via the outgoing query text and the
    computed rate instead:
      - ``_SELF_SIMILARITY_SAMPLE = 30`` — the selection query becomes
        ``{ q(func: has(embedding), first: 30) { uid embedding } }`` (always
        REQUESTS 30, regardless of how many rows the response actually
        returns).
      - ``_SELF_SIMILARITY_K = 1000`` (raised from 5) — every per-sample
        replay becomes ``similar_to(embedding, 1000, "[...]")``.
      - ``_SELF_SIMILARITY_THRESHOLD = 0.5`` — INCLUSIVE: a computed
        ``rate >= 0.5`` PASSES.
  - ``@dataclass(frozen=True) class IndexIntegrityResult`` with EXACTLY
    SEVEN fields: ``reachable: bool``, ``schema_ok: bool | None``,
    ``file_options: tuple[tuple[str, str], ...]``,
    ``live_options: tuple[tuple[str, str], ...] | None``,
    ``self_similarity_ok: bool | None``, ``self_similarity_rate: float |
    None`` (NEW — inserted between ``self_similarity_ok`` and ``message``),
    ``message: str``. NO field carries a default: every RETURN PATH inside
    ``check_index_integrity`` must set all seven explicitly, so an
    incomplete construction is a loud ``TypeError`` at that call site, never
    a silently-defaulted ``None``.
  - ``def check_index_integrity(*, schema_text: str,
    url=DGRAPH_QUERY_URL, timeout=INDEX_PROBE_TIMEOUT_S, http_post=None) ->
    IndexIntegrityResult`` — ALL FOUR parameters are keyword-only (mirrors
    ``probe_health``'s discipline; UNCHANGED). ``http_post`` is the
    INJECTABLE SEAM (defaults to a LAZILY-imported ``requests.post`` so this
    leaf never imports ``requests`` eagerly just by being imported), invoked
    EXACTLY as ``http_post(url, data=..., headers=..., timeout=timeout)`` and
    must return an object exposing ``.status_code`` and ``.json()``. Flow:
      1. Query the live schema (``schema(pred: [embedding]) {}``); a
         connection/timeout failure here short-circuits the WHOLE probe —
         ``reachable=False``, ``schema_ok=None``, ``live_options=None``,
         ``self_similarity_ok=None``, ``self_similarity_rate=None`` — and NO
         further HTTP call is made. UNCHANGED.
      2. Compare the live options (``parse_live_hnsw_options``) against the
         file options (``parse_file_hnsw_options(schema_text)``) ->
         ``schema_ok`` (``True``/``False`` once the live schema query
         SUCCEEDED, independent of whether it matched; never ``None`` in
         that case). UNCHANGED.
      3. ONE selection call, ``{ q(func: has(embedding), first: 30) { uid
         embedding } }``. Let ``M`` be the number of rows THIS query
         actually returns (``M`` may be less than 30 — the request always
         asks for 30; the response controls how many actually come back).
         If ``M == 0``, ``self_similarity_ok`` and ``self_similarity_rate``
         are both ``None`` and NO further call is made.
      4. For EACH of the ``M`` returned rows, IN ORDER: validate its stored
         vector via the SAME ``_safe_vector_literal`` security gate as
         before (element-by-element ``repr(float(x))`` + strict-charset
         ``fullmatch``).
           - VALID: issue ONE ``similar_to(embedding, 1000, "[...]")`` call
             replaying THAT row's own vector; count a HIT iff that row's own
             uid appears in the result set, else a MISS. (Exactly one HTTP
             call per valid row.)
           - INVALID (not a list, or any element fails the float-literal
             gate): count a MISS and issue NO ``similar_to`` call for that
             row — CONTINUE to the next row. The row still counts toward the
             ``M`` denominator; it contributes zero HTTP calls and an
             automatic miss (it is never excluded from ``M`` altogether).
         Total HTTP calls for a fully-completed probe = 2 (schema +
         selection) plus the number of VALID rows among the ``M`` returned.
      5. ``self_similarity_rate = hits / M`` (a ``float``) and
         ``self_similarity_ok = (self_similarity_rate >= 0.5)`` — the
         threshold comparison is INCLUSIVE, so a rate of EXACTLY 0.5 passes.
      6. A ``requests.exceptions.Timeout`` / any other
         ``requests.exceptions.RequestException`` on ANY call — schema,
         selection, OR any mid-loop ``similar_to`` replay — aborts the WHOLE
         probe exactly like a first-call failure: ``reachable=False``,
         ``schema_ok=None``, ``live_options=None``, ``self_similarity_ok=
         None``, ``self_similarity_rate=None``, and NO further HTTP call is
         made (``file_options`` stays populated — it is computed purely from
         ``schema_text``, no network needed).
      7. Every returned ``message`` is a fixed, single-line, path-free
         string ('/' never appears; a raw exception string is never
         interpolated). The self-similarity clause now reports the sample
         and the integer-floor percent (``percent = (100 * hits) // M``),
         using the phrase "N of M" and NEVER a raw "N/M" fraction:
           - PASS (rate >= 0.5): "...the self-similarity probe passed (28 of
             30 sampled parts, 93%, found their own vector at k=1000)." —
             e.g. hits=28, M=30 (floor(100*28/30) == 93).
           - FAIL (rate < 0.5): "...the self-similarity probe failed (only 3
             of 30 sampled parts, 10%, found their own vector at k=1000)." —
             e.g. hits=3, M=30 (floor(100*3/30) == 10).
           - NONE (M == 0, UNCHANGED): "...no embedded parts yet, so the
             self-similarity probe was skipped."
    Any exception that is NOT a ``requests.exceptions.RequestException``
    (e.g. a programming error in an injected seam) PROPAGATES — never a
    blind ``except Exception`` (ruff BLE001). UNCHANGED.
  - ``partgraph db check-index`` (Gate 4, tested separately in
    tests/unit/test_cli_check_index.py — which duck-types ONLY the four
    fields it reads, ``reachable``/``schema_ok``/``self_similarity_ok``/
    ``message``, via ``types.SimpleNamespace``, so it is UNAFFECTED by
    ``self_similarity_rate`` being added and needs NO change) calls
    ``check_index_integrity(schema_text=load_schema(SCHEMA_FILE))`` with
    ZERO overrides and exits ``0`` iff ``reachable and schema_ok and
    self_similarity_ok in (True, None)``, else ``1``. UNCHANGED.

This file mirrors tests/unit/test_health.py's hermetic style throughout:
Given/When/Then docstrings, an injected-seam ``_FakeHttpPost`` spy (never a
real socket), and no sleep/real-clock/randomness anywhere.
``check_index_integrity`` now issues UP TO 32 sequential POSTs (2 + up to 30
per-sample replays), so ``_FakeHttpPost`` is SCRIPTED with an ORDERED
sequence of outcomes (one per call) rather than a single fixed result, and
raises a clear ``AssertionError`` if the leaf calls it more times than were
scripted — an unscripted extra call is exactly the kind of "keeps hammering
an unreachable/exhausted seam" bug this suite must catch, not silently
tolerate by recycling the last outcome. The multi-sample fixture builders
below (``_build_n_row_selection``, ``_build_k_of_n_replay``) generate
DETERMINISTIC, distinct per-row uids/vectors from a plain integer index — no
``random`` module, no real clock — so a scripted "K of N hit" pattern is
exactly reproducible run to run.

Determinism / IO discipline: every HTTP outcome below is injected; nothing in
this file opens a socket, sleeps, or reads the wall clock. The pure parser
tests (AC-IDX-4..7) use INLINE literal schema-file snippets rather than
reading the real (mutable, Gate-4-owned) schema/partgraph.dql — the real
file's content is separately pinned by tests/unit/test_schema_file.py's own
AC-IDX-3 (schema-file-pin) test; this file tests the PARSING ALGORITHM against
representative text, independent of whichever exact schema/partgraph.dql
revision happens to be checked out.
"""

from __future__ import annotations

import dataclasses
import inspect
import math
import re
import subprocess
import sys
from collections.abc import Sequence

import pytest
import requests

from partgraph.util.index_health import (
    DEFAULT_EXPONENT,
    DGRAPH_QUERY_URL,
    INDEX_PROBE_TIMEOUT_S,
    IndexIntegrityResult,
    check_index_integrity,
    parse_file_hnsw_options,
    parse_live_hnsw_options,
)

# ---------------------------------------------------------------------------
# Fakes — an injectable, ORDER-SCRIPTED `http_post` seam that never opens a
# real socket. Modeled on tests/unit/test_health.py's _FakeHealthResponse /
# _FakeHttpGet, extended to script a SEQUENCE of outcomes (one per call) since
# check_index_integrity issues up to three sequential POSTs.
# ---------------------------------------------------------------------------

class _FakeIndexResponse:
    """Minimal fake HTTP response exposing ``.status_code`` and ``.json()``.

    ``payload`` (already-parsed JSON, e.g. ``{"data": {"schema": [...]}}``) is
    returned verbatim by ``.json()``.
    """

    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeHttpPost:
    """Injectable ``http_post`` seam scripted with an ORDERED outcome sequence.

    Each call pops the NEXT scripted outcome — a ``_FakeIndexResponse``
    (returned) or an ``Exception`` (raised) — so the three-step flow (schema
    query, has(embedding) query, similar_to query) can be scripted
    deterministically per test. Records every call's ``(url, kwargs)`` so
    tests can assert exactly what was forwarded (``data=``/``headers=``/
    ``timeout=``) and how many calls were actually made. Calling this beyond
    the scripted sequence raises ``AssertionError`` with a clear message,
    rather than silently reusing the last outcome — an unscripted extra call
    is a real bug (e.g. re-querying after a fatal failure, or querying
    similar_to when there was nothing to embed) that must fail loudly.
    """

    def __init__(self, outcomes: Sequence[_FakeIndexResponse | Exception]) -> None:
        self._outcomes: list[_FakeIndexResponse | Exception] = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, url: str, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        if not self._outcomes:
            raise AssertionError(
                f"_FakeHttpPost called {len(self.calls)} time(s) — more than "
                "the scripted outcome sequence provided. check_index_integrity "
                "issued an unexpected extra HTTP call."
            )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _payload_texts(fake_post: _FakeHttpPost) -> list[str]:
    """Return the ``data=`` kwarg of every call, as a list of strings."""
    return [str(call["kwargs"].get("data", "")) for call in fake_post.calls]


# ---------------------------------------------------------------------------
# Fixture data — RESOLVED FACTS verbatim (live schema JSON response shapes)
# plus representative schema-FILE text snippets. Both the "current" (missing
# exponent, defaults to "3") and "fixed" (exponent "6") variants mirror the
# EXACT schema/partgraph.dql line text (current and Gate-4 target).
# ---------------------------------------------------------------------------

# A minimal, realistic multi-line schema-file snippet, with a decoy predicate
# BEFORE the embedding declaration, mirroring the real file's surrounding
# context (a comment line + a neighbouring predicate) — proves the parser
# targets the `embedding:` line specifically, not merely "the first hnsw/
# @index text found anywhere".
_FILE_TEXT_DEFAULT_EXPONENT = (
    'attr_value_num: float @index(float) .\n'
    '\n'
    '# Sentence-embedding vector for semantic similarity search.\n'
    'embedding:      float32vector @index(hnsw(metric: "cosine")) .\n'
    '\n'
    'stock:          int @index(int) .\n'
)

# Gate-4 target line (schema/partgraph.dql, post-fix): adds exponent: "6".
_FILE_TEXT_EXPONENT_6 = (
    'attr_value_num: float @index(float) .\n'
    '\n'
    '# Sentence-embedding vector for semantic similarity search.\n'
    'embedding:      float32vector @index(hnsw(metric: "cosine", exponent: "6")) .\n'
    '\n'
    'stock:          int @index(int) .\n'
)

# --- Live DQL `schema(pred: [embedding]) {}` response shapes (RESOLVED FACTS) ---

# BEFORE (drifted/default): no exponent key in the live index_specs options.
_LIVE_SCHEMA_BEFORE = {
    "data": {
        "schema": [
            {
                "predicate": "embedding",
                "type": "float32vector",
                "tokenizer": ['hnsw("metric":"cosine")'],
                "index_specs": [
                    {"name": "hnsw", "options": [{"key": "metric", "value": "cosine"}]}
                ],
            }
        ]
    }
}

# AFTER (fixed): live exponent explicitly set to "6", matching the file fix.
_LIVE_SCHEMA_AFTER = {
    "data": {
        "schema": [
            {
                "predicate": "embedding",
                "type": "float32vector",
                "tokenizer": ['hnsw("exponent":"6","metric":"cosine")'],
                "index_specs": [
                    {
                        "name": "hnsw",
                        "options": [
                            {"key": "metric", "value": "cosine"},
                            {"key": "exponent", "value": "6"},
                        ],
                    }
                ],
            }
        ]
    }
}

# Metric mismatch (exponent matches at "6" on both sides; only metric differs).
_LIVE_SCHEMA_METRIC_MISMATCH = {
    "data": {
        "schema": [
            {
                "predicate": "embedding",
                "type": "float32vector",
                "tokenizer": ['hnsw("exponent":"6","metric":"euclidean")'],
                "index_specs": [
                    {
                        "name": "hnsw",
                        "options": [
                            {"key": "metric", "value": "euclidean"},
                            {"key": "exponent", "value": "6"},
                        ],
                    }
                ],
            }
        ]
    }
}

# Edge cases: predicate absent / no index_specs / empty index_specs / non-hnsw index.
_LIVE_SCHEMA_EMPTY_ARRAY = {"data": {"schema": []}}

_LIVE_SCHEMA_PREDICATE_ABSENT = {
    "data": {
        "schema": [
            {"predicate": "xid", "type": "string", "index": True, "tokenizer": ["exact"]}
        ]
    }
}

_LIVE_SCHEMA_NO_INDEX_SPECS_KEY = {
    "data": {"schema": [{"predicate": "embedding", "type": "float32vector"}]}
}

_LIVE_SCHEMA_EMPTY_INDEX_SPECS_LIST = {
    "data": {
        "schema": [
            {"predicate": "embedding", "type": "float32vector", "index_specs": []}
        ]
    }
}

_LIVE_SCHEMA_INDEX_SPECS_WITHOUT_HNSW = {
    "data": {
        "schema": [
            {
                "predicate": "embedding",
                "type": "float32vector",
                "index_specs": [{"name": "term", "options": []}],
            }
        ]
    }
}

# --- has(embedding) / similar_to response shapes (RESOLVED FACTS: block "q") ---

_OWN_UID = "0x15e80"
_OTHER_UID = "0x15e99"
_STORED_VECTOR = [0.1, 0.2, 0.3]  # deliberately short/simple: the probe replays
# it verbatim without a dimension check (see the "unexpected vector length"
# edge-case test below), and simple decimals keep str()/repr() formatting
# predictable for the data-payload substring assertions.

_HAS_EMBEDDING_ONE_PART = {
    "data": {"q": [{"uid": _OWN_UID, "embedding": _STORED_VECTOR}]}
}
_HAS_EMBEDDING_NONE = {"data": {"q": []}}

_SIMILAR_TO_INCLUDES_OWN_UID = {
    "data": {"q": [{"uid": _OWN_UID}, {"uid": _OTHER_UID}]}
}
_SIMILAR_TO_EXCLUDES_OWN_UID = {
    "data": {"q": [{"uid": _OTHER_UID}, {"uid": "0x15ea1"}]}
}

# --- Poisoned/corrupt stored-vector fixtures (SECURITY, ADR-0019 Finding 1) ---
# A hostile string that, if inlined RAW into the similar_to literal, would close
# the literal and the func()/block and smuggle in a second, attacker-chosen query
# block. The leaf MUST validate the stored vector element-by-element and refuse to
# issue the third call at all — never interpolate this text into query bytes.
_POISON_VECTOR_ELEMENT = '"])) { q2(func: type(Part)) { uid } }'
_HAS_EMBEDDING_POISONED_LIST = {
    "data": {"q": [{"uid": _OWN_UID, "embedding": [0.1, _POISON_VECTOR_ELEMENT, 0.3]}]}
}
# Corrupt shape: the stored `embedding` is not a list at all (a bare string).
_HAS_EMBEDDING_NON_LIST_VECTOR = {
    "data": {"q": [{"uid": _OWN_UID, "embedding": "not-a-vector"}]}
}

#: Strict float charset the outgoing similar_to literal elements must match
#: (mirrors the leaf's own validate-before-emit charset; a poisoned value fails it).
_STRICT_FLOAT_CHARSET = re.compile(r"[0-9.eE+\-]+")


def _resp(payload: dict) -> _FakeIndexResponse:
    """Shorthand: a healthy (HTTP 200) fake response wrapping *payload*."""
    return _FakeIndexResponse(200, payload)


def _assert_message_clean(message: object) -> None:
    """Assert *message* is a non-empty, single-line, path-free string.

    The path-free/single-line guarantee is the leaf's message-hygiene contract:
    ``message`` is always safe to print verbatim, so it never carries a '/'-bearing
    path or an embedded newline, and is never empty.
    """
    assert isinstance(message, str) and message, f"message must be non-empty; got {message!r}"
    assert "/" not in message, f"message must be path-free (no '/'); got {message!r}"
    assert "\n" not in message, f"message must be single-line; got {message!r}"


def _extract_similar_to_literal(query_text: str) -> str:
    """Return the inner comma-separated contents of the similar_to vector literal.

    Extracts ``0.1, 0.2, 0.3`` from ``similar_to(embedding, 1000, "[0.1, 0.2,
    0.3]")`` so a test can assert EVERY element (not just a chosen fixture
    component) is strictly numeric — a stronger guarantee than ``str(x) in
    text``. The regex itself is K-agnostic (``\\d+``), so it works unchanged
    for both this file's legacy K=5 fixtures (now unused after the AC-IDX-12
    amendment below) and the current K=1000 multi-sample fixtures.
    """
    match = re.search(
        r'similar_to\(embedding,\s*\d+,\s*"\[(.*?)\]"\)', query_text
    )
    assert match, f"could not locate the similar_to vector literal in {query_text!r}"
    return match.group(1)


# ---------------------------------------------------------------------------
# Multi-sample fixture builders (AC-IDX-28..36) — an "N-row" selection-
# response builder (optionally "poison-at-index-i"), and a "K-of-N-hit"
# scripted-replay builder. Both are PURE and DETERMINISTIC: every uid/vector
# is derived from a plain integer index, never from `random` or the real
# clock, so a scripted "15 of 30 hit" pattern is byte-for-byte reproducible.
# ---------------------------------------------------------------------------

def _uid(index: int) -> str:
    """Return a distinct, deterministic fake uid for sample *index* (0-based).

    A DIFFERENT numeric range (0x2000+) than the module-level _OWN_UID
    (0x15e80) / _OTHER_UID (0x15e99) fixtures above, so a multi-sample test
    can never accidentally collide with those single-sample fixtures.
    """
    return f"0x{0x2000 + index:x}"


def _vector(index: int) -> list[float]:
    """Return a distinct, deterministic, VALID stored vector for sample
    *index*.

    Three simple decimals (mirrors the module-level ``_STORED_VECTOR``
    convention above) so ``repr()`` formatting stays predictable for the
    literal-content assertions, while still being unique per *index* so a
    per-sample similar_to call can be matched back to "its own" row's
    vector.
    """
    return [
        round(0.01 * (index + 1), 4),
        round(0.02 * (index + 1), 4),
        round(0.03 * (index + 1), 4),
    ]


def _build_n_row_selection(n: int, *, poison_at: int | None = None) -> dict:
    """Build an N-row ``has(embedding), first: 30``-style selection response.

    Row *i* (``0 <= i < n``) gets ``uid=_uid(i)`` and a distinct, VALID
    stored vector ``_vector(i)`` — UNLESS ``i == poison_at``, in which case
    that ONE row's stored ``embedding`` is instead the same hostile-string-
    in-a-list DQL-injection shape as the single-sample
    ``_HAS_EMBEDDING_POISONED_LIST`` fixture above (a poisoned/invalid
    vector at a caller-chosen index; every OTHER row stays valid).
    ``poison_at=None`` (the default) means every row is valid.
    """
    rows = []
    for i in range(n):
        embedding = [0.1, _POISON_VECTOR_ELEMENT, 0.3] if i == poison_at else _vector(i)
        rows.append({"uid": _uid(i), "embedding": embedding})
    return {"data": {"q": rows}}


def _similar_to_outcome_for(index: int, *, hit: bool) -> _FakeIndexResponse:
    """A similar_to outcome for sample *index*: HIT includes ``_uid(index)``
    in the result set; MISS excludes it (mirrors ``_SIMILAR_TO_INCLUDES_OWN_
    UID`` / ``_SIMILAR_TO_EXCLUDES_OWN_UID`` above, generalized to an
    arbitrary sample index).
    """
    rows = [{"uid": _uid(index)}, {"uid": _OTHER_UID}] if hit else [{"uid": _OTHER_UID}]
    return _resp({"data": {"q": rows}})


def _build_k_of_n_replay(
    n: int, hits: int, *, skip: frozenset[int] = frozenset()
) -> list[_FakeIndexResponse]:
    """Build a scripted "K of N hit" similar_to outcome sequence.

    Iterates sample indices ``0..n-1`` in order; any index in *skip* (a
    poisoned/invalid row from ``_build_n_row_selection``) is OMITTED
    entirely — no HTTP call, hence no outcome, is issued for it. Of the
    REMAINING (non-skipped) indices, the FIRST *hits* (in index order) are
    scripted as HITS and the rest as MISSES — a deterministic, unambiguous
    pattern shared by the threshold (AC-IDX-29), mid-loop-failure
    (AC-IDX-33), message-wording (AC-IDX-34), and recall-collapse
    (AC-IDX-36) tests. Raises ``AssertionError`` immediately (a
    fixture-authoring bug, not a production-code bug) if *hits* exceeds the
    number of non-skipped rows.
    """
    outcomes: list[_FakeIndexResponse] = []
    hit_budget = hits
    for i in range(n):
        if i in skip:
            continue
        is_hit = hit_budget > 0
        if is_hit:
            hit_budget -= 1
        outcomes.append(_similar_to_outcome_for(i, hit=is_hit))
    assert hit_budget == 0, (
        f"_build_k_of_n_replay: hits={hits} exceeds the {len(outcomes)} "
        f"non-skipped row(s) available for n={n}, skip={skip!r}."
    )
    return outcomes


def _floor_percent(hits: int, total: int) -> int:
    """Return the integer floor percent ``(100 * hits) // total``.

    Mirrors the leaf's own message-formatting formula exactly (integer floor
    division, never a float ``round()``), so a test's expected percent is
    computed the SAME way the production message text is.
    """
    return (100 * hits) // total


# ===========================================================================
# CONTRACT — module constants, IndexIntegrityResult shape, signature
# ===========================================================================

def test_dgraph_query_url_constant_matches_documented_endpoint() -> None:
    """CONTRACT: Given DGRAPH_QUERY_URL is the single source of truth for
    Dgraph's HTTP DQL query endpoint.
    When the constant is read directly.
    Then it equals "http://127.0.0.1:8081/query" — the exact endpoint
    documented in docs/connecting.md section 2.2 (`POST`, `Content-Type:
    application/dql`), distinct from partgraph.util.health's `/health` URL.
    """
    assert DGRAPH_QUERY_URL == "http://127.0.0.1:8081/query"


def test_index_probe_timeout_constant_is_a_finite_bounded_float() -> None:
    """CONTRACT: Given INDEX_PROBE_TIMEOUT_S is the module's named request
    timeout.
    When the constant is read directly.
    Then it is a finite float strictly greater than zero (mirrors
    HEALTH_PROBE_TIMEOUT_S / ADR-0007's bounded-constant precedent) — never an
    unbounded/None timeout.
    """
    assert isinstance(INDEX_PROBE_TIMEOUT_S, float)
    assert math.isfinite(INDEX_PROBE_TIMEOUT_S)
    assert INDEX_PROBE_TIMEOUT_S > 0


def test_default_exponent_constant_is_the_string_three() -> None:
    """CONTRACT: Given DEFAULT_EXPONENT names the Dgraph hnsw driver default
    applied when no explicit exponent is configured.
    When the constant is read directly.
    Then it equals the STRING "3" (not the int 3 — hnsw option values are
    always JSON/DQL string literals, e.g. {"key": "exponent", "value": "3"}).
    """
    assert DEFAULT_EXPONENT == "3"
    assert isinstance(DEFAULT_EXPONENT, str)


def test_index_integrity_result_dataclass_has_exact_contract_fields_and_is_frozen() -> None:
    """CONTRACT: Given IndexIntegrityResult is the DTO returned by
    check_index_integrity().
    When it is instantiated.
    Then it exposes EXACTLY the SEVEN pinned fields (reachable, schema_ok,
    file_options, live_options, self_similarity_ok, self_similarity_rate,
    message) and is frozen — a consumer (e.g. cli.py's `check-index` command)
    cannot mutate a result. self_similarity_rate (float | None) was added by
    the multi-sample-probe rewrite; the field SET is asserted here (every
    construction site in this suite and in cli.py uses keyword arguments
    exclusively, so declaration ORDER is not itself a behavioural contract —
    see test_index_integrity_result_every_field_is_mandatory_no_defaults
    below for the "no defaults" guarantee).
    """
    result = IndexIntegrityResult(
        reachable=True,
        schema_ok=True,
        file_options=(("exponent", "6"), ("metric", "cosine")),
        live_options=(("exponent", "6"), ("metric", "cosine")),
        self_similarity_ok=True,
        self_similarity_rate=1.0,
        message="ok",
    )
    field_names = {f.name for f in dataclasses.fields(result)}
    assert field_names == {
        "reachable",
        "schema_ok",
        "file_options",
        "live_options",
        "self_similarity_ok",
        "self_similarity_rate",
        "message",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.schema_ok = False  # type: ignore[misc]


#: Every IndexIntegrityResult field, with a representative value each — the
#: complete kwargs dict test_index_integrity_result_every_field_is_mandatory_
#: no_defaults below omits ONE key at a time from.
_ALL_RESULT_KWARGS: dict[str, object] = {
    "reachable": True,
    "schema_ok": True,
    "file_options": (("exponent", "6"), ("metric", "cosine")),
    "live_options": (("exponent", "6"), ("metric", "cosine")),
    "self_similarity_ok": True,
    "self_similarity_rate": 1.0,
    "message": "ok",
}


@pytest.mark.parametrize("omitted_field", sorted(_ALL_RESULT_KWARGS))
def test_index_integrity_result_every_field_is_mandatory_no_defaults(omitted_field) -> None:
    """CONTRACT: Given the ratified multi-sample-probe contract's "no field
    defaults — every construction site sets all seven" rule.
    When IndexIntegrityResult is constructed with exactly ONE required
    keyword omitted (parametrized over each of the seven fields in turn,
    including the new self_similarity_rate).
    Then construction raises TypeError for EVERY field — proving none of the
    seven carries a silently-applied default that could mask an incomplete
    construction site (e.g. a code path that forgets to set the new
    self_similarity_rate field on some return branch).
    """
    kwargs = {k: v for k, v in _ALL_RESULT_KWARGS.items() if k != omitted_field}
    with pytest.raises(TypeError):
        IndexIntegrityResult(**kwargs)


def test_check_index_integrity_parameters_are_keyword_only() -> None:
    """CONTRACT: Given check_index_integrity's parameters are declared
    keyword-only (`*, schema_text, url=..., timeout=..., http_post=...`).
    When the signature is inspected.
    Then every one of schema_text/url/timeout/http_post is KEYWORD_ONLY, so a
    future refactor cannot silently reorder them into positional arguments.
    """
    sig = inspect.signature(check_index_integrity)
    for name in ("schema_text", "url", "timeout", "http_post"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"check_index_integrity's {name!r} parameter must be keyword-only."
        )


def test_check_index_integrity_schema_text_has_no_default() -> None:
    """CONTRACT: Given schema_text is the one REQUIRED argument (there is no
    sensible default DQL schema text to fall back to).
    When the signature is inspected.
    Then schema_text carries NO default value.
    """
    sig = inspect.signature(check_index_integrity)
    assert sig.parameters["schema_text"].default is inspect.Parameter.empty


def test_check_index_integrity_signature_defaults_match_module_constants() -> None:
    """CONTRACT: Given check_index_integrity's `url`/`timeout` keyword
    defaults.
    When the signature is inspected (never calling the function with no
    injected http_post, which would open a real socket).
    Then they equal DGRAPH_QUERY_URL / INDEX_PROBE_TIMEOUT_S exactly.
    """
    sig = inspect.signature(check_index_integrity)
    assert sig.parameters["url"].default == DGRAPH_QUERY_URL
    assert sig.parameters["timeout"].default == INDEX_PROBE_TIMEOUT_S


def test_check_index_integrity_http_post_defaults_to_none_for_lazy_requests_import() -> None:
    """CONTRACT: Given http_post defaults to None so the real `requests`
    module is resolved lazily only when a probe actually runs.
    When the signature is inspected.
    Then http_post's default is exactly None.
    """
    sig = inspect.signature(check_index_integrity)
    assert sig.parameters["http_post"].default is None


def test_check_index_integrity_returns_an_index_integrity_result_instance() -> None:
    """CONTRACT: Given check_index_integrity() completes (any outcome).
    When it returns.
    Then the return value is always an IndexIntegrityResult instance — never
    None, a dict, or a bare tuple.
    """
    fake_post = _FakeHttpPost(
        [_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NONE)]
    )
    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )
    assert isinstance(result, IndexIntegrityResult)


def test_check_index_integrity_default_url_is_the_single_source_of_truth_constant() -> None:
    """CONTRACT: Given check_index_integrity() is called without an explicit
    `url=`.
    When the injected http_post spy records every call.
    Then EVERY call (schema, has(embedding), similar_to) targets
    DGRAPH_QUERY_URL exactly — Dgraph's HTTP DQL query endpoint serves all
    three query shapes (schema introspection, plain query, similar_to), so
    there is only ever one URL, no independently hard-coded string anywhere.
    """
    fake_post = _FakeHttpPost(
        [
            _resp(_LIVE_SCHEMA_AFTER),
            _resp(_HAS_EMBEDDING_ONE_PART),
            _resp(_SIMILAR_TO_INCLUDES_OWN_UID),
        ]
    )
    check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)
    assert len(fake_post.calls) == 3
    assert all(call["url"] == DGRAPH_QUERY_URL for call in fake_post.calls)


def test_check_index_integrity_forwards_a_custom_url_override_to_every_call() -> None:
    """CONTRACT: Given check_index_integrity() is called with an explicit,
    non-default `url=`.
    When the injected http_post spy records every call.
    Then that exact custom URL — not the module default — is forwarded on
    EVERY call, not just the first.
    """
    custom_url = "http://127.0.0.1:9999/query"
    fake_post = _FakeHttpPost(
        [
            _resp(_LIVE_SCHEMA_AFTER),
            _resp(_HAS_EMBEDDING_ONE_PART),
            _resp(_SIMILAR_TO_INCLUDES_OWN_UID),
        ]
    )
    check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, url=custom_url, http_post=fake_post
    )
    assert len(fake_post.calls) == 3
    assert all(call["url"] == custom_url for call in fake_post.calls)


def test_check_index_integrity_calls_seam_with_url_positional_and_kwargs() -> None:
    """CONTRACT: Given the injectable http_post seam.
    When check_index_integrity() invokes it.
    Then it is called as `http_post(url, data=..., headers=..., timeout=
    timeout)` — url positional, data/headers/timeout as keywords — exactly
    the calling convention real `requests.post` supports, so the
    `http_post=None -> requests.post` default swap is safe.
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NONE)])
    check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)
    assert len(fake_post.calls) == 2
    for call in fake_post.calls:
        assert "data" in call["kwargs"]
        assert "headers" in call["kwargs"]
        assert "timeout" in call["kwargs"]


def test_check_index_integrity_content_type_header_is_application_dql() -> None:
    """CONTRACT (API boundary stability): Given Dgraph's HTTP `/query`
    endpoint requires `Content-Type: application/dql` for a raw DQL body
    (docs/connecting.md section 2.2 — this is not optional; Dgraph rejects an
    unrecognized content type).
    When check_index_integrity() issues its queries.
    Then every call's headers carry a `content-type` (case-insensitive key)
    whose value contains "application/dql" (case-insensitive) — so a
    real Dgraph instance actually accepts the request body.
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NONE)])
    check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    for call in fake_post.calls:
        headers = call["kwargs"].get("headers") or {}
        lowered = {str(k).lower(): str(v).lower() for k, v in headers.items()}
        content_type = lowered.get("content-type", "")
        assert "application/dql" in content_type, (
            f"Expected a Content-Type header containing 'application/dql'. "
            f"Got headers: {headers!r}"
        )


# ===========================================================================
# AC-IDX-4..7 — pure parsers
# ===========================================================================

def test_ac_idx_4_parse_file_hnsw_options_normalizes_missing_exponent() -> None:
    """AC-IDX-4: Given schema-file text whose embedding predicate declares
    only metric (the CURRENT schema/partgraph.dql line, no exponent key).
    When parse_file_hnsw_options() is called.
    Then it returns the sorted tuple (("exponent", "3"), ("metric",
    "cosine")) — the missing exponent is normalized to the literal default
    "3", not omitted.
    """
    result = parse_file_hnsw_options(_FILE_TEXT_DEFAULT_EXPONENT)
    assert result == (("exponent", "3"), ("metric", "cosine"))


def test_ac_idx_5_parse_file_hnsw_options_reads_explicit_exponent() -> None:
    """AC-IDX-5: Given schema-file text whose embedding predicate declares
    both metric and exponent: "6" (the Gate-4 TARGET schema/partgraph.dql
    line).
    When parse_file_hnsw_options() is called.
    Then it returns the sorted tuple (("exponent", "6"), ("metric",
    "cosine")).
    """
    result = parse_file_hnsw_options(_FILE_TEXT_EXPONENT_6)
    assert result == (("exponent", "6"), ("metric", "cosine"))


def test_ac_idx_6_parse_live_hnsw_options_normalizes_missing_exponent() -> None:
    """AC-IDX-6: Given the live schema JSON BEFORE fixture (RESOLVED FACT: no
    exponent key in index_specs.options).
    When parse_live_hnsw_options() is called with the FULL parsed response
    body (`{"data": {"schema": [...]}}`, exactly what `.json()` returns).
    Then it returns the sorted tuple (("exponent", "3"), ("metric",
    "cosine")) — normalized identically to the file-side default.
    """
    result = parse_live_hnsw_options(_LIVE_SCHEMA_BEFORE)
    assert result == (("exponent", "3"), ("metric", "cosine"))


def test_ac_idx_7_parse_live_hnsw_options_reads_explicit_exponent() -> None:
    """AC-IDX-7: Given the live schema JSON AFTER fixture (RESOLVED FACT:
    exponent explicitly "6").
    When parse_live_hnsw_options() is called.
    Then it returns the sorted tuple (("exponent", "6"), ("metric",
    "cosine")).
    """
    result = parse_live_hnsw_options(_LIVE_SCHEMA_AFTER)
    assert result == (("exponent", "6"), ("metric", "cosine"))


@pytest.mark.parametrize(
    "live_schema_json",
    [
        pytest.param(_LIVE_SCHEMA_EMPTY_ARRAY, id="empty_schema_array"),
        pytest.param(_LIVE_SCHEMA_PREDICATE_ABSENT, id="predicate_absent"),
        pytest.param(_LIVE_SCHEMA_NO_INDEX_SPECS_KEY, id="no_index_specs_key"),
        pytest.param(_LIVE_SCHEMA_EMPTY_INDEX_SPECS_LIST, id="empty_index_specs_list"),
        pytest.param(_LIVE_SCHEMA_INDEX_SPECS_WITHOUT_HNSW, id="index_specs_without_hnsw"),
    ],
)
def test_parse_live_hnsw_options_returns_none_when_predicate_or_hnsw_absent(
    live_schema_json,
) -> None:
    """EDGE CASE (parser level): Given a live schema response in which the
    `embedding` predicate is entirely absent (including an empty `schema`
    array) OR present but carrying no `hnsw` index_specs entry (missing key,
    empty list, or a DIFFERENT index type only).
    When parse_live_hnsw_options() is called.
    Then it returns None — never raises, never fabricates a default tuple.
    """
    assert parse_live_hnsw_options(live_schema_json) is None


# ===========================================================================
# AC-IDX-8..11 — schema comparison (via check_index_integrity; there is no
# separate exposed "compare" function in the ratified contract). Each test
# scripts exactly two calls (schema + has(embedding)->empty) to isolate
# schema_ok from the self-similarity probe.
# ===========================================================================

def test_ac_idx_8_schema_drift_file_wants_exponent_live_has_default() -> None:
    """AC-IDX-8 (drift, direction 1): Given the file declares exponent "6"
    but the LIVE predicate has no exponent (defaults to "3").
    When check_index_integrity() is called.
    Then schema_ok is False, and file_options/live_options reflect the
    mismatch (both normalized, both real tuples — not None).
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_BEFORE), _resp(_HAS_EMBEDDING_NONE)])
    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )
    assert result.reachable is True
    assert result.schema_ok is False
    assert result.file_options == (("exponent", "6"), ("metric", "cosine"))
    assert result.live_options == (("exponent", "3"), ("metric", "cosine"))
    _assert_message_clean(result.message)


def test_ac_idx_9_schema_drift_file_default_live_has_explicit_exponent() -> None:
    """AC-IDX-9 (drift, direction 2 — the reverse of AC-IDX-8): Given the file
    declares no exponent (defaults to "3") but the LIVE predicate has been
    bumped to exponent "6" (e.g. `apply-schema` was run against a newer
    schema file than what's on disk NOW).
    When check_index_integrity() is called.
    Then schema_ok is False.
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NONE)])
    result = check_index_integrity(
        schema_text=_FILE_TEXT_DEFAULT_EXPONENT, http_post=fake_post
    )
    assert result.reachable is True
    assert result.schema_ok is False
    assert result.file_options == (("exponent", "3"), ("metric", "cosine"))
    assert result.live_options == (("exponent", "6"), ("metric", "cosine"))
    _assert_message_clean(result.message)


def test_ac_idx_10_schema_drift_metric_mismatch_exponent_matches() -> None:
    """AC-IDX-10 (metric mismatch): Given both sides agree on exponent "6",
    but the LIVE metric is "euclidean" while the file wants "cosine".
    When check_index_integrity() is called.
    Then schema_ok is False — a metric-only mismatch is caught just as
    reliably as an exponent-only mismatch.
    """
    fake_post = _FakeHttpPost(
        [_resp(_LIVE_SCHEMA_METRIC_MISMATCH), _resp(_HAS_EMBEDDING_NONE)]
    )
    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )
    assert result.schema_ok is False
    assert result.live_options == (("exponent", "6"), ("metric", "euclidean"))


@pytest.mark.parametrize(
    ("file_text", "live_schema_json", "case_id"),
    [
        pytest.param(
            _FILE_TEXT_EXPONENT_6, _LIVE_SCHEMA_AFTER, "explicit_exponent_matches",
        ),
        pytest.param(
            _FILE_TEXT_DEFAULT_EXPONENT, _LIVE_SCHEMA_BEFORE, "both_default_via_normalization",
        ),
    ],
)
def test_ac_idx_11_schema_ok_true_when_file_and_live_options_are_equal(
    file_text, live_schema_json, case_id,
) -> None:
    """AC-IDX-11 (equal -> ok): Given the file and live hnsw options are
    equal — either because BOTH explicitly set exponent "6", or because
    NEITHER sets it and both normalize to the SAME default "3".
    When check_index_integrity() is called.
    Then schema_ok is True in both cases — proving the default-exponent
    normalization is applied consistently on BOTH the file-parsing and the
    live-parsing side (a one-sided normalization bug would make the
    "both_default" case wrongly drift).
    """
    fake_post = _FakeHttpPost([_resp(live_schema_json), _resp(_HAS_EMBEDDING_NONE)])
    result = check_index_integrity(schema_text=file_text, http_post=fake_post)
    assert result.schema_ok is True, f"case={case_id}: expected schema_ok True"
    assert result.file_options == result.live_options


# ===========================================================================
# AC-IDX-12..22 — probe end-to-end (check_index_integrity's full flow)
# ===========================================================================

def test_ac_idx_12_healthy_end_to_end_schema_ok_and_self_similarity_ok() -> None:
    """AC-IDX-12 (M=1 degenerate case of the multi-sample probe): Given (1)
    the live schema matches the file (exponent "6" both sides), (2) the
    `first: 30` selection query happens to return exactly ONE embedded part
    (M=1 — fewer than 30 embedded parts exist), and (3) re-issuing its stored
    vector through similar_to returns a result set CONTAINING its own uid.
    When check_index_integrity() is called.
    Then reachable/schema_ok/self_similarity_ok are all True,
    self_similarity_rate is exactly 1.0 (1 hit / 1 sampled), and the message
    is a non-empty, single-line, path-free string. Also pins the exact DQL
    text of all three calls (API boundary stability): the schema
    introspection query, the `has(embedding), first: 30` selection (the
    REQUEST always asks for 30 regardless of how many rows come back), and a
    `similar_to(embedding, 1000, ...)` call whose vector components are the
    part's OWN stored vector.
    """
    fake_post = _FakeHttpPost(
        [
            _resp(_LIVE_SCHEMA_AFTER),
            _resp(_HAS_EMBEDDING_ONE_PART),
            _resp(_SIMILAR_TO_INCLUDES_OWN_UID),
        ]
    )
    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )

    assert result.reachable is True
    assert result.schema_ok is True
    assert result.self_similarity_ok is True
    assert result.self_similarity_rate == 1.0
    assert isinstance(result.message, str) and result.message
    assert "\n" not in result.message
    assert "/" not in result.message

    assert len(fake_post.calls) == 3
    texts = _payload_texts(fake_post)
    assert texts[0].strip() == "schema(pred: [embedding]) {}", (
        f"Expected the exact live-schema introspection query text. Got: {texts[0]!r}"
    )
    assert "has(embedding)" in texts[1]
    assert "first: 30" in texts[1]
    assert "uid" in texts[1]
    assert "embedding" in texts[1]
    assert "similar_to(embedding, 1000," in texts[2]
    for component in _STORED_VECTOR:
        assert str(component) in texts[2], (
            f"Expected the part's own stored vector component {component!r} "
            f"to be re-issued verbatim in the similar_to call. Got: {texts[2]!r}"
        )

    # Literal-safety (SECURITY, ADR-0019): beyond "each component appears", assert
    # EVERY comma-separated element of the outgoing similar_to literal matches the
    # strict float charset. A poisoned element could incidentally satisfy
    # `str(x) in text`, but never this fullmatch on the joined literal contents.
    literal_body = _extract_similar_to_literal(texts[2])
    for element in (piece.strip() for piece in literal_body.split(",")):
        assert _STRICT_FLOAT_CHARSET.fullmatch(element), (
            f"similar_to literal element {element!r} is not strictly numeric; "
            f"literal was {literal_body!r}"
        )


def test_ac_idx_13_drift_does_not_prevent_self_similarity_check() -> None:
    """AC-IDX-13: Given the live schema DRIFTS from the file (schema_ok will
    be False), but an embedded part still exists and its self-similarity
    check succeeds.
    When check_index_integrity() is called.
    Then schema_ok is False AND self_similarity_ok is True — the two checks
    are computed INDEPENDENTLY; a schema drift never short-circuits or masks
    the self-similarity result (a corrupted index and a merely-outdated
    schema are different problems and must be independently diagnosable).
    """
    fake_post = _FakeHttpPost(
        [
            _resp(_LIVE_SCHEMA_BEFORE),
            _resp(_HAS_EMBEDDING_ONE_PART),
            _resp(_SIMILAR_TO_INCLUDES_OWN_UID),
        ]
    )
    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )
    assert result.schema_ok is False
    assert result.self_similarity_ok is True
    assert len(fake_post.calls) == 3
    _assert_message_clean(result.message)


def test_ac_idx_14_self_similarity_fails_when_own_uid_absent_from_results() -> None:
    """AC-IDX-14 (M=1 degenerate case of the multi-sample probe): Given the
    schema matches, the `first: 30` selection returns exactly ONE embedded
    part (M=1), but re-issuing its OWN stored vector through similar_to
    returns a result set that does NOT contain that part's own uid (a
    corrupted/stale vector index — the part cannot even find itself).
    When check_index_integrity() is called.
    Then self_similarity_ok is False and self_similarity_rate is exactly 0.0
    (0 hits / 1 sampled).
    """
    fake_post = _FakeHttpPost(
        [
            _resp(_LIVE_SCHEMA_AFTER),
            _resp(_HAS_EMBEDDING_ONE_PART),
            _resp(_SIMILAR_TO_EXCLUDES_OWN_UID),
        ]
    )
    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )
    assert result.schema_ok is True
    assert result.self_similarity_ok is False
    assert result.self_similarity_rate == 0.0
    _assert_message_clean(result.message)


def test_ac_idx_15_no_embedded_parts_yields_self_similarity_none_and_no_third_call() -> None:
    """AC-IDX-15: Given the schema matches but Dgraph has NO embedded parts
    at all (`has(embedding)` returns an empty row set — e.g. `partgraph
    embed` has never been run).
    When check_index_integrity() is called.
    Then self_similarity_ok AND self_similarity_rate are both None (there is
    nothing to self-check) and EXACTLY TWO HTTP calls were made — the leaf
    never issues a third, meaningless similar_to("[]"...) call when there is
    no stored vector to replay.
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NONE)])
    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )
    assert result.schema_ok is True
    assert result.self_similarity_ok is None
    assert result.self_similarity_rate is None
    assert len(fake_post.calls) == 2, (
        "check_index_integrity must not issue a similar_to call when "
        f"has(embedding) found nothing. Calls made: {len(fake_post.calls)}"
    )


def test_ac_idx_16_connection_error_on_first_call_is_unreachable() -> None:
    """AC-IDX-16: Given the injected http_post raises requests.ConnectionError
    on the VERY FIRST call (the live schema query).
    When check_index_integrity() is called.
    Then reachable is False, schema_ok/live_options/self_similarity_ok/
    self_similarity_rate are all None, EXACTLY ONE HTTP call was attempted
    (no further calls after the first failure), and the message is a fixed,
    path-free string naming `partgraph db up` that contains NEITHER the raw
    exception text NOR a '/'. file_options is still populated (it is
    computed PURELY from schema_text, no network needed).
    """
    exc = requests.ConnectionError(
        "HTTPConnectionPool(host='127.0.0.1', port=8081): "
        "Max retries exceeded (Caused by NewConnectionError(...))"
    )
    fake_post = _FakeHttpPost([exc])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.reachable is False
    assert result.schema_ok is None
    assert result.live_options is None
    assert result.self_similarity_ok is None
    assert result.self_similarity_rate is None
    assert result.file_options == (("exponent", "6"), ("metric", "cosine"))
    assert len(fake_post.calls) == 1
    assert "partgraph db up" in result.message
    assert str(exc) not in result.message, (
        f"IndexIntegrityResult.message must never leak the raw exception "
        f"text. Got: {result.message!r}"
    )
    assert "/" not in result.message, (
        f"IndexIntegrityResult.message must be path-free. Got: {result.message!r}"
    )
    assert "\n" not in result.message


def test_ac_idx_17_timeout_message_is_dedicated_not_generic() -> None:
    """AC-IDX-17: Given a requests.exceptions.Timeout vs a generic
    requests.ConnectionError on the first call.
    When check_index_integrity() is called once for each (hermetically, via
    two independently scripted fakes).
    Then the two produce DIFFERENT messages: the timeout case names the
    concept of a timeout and is not the generic "unreachable" wording.
    """
    timeout_result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6,
        http_post=_FakeHttpPost([requests.exceptions.Timeout("t")]),
    )
    conn_result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6,
        http_post=_FakeHttpPost([requests.ConnectionError("c")]),
    )

    assert timeout_result.reachable is False
    assert timeout_result.self_similarity_rate is None
    assert conn_result.self_similarity_rate is None
    assert timeout_result.message != conn_result.message, (
        "The timeout message must be dedicated, not the generic unreachable "
        f"message. Both were: {timeout_result.message!r}"
    )
    assert (
        "timeout" in timeout_result.message.lower()
        or "timed out" in timeout_result.message.lower()
    )
    assert "/" not in timeout_result.message
    assert "\n" not in timeout_result.message


def test_ac_idx_18_forwards_default_timeout_kwarg_to_every_call() -> None:
    """AC-IDX-18: Given check_index_integrity() is called with its default
    timeout (no explicit `timeout=` override).
    When the injected http_post spy records every call.
    Then INDEX_PROBE_TIMEOUT_S was forwarded as the exact `timeout=` kwarg on
    EVERY call, not just the first.
    """
    fake_post = _FakeHttpPost(
        [
            _resp(_LIVE_SCHEMA_AFTER),
            _resp(_HAS_EMBEDDING_ONE_PART),
            _resp(_SIMILAR_TO_INCLUDES_OWN_UID),
        ]
    )
    check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert len(fake_post.calls) == 3
    for call in fake_post.calls:
        assert call["kwargs"].get("timeout") == INDEX_PROBE_TIMEOUT_S


def test_ac_idx_19_forwards_a_custom_timeout_override_to_every_call() -> None:
    """AC-IDX-19: Given check_index_integrity() is called with an explicit,
    non-default timeout value.
    When the injected http_post spy records every call.
    Then that exact custom value — not the module default — is forwarded as
    the `timeout=` kwarg on EVERY call.
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NONE)])
    check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post, timeout=9.5
    )
    assert len(fake_post.calls) == 2
    for call in fake_post.calls:
        assert call["kwargs"].get("timeout") == 9.5


def test_ac_idx_20_unexpected_error_propagates_and_is_not_swallowed() -> None:
    """AC-IDX-20 (robustness / no blind except): Given the injected http_post
    raises an exception that is NOT a requests.exceptions.RequestException (a
    RuntimeError here — modelling a programming error in the seam, never a
    network condition).
    When check_index_integrity() is called.
    Then that exception PROPAGATES unchanged — never coerced into a generic
    unreachable IndexIntegrityResult. Only the specific requests timeout/
    connection families are caught (no blind `except Exception`, ruff
    BLE001); anything else must surface so a real bug is never silently
    masked as "database down".
    """
    fake_post = _FakeHttpPost([RuntimeError("boom")])

    with pytest.raises(RuntimeError, match="boom"):
        check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)


def test_ac_idx_21_importing_index_health_module_does_not_eagerly_import_requests() -> None:
    """AC-IDX-21 (ARCH/SEC leaf discipline): Given partgraph.util.index_health
    declares `requests` as a LAZY import inside check_index_integrity only
    (never at module top level).
    When the module is imported in a FRESH interpreter — subprocess-isolated,
    so this file's own eager `import requests` cannot mask the check
    (mirrors tests/unit/test_health.py:690-719).
    Then `requests` is ABSENT from sys.modules: importing the package pulls
    in no third-party HTTP dependency until a probe actually runs.
    """
    probe_source = (
        "import sys\n"
        "import partgraph.util.index_health\n"
        "assert 'requests' not in sys.modules, "
        "'requests was imported eagerly by partgraph.util.index_health'\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe_source],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, (
        "Importing partgraph.util.index_health must NOT eagerly import "
        f"`requests`.\nreturncode={completed.returncode}\n"
        f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
    )


def test_ac_idx_22_signature_and_dataclass_discipline_recap() -> None:
    """AC-IDX-22 (signature discipline recap): Given the CONTRACT section
    above already pins keyword-only-ness, constant-matching defaults, and the
    http_post=None default individually.
    When all of check_index_integrity's parameters are inspected together.
    Then EVERY parameter (including schema_text) is KEYWORD_ONLY and the
    function accepts no *args/**kwargs escape hatch (VAR_POSITIONAL /
    VAR_KEYWORD are absent) — a future refactor cannot smuggle in a
    positional or unbounded-kwargs calling convention.
    """
    sig = inspect.signature(check_index_integrity)
    kinds = {p.kind for p in sig.parameters.values()}
    assert inspect.Parameter.VAR_POSITIONAL not in kinds
    assert inspect.Parameter.VAR_KEYWORD not in kinds
    assert kinds == {inspect.Parameter.KEYWORD_ONLY}


# ===========================================================================
# AC-IDX-28..36 — multi-sample self-similarity probe (recall-collapse
# hardening). The single-sample AC-IDX-12/13/14 tests above still exercise
# the SAME code path as an M=1 degenerate case (see their amended docstrings)
# — this section adds genuinely MULTI-row (M>1) coverage: the sampled pass
# RATE, the inclusive 0.5 threshold, per-sample invalid-row miss+continue, a
# denominator smaller than the requested sample of 30, mid-loop network
# failure, the pinned N-of-M message wording, an all-invalid edge, and a
# recall-collapse regression canary (mirrors the ADR-0019 production
# incident this rewrite responds to).
# ===========================================================================

def test_ac_idx_28_multi_sample_healthy_all_thirty_samples_hit() -> None:
    """AC-IDX-28: Given the live schema matches the file, the `first: 30`
    selection returns exactly 30 embedded parts (M=N=30), and EVERY one of
    their replayed similar_to calls finds its own uid.
    When check_index_integrity() is called.
    Then self_similarity_ok is True, self_similarity_rate is exactly 1.0,
    EXACTLY 32 (= 30 + 2) HTTP calls were made, the selection request body
    contains "first: 30", and EVERY one of the 30 similar_to call bodies (a)
    contains "similar_to(embedding, 1000," and (b) replays THAT sample's own
    vector — proving samples are not accidentally cross-wired to each
    other's stored vectors.
    """
    n = 30
    selection_body = _build_n_row_selection(n)
    replay = _build_k_of_n_replay(n, hits=n)
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(selection_body), *replay])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.reachable is True
    assert result.schema_ok is True
    assert result.self_similarity_ok is True
    assert result.self_similarity_rate == 1.0
    assert len(fake_post.calls) == n + 2

    texts = _payload_texts(fake_post)
    assert "has(embedding)" in texts[1]
    assert "first: 30" in texts[1]

    for i in range(n):
        call_text = texts[2 + i]
        assert "similar_to(embedding, 1000," in call_text, (
            f"sample {i}: expected K=1000 in the similar_to call. Got: {call_text!r}"
        )
        literal_body = _extract_similar_to_literal(call_text)
        elements = [piece.strip() for piece in literal_body.split(",")]
        expected = [repr(float(component)) for component in _vector(i)]
        assert elements == expected, (
            f"sample {i}: expected its OWN vector {expected!r} to be replayed "
            f"verbatim, got {elements!r} — samples must not be cross-wired."
        )


@pytest.mark.parametrize(
    ("hits", "expected_ok", "case_id"),
    [
        pytest.param(15, True, "fifteen_of_thirty_is_exactly_half_and_passes"),
        pytest.param(14, False, "fourteen_of_thirty_is_just_below_half_and_fails"),
    ],
)
def test_ac_idx_29_threshold_of_one_half_is_inclusive(hits, expected_ok, case_id) -> None:
    """AC-IDX-29 (inclusive threshold boundary): Given 30 samples of which
    EXACTLY 15 (one half) hit in one case, and 14 (just below one half) hit
    in the other.
    When check_index_integrity() is called.
    Then a rate of EXACTLY 0.5 PASSES (self_similarity_ok True) — the
    threshold comparison is `rate >= 0.5`, not `rate > 0.5` — while a rate
    just below 0.5 FAILS, proving the boundary is inclusive on the passing
    side, not the failing side.
    """
    n = 30
    selection_body = _build_n_row_selection(n)
    replay = _build_k_of_n_replay(n, hits=hits)
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(selection_body), *replay])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.self_similarity_rate == hits / n, f"case={case_id}"
    assert result.self_similarity_ok is expected_ok, f"case={case_id}"
    assert len(fake_post.calls) == n + 2, f"case={case_id}"


def test_ac_idx_30_per_sample_invalid_mid_sequence_counts_as_a_miss_and_continues() -> None:
    """AC-IDX-30: Given 5 samples where ONE (index 2 of 0..4 — neither first
    nor last) has an INVALID stored vector (a poisoned, hostile-string-in-a-
    list shape) and the OTHER FOUR are valid and all hit.
    When check_index_integrity() is called.
    Then:
      - EXACTLY 2 + (5 - 1) = 6 HTTP calls are made — the leaf issues NO
        similar_to call for the poisoned row but keeps going (does not abort
        the whole probe, unlike a NETWORK failure).
      - the poisoned text never reaches ANY outgoing request body.
      - self_similarity_rate is (5 - 1) / 5 = 0.8 — the poisoned row COUNTS
        toward the M=5 denominator as an automatic miss; it is not silently
        excluded from M (which would wrongly give a rate of 4/4 = 1.0).
      - self_similarity_ok is True (0.8 >= 0.5), no exception escapes, and
        the message stays clean.
    """
    n = 5
    poison_at = 2
    selection_body = _build_n_row_selection(n, poison_at=poison_at)
    replay = _build_k_of_n_replay(n, hits=n - 1, skip=frozenset({poison_at}))
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(selection_body), *replay])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert len(fake_post.calls) == 2 + (n - 1), (
        "expected NO similar_to call for the poisoned row (2 + 4 valid calls), "
        f"got {len(fake_post.calls)} calls."
    )
    assert _POISON_VECTOR_ELEMENT not in "".join(_payload_texts(fake_post)), (
        "the poisoned stored-vector text must never reach an outgoing request body."
    )
    assert result.self_similarity_rate == (n - 1) / n, (
        "a poisoned/invalid row must count as a MISS in the M denominator, "
        "not be excluded from it entirely."
    )
    assert result.self_similarity_ok is True
    _assert_message_clean(result.message)


@pytest.mark.parametrize(
    ("m", "hits", "case_id"),
    [
        pytest.param(1, 1, "m_equals_one"),
        pytest.param(3, 2, "m_equals_three"),
    ],
)
def test_ac_idx_31_rate_denominator_is_the_actual_row_count_m_not_the_requested_thirty(
    m, hits, case_id,
) -> None:
    """AC-IDX-31: Given the selection query always REQUESTS `first: 30`, but
    Dgraph has fewer than 30 embedded parts total, so the response actually
    returns only M rows (M=1 and M=3, tested separately).
    When check_index_integrity() is called.
    Then self_similarity_rate is computed as hits / M (the ACTUAL row count),
    NOT hits / 30 — a rate of hits/30 here would be a materially different
    (and, for these small M, much smaller) number, so this is a sharp
    differentiator against a "wrong denominator" bug — and exactly 2 + M
    HTTP calls are made (2 + valid(M), and every row here is valid).
    """
    selection_body = _build_n_row_selection(m)
    replay = _build_k_of_n_replay(m, hits=hits)
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(selection_body), *replay])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    expected_rate = hits / m
    assert result.self_similarity_rate == expected_rate, (
        f"case={case_id}: rate must be computed over the ACTUAL row count "
        f"M={m}, not the requested sample size 30. Got "
        f"{result.self_similarity_rate!r}, expected {expected_rate!r} (would "
        f"be {hits / 30!r} if wrongly divided by 30)."
    )
    assert result.self_similarity_ok is (expected_rate >= 0.5), f"case={case_id}"
    assert len(fake_post.calls) == 2 + m, f"case={case_id}"
    texts = _payload_texts(fake_post)
    assert "first: 30" in texts[1], (
        "the SELECTION request must still ask for first: 30 regardless of "
        "how many rows the (faked) response actually returns."
    )


# --- AC-IDX-32: self_similarity_rate is None/None/float across the three
# reachability outcomes (mirrors self_similarity_ok's own None/None/bool
# split — one dedicated test per outcome for a clear, independent failure
# signal per scenario). ---

def test_ac_idx_32_self_similarity_rate_is_none_when_unreachable() -> None:
    """AC-IDX-32a: Given the very first HTTP call (schema introspection)
    fails with a connection error.
    When check_index_integrity() is called.
    Then self_similarity_rate is None (there is nothing to compute a rate
    over — the whole probe aborted before any sampling happened).
    """
    fake_post = _FakeHttpPost([requests.ConnectionError("c")])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.reachable is False
    assert result.self_similarity_rate is None


def test_ac_idx_32_self_similarity_rate_is_none_when_no_embedded_parts() -> None:
    """AC-IDX-32b: Given the schema matches but the `first: 30` selection
    returns ZERO rows (M=0 — no embedded parts at all).
    When check_index_integrity() is called.
    Then self_similarity_ok AND self_similarity_rate are both None (there is
    nothing to sample), and exactly 2 HTTP calls were made.
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NONE)])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.self_similarity_ok is None
    assert result.self_similarity_rate is None
    assert len(fake_post.calls) == 2


def test_ac_idx_32_self_similarity_rate_is_a_float_when_probed() -> None:
    """AC-IDX-32c: Given a normal, completed multi-sample probe (M=5, 3 hits).
    When check_index_integrity() is called.
    Then self_similarity_rate is a `float` instance (never an `int`, a
    `Fraction`, or any other numeric type) equal to hits / M exactly.
    """
    n = 5
    selection_body = _build_n_row_selection(n)
    replay = _build_k_of_n_replay(n, hits=3)
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(selection_body), *replay])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert isinstance(result.self_similarity_rate, float)
    assert result.self_similarity_rate == 3 / 5


@pytest.mark.parametrize(
    "injected_exception",
    [
        pytest.param(requests.exceptions.Timeout("t"), id="timeout"),
        pytest.param(requests.ConnectionError("c"), id="connection_error"),
    ],
)
def test_ac_idx_33_mid_loop_network_failure_aborts_the_whole_probe(injected_exception) -> None:
    """AC-IDX-33: Given the schema and selection calls both succeed (M=5, all
    valid rows), TWO per-sample similar_to replays complete successfully, and
    THEN the third per-sample replay call raises a network exception
    (Timeout and ConnectionError tested separately) — genuinely MID-LOOP, not
    the first call ever made.
    When check_index_integrity() is called.
    Then the WHOLE probe aborts exactly as a first-call failure would:
    reachable=False, schema_ok=None, live_options=None,
    self_similarity_ok=None, self_similarity_rate=None, file_options is still
    populated (pure, no network needed), NO further HTTP call is made beyond
    the failing one, and the message stays clean. The call count pins exactly
    WHERE the failure happened: 2 (schema + selection) + 2 (the two
    successful replays) + 1 (the failing call itself) = 5.
    """
    n = 5
    fail_at = 2  # 0-based sample index: two successful replays precede it.
    selection_body = _build_n_row_selection(n)
    successful_replays = _build_k_of_n_replay(fail_at, hits=fail_at)
    fake_post = _FakeHttpPost(
        [
            _resp(_LIVE_SCHEMA_AFTER),
            _resp(selection_body),
            *successful_replays,
            injected_exception,
        ]
    )

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.reachable is False
    assert result.schema_ok is None
    assert result.live_options is None
    assert result.self_similarity_ok is None
    assert result.self_similarity_rate is None
    assert result.file_options == (("exponent", "6"), ("metric", "cosine"))
    assert len(fake_post.calls) == 2 + fail_at + 1

    texts = _payload_texts(fake_post)
    assert texts[0].strip() == "schema(pred: [embedding]) {}"
    assert "first: 30" in texts[1]
    for i in range(fail_at):
        assert "similar_to(embedding, 1000," in texts[2 + i], (
            f"expected a successful similar_to call for sample {i} BEFORE "
            "the mid-loop failure."
        )
    _assert_message_clean(result.message)


def test_ac_idx_34_message_wording_is_pinned_with_floor_percent_and_n_of_m_phrasing() -> None:
    """AC-IDX-34 (pinned wording): Given a PASS scenario (28 of 30 samples
    hit — 93%, since floor(100*28/30) == 93) and, separately, a FAIL scenario
    (3 of 30 samples hit — 10%, since floor(100*3/30) == 10).
    When check_index_integrity() is called for each.
    Then the message contains the EXACT pinned self-similarity clause for
    that outcome — "the self-similarity probe passed (28 of 30 sampled
    parts, 93%, found their own vector at k=1000)." for the PASS case, "the
    self-similarity probe failed (only 3 of 30 sampled parts, 10%, found
    their own vector at k=1000)." for the FAIL case (mirroring the existing
    _SELF_PASS_CLAUSE/_SELF_FAIL_CLAUSE naming split — only the parenthetical
    detail changed, the leading "passed"/"failed" verb did not) — and NEVER
    the raw fraction form "28/30" or "3/30" (the message must say "N of M",
    never "N/M"), staying single-line and path-free throughout.
    """
    n = 30

    # --- PASS: 28 of 30 hit -> floor(100*28/30) == 93 ---
    pass_selection = _build_n_row_selection(n)
    pass_replay = _build_k_of_n_replay(n, hits=28)
    pass_fake_post = _FakeHttpPost(
        [_resp(_LIVE_SCHEMA_AFTER), _resp(pass_selection), *pass_replay]
    )
    pass_result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=pass_fake_post
    )
    assert pass_result.self_similarity_ok is True
    assert _floor_percent(28, n) == 93
    assert (
        "the self-similarity probe passed "
        "(28 of 30 sampled parts, 93%, found their own vector at k=1000)."
    ) in pass_result.message, pass_result.message
    assert "28/30" not in pass_result.message
    _assert_message_clean(pass_result.message)

    # --- FAIL: 3 of 30 hit -> floor(100*3/30) == 10 ---
    fail_selection = _build_n_row_selection(n)
    fail_replay = _build_k_of_n_replay(n, hits=3)
    fail_fake_post = _FakeHttpPost(
        [_resp(_LIVE_SCHEMA_AFTER), _resp(fail_selection), *fail_replay]
    )
    fail_result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fail_fake_post
    )
    assert fail_result.self_similarity_ok is False
    assert _floor_percent(3, n) == 10
    assert (
        "the self-similarity probe failed "
        "(only 3 of 30 sampled parts, 10%, found their own vector at k=1000)."
    ) in fail_result.message, fail_result.message
    assert "3/30" not in fail_result.message
    _assert_message_clean(fail_result.message)


def test_ac_idx_35_all_stored_vectors_invalid_yields_zero_rate_and_no_similar_to_call() -> None:
    """AC-IDX-35 (all-invalid edge): Given the `first: 30` selection returns
    30 rows, and EVERY single one carries an invalid (non-list) stored
    vector.
    When check_index_integrity() is called.
    Then self_similarity_rate is exactly 0.0, self_similarity_ok is False,
    and EXACTLY 2 HTTP calls were made — NO similar_to call at all (the
    scripted fake provides only 2 outcomes, so a 3rd call would raise
    AssertionError from _FakeHttpPost itself).
    """
    n = 30
    selection_body = {
        "data": {"q": [{"uid": _uid(i), "embedding": "not-a-vector"} for i in range(n)]}
    }
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(selection_body)])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.self_similarity_rate == 0.0
    assert result.self_similarity_ok is False
    assert len(fake_post.calls) == 2, (
        "no similar_to call may be issued when EVERY stored vector is invalid."
    )
    _assert_message_clean(result.message)


def test_ac_idx_36_recall_collapse_regression_every_sample_misses_the_canary_fires() -> None:
    """AC-IDX-36 (regression canary — mirrors the ADR-0019 measured "4 of
    1000" production recall collapse this rewrite responds to): Given 30
    valid, distinct embedded-part samples, but EVERY SINGLE one of their
    similar_to replays EXCLUDES its own uid (a totally collapsed index).
    When check_index_integrity() is called.
    Then self_similarity_ok is False and self_similarity_rate is exactly
    0.0 — the canary correctly reports total failure rather than any
    rate-computation bug (e.g. an `any()`/`all()` mixup) accidentally
    reporting a false pass when NOTHING found itself.
    """
    n = 30
    selection_body = _build_n_row_selection(n)
    replay = _build_k_of_n_replay(n, hits=0)
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(selection_body), *replay])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.self_similarity_ok is False
    assert result.self_similarity_rate == 0.0
    assert len(fake_post.calls) == n + 2
    _assert_message_clean(result.message)


# ===========================================================================
# EDGE CASES (probe level) — schema reachable but unparseable; oversized/
# undersized stored vector.
# ===========================================================================

def test_edge_empty_schema_array_is_schema_ok_false() -> None:
    """EDGE CASE: Given the live schema query succeeds (HTTP 200) but returns
    an EMPTY `schema` array (the `embedding` predicate does not exist in
    Dgraph at all — e.g. schema was never applied).
    When check_index_integrity() is called.
    Then reachable is True (the socket answered) but schema_ok is False
    (never None — the query succeeded, it just found nothing to compare).
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_EMPTY_ARRAY), _resp(_HAS_EMBEDDING_NONE)])
    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)
    assert result.reachable is True
    assert result.schema_ok is False
    assert result.live_options is None
    _assert_message_clean(result.message)


def test_edge_predicate_present_without_index_specs_is_schema_ok_false() -> None:
    """EDGE CASE: Given the live `embedding` predicate exists but carries NO
    `index_specs` at all (e.g. declared as a plain float32vector with no
    @index directive — the hnsw index was dropped).
    When check_index_integrity() is called.
    Then reachable is True and schema_ok is False.
    """
    fake_post = _FakeHttpPost(
        [_resp(_LIVE_SCHEMA_NO_INDEX_SPECS_KEY), _resp(_HAS_EMBEDDING_NONE)]
    )
    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)
    assert result.reachable is True
    assert result.schema_ok is False
    assert result.live_options is None
    _assert_message_clean(result.message)


def test_edge_stored_vector_of_unexpected_length_does_not_crash() -> None:
    """EDGE CASE: Given the stored vector returned by the has(embedding)
    query has an "unexpected" length (this fixture's 3-float _STORED_VECTOR
    throughout this file — schema/partgraph.dql says nothing about
    dimensionality, and check_index_integrity's job is to REPLAY whatever was
    actually stored, not to validate it against an external dimension
    constant like `dql_builder.EMBED_DIM`).
    When check_index_integrity() is called and the third (similar_to) call
    succeeds.
    Then it does not raise, returns a valid IndexIntegrityResult, and the
    message is still path-free and single-line. This test deliberately does
    NOT assert a specific self_similarity_ok value: whether a short/atypical
    vector round-trips through a real Dgraph is an integration concern, not
    a hermetic-unit one.
    """
    fake_post = _FakeHttpPost(
        [
            _resp(_LIVE_SCHEMA_AFTER),
            _resp(_HAS_EMBEDDING_ONE_PART),  # embedding=[0.1, 0.2, 0.3]
            _resp(_SIMILAR_TO_INCLUDES_OWN_UID),
        ]
    )
    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert isinstance(result, IndexIntegrityResult)
    assert isinstance(result.message, str) and result.message
    assert "/" not in result.message
    assert "\n" not in result.message


# ===========================================================================
# SECURITY (ADR-0019 Finding 1) — a poisoned/corrupt stored vector is a HANDLED
# integrity failure: it is NEVER inlined into the similar_to literal, and NO
# third HTTP call is issued for it. The scripted _FakeHttpPost provides exactly
# TWO outcomes, so any attempt at a third call raises AssertionError — proving
# the third call is skipped, not merely that the payload was scrubbed.
# ===========================================================================

def test_security_poisoned_stored_vector_element_blocks_third_call() -> None:
    """SECURITY (M=1 case): Given the `first: 30` selection returns exactly
    ONE row (M=1) whose stored `embedding` is a LIST containing a hostile
    string element (a DQL-injection attempt that, inlined raw, would close
    the literal and smuggle in a second query block).
    When check_index_integrity() is called (schema matches; one "embedded" part).
    Then:
      - EXACTLY TWO HTTP calls are made — the leaf issues NO third (similar_to)
        call (the scripted fake would raise AssertionError on a third call).
      - the poisoned text never appears in ANY outgoing request body.
      - the result is a clean IndexIntegrityResult with self_similarity_ok=False
        (a handled failure) and self_similarity_rate=0.0 (0 hits / 1 sampled),
        reachable=True, schema_ok=True, and a path-free, single-line message —
        no exception escapes.
    """
    fake_post = _FakeHttpPost(
        [_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_POISONED_LIST)]
    )

    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )

    assert len(fake_post.calls) == 2, (
        "check_index_integrity issued a third (similar_to) call on a poisoned "
        f"stored vector — it must not. Calls made: {len(fake_post.calls)}"
    )
    assert isinstance(result, IndexIntegrityResult)
    assert result.reachable is True
    assert result.schema_ok is True
    assert result.self_similarity_ok is False
    assert result.self_similarity_rate == 0.0
    assert all(
        _POISON_VECTOR_ELEMENT not in text for text in _payload_texts(fake_post)
    ), "the poisoned stored-vector text must never reach an outgoing request body."
    _assert_message_clean(result.message)


def test_security_non_list_stored_vector_blocks_third_call() -> None:
    """SECURITY (M=1 case): Given the `first: 30` selection returns exactly
    ONE row (M=1) whose stored `embedding` is not a list at all (a bare
    string — a corrupt/unexpected shape).
    When check_index_integrity() is called.
    Then it is the SAME clean, handled failure as the poisoned-list case: EXACTLY
    TWO HTTP calls (no similar_to replay), self_similarity_ok=False,
    self_similarity_rate=0.0 (0 hits / 1 sampled), no exception, and a
    path-free, single-line message.
    """
    fake_post = _FakeHttpPost(
        [_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NON_LIST_VECTOR)]
    )

    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )

    assert len(fake_post.calls) == 2, (
        "check_index_integrity issued a third (similar_to) call for a non-list "
        f"stored vector — it must not. Calls made: {len(fake_post.calls)}"
    )
    assert result.reachable is True
    assert result.schema_ok is True
    assert result.self_similarity_ok is False
    assert result.self_similarity_rate == 0.0
    _assert_message_clean(result.message)


# ===========================================================================
# SECURITY (Gate 3a) — variable-denominator message content, M=0 message
# hygiene, and bounded per-call timeout across ALL 32 calls of a healthy N=30
# probe. Add-only hardening: none of these existed in the single-sample suite,
# and each guards a distinct multi-sample failure mode (misreported recall on a
# small catalogue, an un-cleaned skip message, an unbounded per-replay wait).
# ===========================================================================

def test_security_message_denominator_tracks_actual_m_not_the_requested_thirty() -> None:
    """SECURITY (variable-denominator message content): Given the `first: 30`
    selection returns only M=3 rows (fewer than 30 embedded parts exist), of
    which 2 hit.
    When check_index_integrity() is called.
    Then the summary reports the ACTUAL denominator M=3 — "2 of 3 sampled
    parts" and the floored percent floor(100*2/3)=66% — and NEVER the requested
    sample size 30 as the denominator (a message that hard-coded "30" would
    misreport recall on any DB holding fewer than 30 embedded parts). The
    message stays single-line and path-free.
    """
    m, hits = 3, 2
    selection_body = _build_n_row_selection(m)
    replay = _build_k_of_n_replay(m, hits=hits)
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(selection_body), *replay])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.self_similarity_rate == hits / m
    assert "2 of 3 sampled parts" in result.message, result.message
    assert f"{_floor_percent(hits, m)}%" in result.message, result.message  # 66%
    assert "30" not in result.message, (
        "the message must report the ACTUAL denominator M=3, never the requested "
        f"sample size 30. Got: {result.message!r}"
    )
    _assert_message_clean(result.message)


def test_security_no_embedded_parts_message_is_clean() -> None:
    """SECURITY (M=0 message hygiene): Given the schema matches but no embedded
    parts exist (the skip case, M=0).
    When check_index_integrity() is called.
    Then the "skipped" summary is held to the SAME message-hygiene contract as
    the pass/fail branches: non-empty, single-line, and path-free.
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NONE)])

    result = check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert result.self_similarity_ok is None
    assert result.self_similarity_rate is None
    _assert_message_clean(result.message)


def test_security_timeout_forwarded_on_all_thirty_two_calls_in_healthy_probe() -> None:
    """SECURITY/robustness (bounded per-call timeout): Given a fully healthy
    N=30 probe that issues all 32 (= 30 + 2) calls.
    When the injected http_post spy records every call.
    Then the finite INDEX_PROBE_TIMEOUT_S is forwarded as the `timeout=` kwarg
    on EVERY one of the 32 calls — not just the first two — so no single wedged
    per-sample replay can hang the probe on an unbounded wait.
    """
    n = 30
    selection_body = _build_n_row_selection(n)
    replay = _build_k_of_n_replay(n, hits=n)
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(selection_body), *replay])

    check_index_integrity(schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post)

    assert len(fake_post.calls) == n + 2
    for call in fake_post.calls:
        assert call["kwargs"].get("timeout") == INDEX_PROBE_TIMEOUT_S
