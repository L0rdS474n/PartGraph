"""
Tests: AC-IDX-4..22 — partgraph.util.index_health (leaf module) +
`partgraph db check-index` vector-index integrity gate (ADR-0019).

Specifies the behaviour of the NEW leaf module ``partgraph.util.index_health``,
which lets `partgraph db check-index` answer a question `db status` cannot:
not just "is Dgraph alive" but "does the LIVE hnsw vector-index configuration
on the `embedding` predicate actually match what schema/partgraph.dql
declares, and does a self-similarity probe (re-issuing an already-embedded
part's own stored vector through `similar_to`) still find that same part" —
catching a schema/live drift (e.g. `apply-schema` never re-run after an
exponent bump) or a corrupted/rebuilding vector index that a bare `/health`
200 would never reveal.

Pinned contract (leaf module ``partgraph.util.index_health`` — NOT YET
IMPLEMENTED; collection of THIS file is expected to ERROR with
ModuleNotFoundError until it exists, and that is the correct test-first red
state, mirroring tests/unit/test_health.py's own documented red state):
  - ``DGRAPH_QUERY_URL: str`` = "http://127.0.0.1:8081/query" — Dgraph's HTTP
    DQL query endpoint (documented in docs/connecting.md section 2.2:
    ``POST``, ``Content-Type: application/dql``), distinct from
    ``partgraph.util.health.DGRAPH_HTTP_HEALTH_URL``.
  - ``INDEX_PROBE_TIMEOUT_S: float`` = 2.0 — a finite, named, bounded timeout
    (mirrors ``HEALTH_PROBE_TIMEOUT_S`` / ADR-0007's bounded-constant
    precedent).
  - ``DEFAULT_EXPONENT: str`` = "3" — the Dgraph hnsw driver default applied
    when neither the schema file nor a live predicate's index_specs carries
    an explicit ``exponent`` key, so "not configured" and "configured to the
    documented default" compare equal rather than spuriously drifting.
  - ``parse_file_hnsw_options(schema_text: str) -> tuple[tuple[str, str],
    ...]`` — PURE: extracts the ``hnsw(...)`` options for the ``embedding:``
    predicate line out of schema-FILE text, normalizes a missing
    ``exponent`` to ``("exponent", DEFAULT_EXPONENT)``, and returns a SORTED
    tuple of ``(key, value)`` pairs.
  - ``parse_live_hnsw_options(schema_json: dict) -> tuple[tuple[str, str],
    ...] | None`` — PURE: takes the FULL parsed JSON body of a live DQL
    ``schema(pred: [embedding]) {}`` response (i.e. exactly what
    ``response.json()`` returns — ``{"data": {"schema": [...]}}``), applies
    the SAME default-exponent normalization, and returns ``None`` when the
    ``embedding`` predicate is absent (including an empty ``schema`` array)
    OR its ``index_specs`` carries no ``hnsw`` entry.
  - ``@dataclass(frozen=True) class IndexIntegrityResult`` with EXACTLY six
    fields: ``reachable: bool``, ``schema_ok: bool | None``,
    ``file_options: tuple[tuple[str, str], ...]``,
    ``live_options: tuple[tuple[str, str], ...] | None``,
    ``self_similarity_ok: bool | None``, ``message: str``.
  - ``def check_index_integrity(*, schema_text: str,
    url=DGRAPH_QUERY_URL, timeout=INDEX_PROBE_TIMEOUT_S, http_post=None) ->
    IndexIntegrityResult`` — ALL FOUR parameters are keyword-only (mirrors
    ``probe_health``'s discipline). ``http_post`` is the INJECTABLE SEAM
    (defaults to a LAZILY-imported ``requests.post`` so this leaf never
    imports ``requests`` eagerly just by being imported), invoked EXACTLY as
    ``http_post(url, data=..., headers=..., timeout=timeout)`` and must
    return an object exposing ``.status_code`` and ``.json()``. Flow:
      1. Query the live schema (``schema(pred: [embedding]) {}``); a
         connection/timeout failure here short-circuits the WHOLE probe —
         ``reachable=False``, ``schema_ok=None``, ``live_options=None``,
         ``self_similarity_ok=None`` — and NO further HTTP call is made.
      2. Compare the live options (``parse_live_hnsw_options``) against the
         file options (``parse_file_hnsw_options(schema_text)``) ->
         ``schema_ok`` (``True``/``False`` once the live schema query
         SUCCEEDED, independent of whether it matched; never ``None`` in
         that case).
      3. Query the first embedded part (``{ q(func: has(embedding), first:
         1) { uid embedding } }``); if none exists, ``self_similarity_ok``
         is ``None`` and NO third call is made. Otherwise its stored vector
         is re-issued verbatim through ``similar_to(embedding, 5,
         "[...]")``; ``self_similarity_ok`` is ``True`` iff the part's OWN
         uid is present in that result set.
      4. Every returned ``message`` is a fixed, single-line, path-free
         string ('/' never appears; a raw exception string is never
         interpolated).
    Any exception that is NOT a ``requests.exceptions.RequestException``
    (e.g. a programming error in an injected seam) PROPAGATES — never a
    blind ``except Exception`` (ruff BLE001).
  - ``partgraph db check-index`` (Gate 4, tested separately in
    tests/unit/test_cli_check_index.py) calls
    ``check_index_integrity(schema_text=load_schema(SCHEMA_FILE))`` with
    ZERO overrides and exits ``0`` iff ``reachable and schema_ok and
    self_similarity_ok in (True, None)``, else ``1``.

This file mirrors tests/unit/test_health.py's hermetic style throughout:
Given/When/Then docstrings, an injected-seam ``_FakeHttpPost`` spy (never a
real socket), and no sleep/real-clock anywhere. Unlike ``probe_health``'s
single GET, ``check_index_integrity`` issues UP TO THREE sequential POSTs, so
``_FakeHttpPost`` is SCRIPTED with an ORDERED sequence of outcomes (one per
call) rather than a single fixed result, and raises a clear ``AssertionError``
if the leaf calls it more times than were scripted — an unscripted extra call
is exactly the kind of "keeps hammering an unreachable/exhausted seam" bug
this suite must catch, not silently tolerate by recycling the last outcome.

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

    Extracts ``0.1, 0.2, 0.3`` from ``similar_to(embedding, 5, "[0.1, 0.2, 0.3]")``
    so a test can assert EVERY element (not just a chosen fixture component) is
    strictly numeric — a stronger guarantee than ``str(x) in text``.
    """
    match = re.search(
        r'similar_to\(embedding,\s*\d+,\s*"\[(.*?)\]"\)', query_text
    )
    assert match, f"could not locate the similar_to vector literal in {query_text!r}"
    return match.group(1)


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
    Then it exposes EXACTLY the six pinned fields (reachable, schema_ok,
    file_options, live_options, self_similarity_ok, message) and is frozen —
    a consumer (e.g. cli.py's `check-index` command) cannot mutate a result.
    """
    result = IndexIntegrityResult(
        reachable=True,
        schema_ok=True,
        file_options=(("exponent", "6"), ("metric", "cosine")),
        live_options=(("exponent", "6"), ("metric", "cosine")),
        self_similarity_ok=True,
        message="ok",
    )
    field_names = {f.name for f in dataclasses.fields(result)}
    assert field_names == {
        "reachable",
        "schema_ok",
        "file_options",
        "live_options",
        "self_similarity_ok",
        "message",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.schema_ok = False  # type: ignore[misc]


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
    """AC-IDX-12: Given (1) the live schema matches the file (exponent "6"
    both sides), (2) exactly one embedded part exists, and (3) re-issuing its
    stored vector through similar_to returns a result set CONTAINING its own
    uid.
    When check_index_integrity() is called.
    Then reachable/schema_ok/self_similarity_ok are all True, and the message
    is a non-empty, single-line, path-free string. Also pins the exact DQL
    text of all three calls (API boundary stability): the schema
    introspection query, the `has(embedding), first: 1` selection, and a
    `similar_to(embedding, 5, ...)` call whose vector components are the
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
    assert isinstance(result.message, str) and result.message
    assert "\n" not in result.message
    assert "/" not in result.message

    assert len(fake_post.calls) == 3
    texts = _payload_texts(fake_post)
    assert texts[0].strip() == "schema(pred: [embedding]) {}", (
        f"Expected the exact live-schema introspection query text. Got: {texts[0]!r}"
    )
    assert "has(embedding)" in texts[1]
    assert "first: 1" in texts[1]
    assert "uid" in texts[1]
    assert "embedding" in texts[1]
    assert "similar_to(embedding, 5," in texts[2]
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
    """AC-IDX-14: Given the schema matches, an embedded part exists, but
    re-issuing its OWN stored vector through similar_to returns a result set
    that does NOT contain that part's own uid (a corrupted/stale vector
    index — the part cannot even find itself).
    When check_index_integrity() is called.
    Then self_similarity_ok is False.
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
    _assert_message_clean(result.message)


def test_ac_idx_15_no_embedded_parts_yields_self_similarity_none_and_no_third_call() -> None:
    """AC-IDX-15: Given the schema matches but Dgraph has NO embedded parts
    at all (`has(embedding)` returns an empty row set — e.g. `partgraph
    embed` has never been run).
    When check_index_integrity() is called.
    Then self_similarity_ok is None (there is nothing to self-check) and
    EXACTLY TWO HTTP calls were made — the leaf never issues a third,
    meaningless similar_to("[]"...) call when there is no stored vector to
    replay.
    """
    fake_post = _FakeHttpPost([_resp(_LIVE_SCHEMA_AFTER), _resp(_HAS_EMBEDDING_NONE)])
    result = check_index_integrity(
        schema_text=_FILE_TEXT_EXPONENT_6, http_post=fake_post
    )
    assert result.schema_ok is True
    assert result.self_similarity_ok is None
    assert len(fake_post.calls) == 2, (
        "check_index_integrity must not issue a similar_to call when "
        f"has(embedding) found nothing. Calls made: {len(fake_post.calls)}"
    )


def test_ac_idx_16_connection_error_on_first_call_is_unreachable() -> None:
    """AC-IDX-16: Given the injected http_post raises requests.ConnectionError
    on the VERY FIRST call (the live schema query).
    When check_index_integrity() is called.
    Then reachable is False, schema_ok/live_options/self_similarity_ok are
    all None, EXACTLY ONE HTTP call was attempted (no further calls after the
    first failure), and the message is a fixed, path-free string naming
    `partgraph db up` that contains NEITHER the raw exception text NOR a '/'.
    file_options is still populated (it is computed PURELY from schema_text,
    no network needed).
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
    """SECURITY: Given the has(embedding) query returns a part whose stored
    `embedding` is a LIST containing a hostile string element (a DQL-injection
    attempt that, inlined raw, would close the literal and smuggle in a second
    query block).
    When check_index_integrity() is called (schema matches; one "embedded" part).
    Then:
      - EXACTLY TWO HTTP calls are made — the leaf issues NO third (similar_to)
        call (the scripted fake would raise AssertionError on a third call).
      - the poisoned text never appears in ANY outgoing request body.
      - the result is a clean IndexIntegrityResult with self_similarity_ok=False
        (a handled failure), reachable=True, schema_ok=True, and a path-free,
        single-line message — no exception escapes.
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
    assert all(
        _POISON_VECTOR_ELEMENT not in text for text in _payload_texts(fake_post)
    ), "the poisoned stored-vector text must never reach an outgoing request body."
    _assert_message_clean(result.message)


def test_security_non_list_stored_vector_blocks_third_call() -> None:
    """SECURITY: Given the has(embedding) query returns a part whose stored
    `embedding` is not a list at all (a bare string — a corrupt/unexpected shape).
    When check_index_integrity() is called.
    Then it is the SAME clean, handled failure as the poisoned-list case: EXACTLY
    TWO HTTP calls (no similar_to replay), self_similarity_ok=False, no exception,
    and a path-free, single-line message.
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
    _assert_message_clean(result.message)
