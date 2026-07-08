"""HTTP DQL vector-index integrity probe for the local Dgraph instance.

This is a **leaf** module: its top-level imports are the Python standard library
only (:mod:`re`, :mod:`dataclasses`, :mod:`collections.abc`, :mod:`typing`). It
imports :mod:`requests` **lazily**, inside :func:`check_index_integrity`, never
at module import time — so importing :mod:`partgraph.util.index_health` pulls in
no third-party HTTP dependency, and a consumer that only needs the constants,
the pure parsers, or the :class:`IndexIntegrityResult` DTO never pays for the
``requests`` import. It must never import :mod:`partgraph.cli` or any of the
embed/query/load layers, so both the CLI and the integration tests can use it
without an import cycle (mirrors :mod:`partgraph.util.health`).

Why this exists
---------------
``partgraph db status`` (ADR-0018) answers "is Dgraph alive?" with an HTTP
``/health`` 200. It cannot answer a subtler, ADR-0019 question: does the LIVE
``hnsw`` index configuration on the ``embedding`` predicate actually match what
``schema/partgraph.dql`` declares, and does a self-similarity probe (re-issuing
an already-embedded part's OWN stored vector through ``similar_to``) still find
that same part? A schema-file/live drift (``apply-schema`` never re-run after an
``exponent`` bump) or a corrupted/rebuilding vector index is invisible to a bare
``/health`` 200. ``partgraph db check-index`` closes that gap.

Contract
--------
- :data:`DGRAPH_QUERY_URL` is the single source of truth for Dgraph's HTTP DQL
  query endpoint (``POST``, ``Content-Type: application/dql``; docs/connecting.md
  section 2.2), distinct from :data:`partgraph.util.health.DGRAPH_HTTP_HEALTH_URL`.
- :data:`INDEX_PROBE_TIMEOUT_S` is a finite, bounded request timeout (never an
  unbounded/``None`` timeout), extending ADR-0007's bounded-constant precedent.
- :func:`check_index_integrity` returns a frozen :class:`IndexIntegrityResult` on
  every handled outcome, and never leaks a raw exception string, a response body,
  or a filesystem path into :attr:`IndexIntegrityResult.message` (the message is
  always safe to print verbatim).
- Security (ADR-0019 / ADR-INJECT): a stored ``embedding`` vector is validated
  element-by-element with a local ``repr(float(x))`` + strict-charset formatter
  BEFORE it is inlined into the ``similar_to`` literal. A poisoned or corrupt
  stored value is reported as a handled integrity failure — it is never
  interpolated raw into query text, and no third HTTP call is issued for it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_EXPONENT",
    "DGRAPH_QUERY_URL",
    "INDEX_PROBE_TIMEOUT_S",
    "IndexIntegrityResult",
    "check_index_integrity",
    "parse_file_hnsw_options",
    "parse_live_hnsw_options",
]

#: Single source of truth for Dgraph's HTTP DQL query endpoint (host port 8081
#: -> container 8080; loopback only). A raw DQL body is POSTed here with
#: ``Content-Type: application/dql`` (docs/connecting.md section 2.2). Distinct
#: from :data:`partgraph.util.health.DGRAPH_HTTP_HEALTH_URL` (the ``/health`` URL).
DGRAPH_QUERY_URL = "http://127.0.0.1:8081/query"

#: Finite, named, bounded per-request timeout (seconds) for every probe call.
#: Deliberately a small positive float, never ``None`` (an unbounded wait):
#: ``db check-index`` must return promptly whether Dgraph is up, down, or wedged.
#: Extends ADR-0007's bounded-constant precedent to these outbound calls.
INDEX_PROBE_TIMEOUT_S = 2.0

#: The Dgraph ``hnsw`` driver default applied when neither the schema file nor a
#: live predicate's ``index_specs`` carries an explicit ``exponent`` key, so
#: "not configured" and "configured to the documented default" compare equal
#: rather than spuriously drifting. A STRING, because ``hnsw`` option values are
#: always JSON/DQL string literals (e.g. ``{"key": "exponent", "value": "3"}``).
DEFAULT_EXPONENT = "3"

#: Number of neighbours requested by the self-similarity replay probe. Named so
#: the query builder below stays magic-value-free.
_SELF_SIMILARITY_K = 5

#: The Content-Type Dgraph's HTTP ``/query`` endpoint requires so it parses the
#: body as DQL (not GraphQL); docs/connecting.md section 2.2.
_DQL_HEADERS = {"Content-Type": "application/dql"}

#: The exact live-schema introspection query (predicate-scoped, body-less).
_SCHEMA_INTROSPECTION_QUERY = "schema(pred: [embedding]) {}"

#: Selects a single embedded part plus its stored vector, for the replay probe.
_HAS_EMBEDDING_QUERY = "{ q(func: has(embedding), first: 1) { uid embedding } }"

#: Matches one ``key: "value"`` ``hnsw`` option pair inside a directive block.
_HNSW_OPTION_RE = re.compile(r'(\w+)\s*:\s*"([^"]*)"')

#: Matches the ``hnsw(...)`` block within an ``@index(...)`` directive. The
#: non-greedy body stops at the first ``)``; ``hnsw`` options never nest parens.
_HNSW_BLOCK_RE = re.compile(r"hnsw\((.*?)\)")

#: Strict charset a formatted float literal must match before it can reach the
#: ``similar_to`` query text (mirrors dql_builder's ADR-INJECT discipline). A
#: locally-owned copy: this leaf never imports :mod:`partgraph.query.dql_builder`.
_FLOAT_LITERAL_RE = re.compile(r"[0-9.eE+\-]+")

#: Fixed, path-free message for an unreachable database (connection refused /
#: DNS / reset — anything but a timeout). Names the remedy without echoing the
#: raw exception, the URL, or any '/'-bearing path, so nothing internal leaks.
_UNREACHABLE_MESSAGE = "Dgraph is not reachable. Start it with partgraph db up."

#: Fixed, path-free message clauses composed into the single-line summary. Kept
#: '/'-free so the whole message is safe to print verbatim.
_SCHEMA_MATCH_CLAUSE = "live hnsw index options match the schema file"
_SCHEMA_DRIFT_CLAUSE = (
    "live hnsw index options drifted from the schema file "
    "(run partgraph db apply-schema)"
)
_SELF_PASS_CLAUSE = "the self-similarity probe passed"
_SELF_FAIL_CLAUSE = "the self-similarity probe failed (an embedded part did not find itself)"
_SELF_NONE_CLAUSE = "there are no embedded parts yet, so the self-similarity probe was skipped"
_SELF_INVALID_CLAUSE = (
    "a stored embedding vector was not a valid numeric vector, so the "
    "self-similarity probe was skipped"
)


@dataclass(frozen=True)
class IndexIntegrityResult:
    """Immutable outcome of a single ``check-index`` probe.

    Attributes:
        reachable: True iff the live-schema introspection call answered at all;
            False on a connection failure or a timeout on that first call.
        schema_ok: True iff the live ``hnsw`` options equal the schema-file
            options (both normalized). False when they differ or the live index
            is absent. None ONLY when the live schema query itself failed (which
            always implies ``reachable=False``).
        file_options: The normalized ``hnsw`` options parsed from the schema file
            (a sorted tuple of ``(key, value)`` pairs). Always populated — it is
            computed purely from ``schema_text``, so it survives even an
            unreachable database.
        live_options: The normalized live ``hnsw`` options, or None when the live
            predicate/index is absent or the schema query failed.
        self_similarity_ok: True iff an embedded part's own stored vector, replayed
            through ``similar_to``, returns that part's own uid. False on a
            self-similarity miss OR an invalid stored vector. None when there is
            nothing to self-check (no embedded parts).
        message: A human-readable, path-free, single-line summary safe to print
            verbatim. Never contains a raw exception string or a response body.
    """

    reachable: bool
    schema_ok: bool | None
    file_options: tuple[tuple[str, str], ...]
    live_options: tuple[tuple[str, str], ...] | None
    self_similarity_ok: bool | None
    message: str


def parse_file_hnsw_options(schema_text: str) -> tuple[tuple[str, str], ...]:
    """Extract the ``embedding`` predicate's ``hnsw`` options from schema-file text.

    PURE (no network). Finds the ``embedding:`` predicate line, extracts the
    ``hnsw(...)`` option pairs, normalizes a missing ``exponent`` to
    :data:`DEFAULT_EXPONENT`, and returns a SORTED tuple of ``(key, value)`` pairs.
    Returns an empty tuple when no ``embedding`` line or no ``hnsw(...)`` block is
    present (never a fabricated default).
    """
    line = _find_embedding_line(schema_text)
    if line is None:
        return ()
    block = _HNSW_BLOCK_RE.search(line)
    if block is None:
        return ()
    options = dict(_HNSW_OPTION_RE.findall(block.group(1)))
    options.setdefault("exponent", DEFAULT_EXPONENT)
    return tuple(sorted(options.items()))


def parse_live_hnsw_options(
    schema_json: dict,
) -> tuple[tuple[str, str], ...] | None:
    """Extract the live ``hnsw`` options from a DQL ``schema(pred: [embedding]) {}``.

    PURE (no network). Takes the FULL parsed response body (exactly what
    ``response.json()`` returns — ``{"data": {"schema": [...]}}``), applies the
    same default-``exponent`` normalization as :func:`parse_file_hnsw_options`,
    and returns a SORTED tuple of ``(key, value)`` pairs. Returns None when the
    ``embedding`` predicate is absent (including an empty ``schema`` array) or its
    ``index_specs`` carries no ``hnsw`` entry — never raises, never fabricates.
    """
    spec = _find_live_hnsw_spec(schema_json)
    if spec is None:
        return None
    options: dict[str, str] = {}
    for option in spec.get("options", []):
        if isinstance(option, dict):
            key = option.get("key")
            value = option.get("value")
            if isinstance(key, str) and isinstance(value, str):
                options[key] = value
    options.setdefault("exponent", DEFAULT_EXPONENT)
    return tuple(sorted(options.items()))


def check_index_integrity(
    *,
    schema_text: str,
    url: str = DGRAPH_QUERY_URL,
    timeout: float = INDEX_PROBE_TIMEOUT_S,
    http_post: Callable[..., Any] | None = None,
) -> IndexIntegrityResult:
    """Probe the live vector-index integrity and classify the outcome (ADR-0019).

    All parameters are keyword-only, so a future refactor cannot silently reorder
    them into positional arguments. ``http_post`` is the injectable HTTP seam
    (mirroring :mod:`partgraph.util.health`): tests pass a fake that never opens a
    real socket. It defaults to ``None`` so the real :func:`requests.post` is
    resolved **lazily** — this leaf never imports :mod:`requests` merely by being
    imported. The seam is invoked exactly as ``http_post(url, data=...,
    headers=..., timeout=timeout)`` and must return an object exposing
    ``.status_code`` and ``.json()``.

    Sequence (up to three sequential POSTs to the ONE ``url``):

    1. Live-schema introspection (``schema(pred: [embedding]) {}``). This is the
       reachability gate: a timeout or connection failure short-circuits the WHOLE
       probe (``reachable=False``; ``schema_ok``/``live_options``/
       ``self_similarity_ok`` all None; ``file_options`` still populated from
       ``schema_text``) and no further call is made. On success, the live options
       are compared with the file options to set ``schema_ok``.
    2. First embedded part (``has(embedding), first: 1``). With no embedded part,
       ``self_similarity_ok`` is None and no third call is issued.
    3. Self-similarity replay: the part's OWN stored vector is re-issued through
       ``similar_to(embedding, K, "[...]")``; ``self_similarity_ok`` is True iff
       the part's own uid appears in the result set.

    Security (ADR-0019 / ADR-INJECT): before step 3, every element of the stored
    vector is validated with :func:`_safe_vector_literal` (``repr(float(x))`` +
    strict-charset ``fullmatch``, implemented locally). If the stored value is not
    a list, or any element fails validation, NO third call is issued and a handled
    failure is returned (``self_similarity_ok=False``) — a poisoned/corrupt value
    never reaches the query text.

    Any exception that is NOT a :class:`requests.exceptions.RequestException`
    (e.g. a programming error in an injected seam) is deliberately NOT caught: it
    propagates rather than being coerced into a misleading "unreachable" result
    (no blind ``except Exception``; ruff BLE001).

    Returns:
        A frozen :class:`IndexIntegrityResult`. The ``message`` is always safe to
        print verbatim: never a raw exception string, a response body, or a path.
    """
    # Lazy import: keeps this leaf free of a module-level third-party dependency
    # (a subprocess test pins that importing this module does not pull in
    # ``requests``). ``requests`` is resolved only when a probe actually runs —
    # needed both for the default seam AND for the specific ``requests.exceptions.*``
    # classes caught below. ``requests`` is a declared core dependency
    # (pyproject.toml), so this import cannot fail at runtime.
    import requests  # noqa: PLC0415 — deliberate lazy import; see docstring.

    if http_post is None:
        http_post = requests.post

    # file_options is PURE (no network): always available, even when unreachable.
    file_options = parse_file_hnsw_options(schema_text)

    def _post(query: str) -> Any:
        response = http_post(url, data=query, headers=_DQL_HEADERS, timeout=timeout)
        return response.json()

    try:
        # (1) Live schema introspection — the reachability gate.
        live_options = parse_live_hnsw_options(_post(_SCHEMA_INTROSPECTION_QUERY))
        schema_ok = file_options == live_options

        # (2) Fetch the first embedded part's own stored vector.
        probe = _first_embedded_part(_post(_HAS_EMBEDDING_QUERY))
        if probe is None:
            return IndexIntegrityResult(
                reachable=True,
                schema_ok=schema_ok,
                file_options=file_options,
                live_options=live_options,
                self_similarity_ok=None,
                message=_integrity_message(schema_ok, _SELF_NONE_CLAUSE),
            )

        own_uid, stored_vector = probe

        # SECURITY GATE (ADR-0019): validate every stored element BEFORE it is
        # inlined into the similar_to literal. A non-list or an invalid element
        # yields a handled failure and issues NO third call — the raw stored value
        # never reaches query text.
        vector_literal = _safe_vector_literal(stored_vector)
        if vector_literal is None:
            return IndexIntegrityResult(
                reachable=True,
                schema_ok=schema_ok,
                file_options=file_options,
                live_options=live_options,
                self_similarity_ok=False,
                message=_integrity_message(schema_ok, _SELF_INVALID_CLAUSE),
            )

        # (3) Replay the validated vector through similar_to.
        self_similarity_ok = _own_uid_in_results(
            _post(_similar_to_query(vector_literal)), own_uid
        )
        return IndexIntegrityResult(
            reachable=True,
            schema_ok=schema_ok,
            file_options=file_options,
            live_options=live_options,
            self_similarity_ok=self_similarity_ok,
            message=_integrity_message(
                schema_ok,
                _SELF_PASS_CLAUSE if self_similarity_ok else _SELF_FAIL_CLAUSE,
            ),
        )
    except requests.exceptions.Timeout:
        return _unreachable_result(
            file_options, f"Dgraph index probe timed out after {timeout}s."
        )
    except requests.exceptions.RequestException:
        return _unreachable_result(file_options, _UNREACHABLE_MESSAGE)


# ---------------------------------------------------------------------------
# Internal helpers (pure; no network, no exception coercion beyond the seam)
# ---------------------------------------------------------------------------

def _find_embedding_line(schema_text: str) -> str | None:
    """Return the schema-file line declaring the ``embedding:`` predicate, or None."""
    for line in schema_text.splitlines():
        if line.lstrip().startswith("embedding:"):
            return line
    return None


def _live_schema_entries(schema_json: Any) -> list:
    """Return the ``data.schema`` predicate list from a live response body, or [].

    Defensively navigates ``{"data": {"schema": [...]}}``: any missing or
    wrong-typed level yields an empty list rather than raising.
    """
    if not isinstance(schema_json, dict):
        return []
    data = schema_json.get("data")
    if not isinstance(data, dict):
        return []
    schema_list = data.get("schema")
    return schema_list if isinstance(schema_list, list) else []


def _find_live_hnsw_spec(schema_json: Any) -> dict | None:
    """Return the live ``hnsw`` index-spec dict for ``embedding``, or None.

    An absent ``embedding`` predicate, an absent/empty ``index_specs``, or an
    ``index_specs`` carrying no ``hnsw`` entry all yield None.
    """
    for entry in _live_schema_entries(schema_json):
        if isinstance(entry, dict) and entry.get("predicate") == "embedding":
            index_specs = entry.get("index_specs")
            specs = index_specs if isinstance(index_specs, list) else []
            for spec in specs:
                if isinstance(spec, dict) and spec.get("name") == "hnsw":
                    return spec
            return None
    return None


def _first_embedded_part(body: Any) -> tuple[Any, Any] | None:
    """Return ``(uid, stored_vector)`` of the first ``has(embedding)`` row, or None.

    None means the query returned no embedded part (nothing to self-check). Any
    malformed body shape also degrades to None rather than raising.
    """
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    rows = data.get("q")
    if not isinstance(rows, list) or not rows:
        return None
    first = rows[0]
    if not isinstance(first, dict):
        return None
    return first.get("uid"), first.get("embedding")


def _fmt_float(value: Any) -> str:
    """Return a locale-invariant, charset-validated float literal for *value*.

    Mirrors :func:`partgraph.query.dql_builder._fmt_float`'s discipline (a
    locally-owned copy — this leaf never imports that builder): ``repr(float(x))``
    round-trips exactly and is locale-invariant, and the result is validated
    against a strict ``[0-9.eE+-]`` charset so a malformed literal (or a non-finite
    ``inf``/``nan`` whose ``repr`` carries letters) can never reach the query text.

    Raises:
        ValueError: If *value* cannot be coerced to a float, or the formatted
            literal contains any character outside the permitted numeric set.
        TypeError: If *value* is of a type ``float()`` cannot coerce.
    """
    text = repr(float(value))
    if not _FLOAT_LITERAL_RE.fullmatch(text):
        raise ValueError(f"Unsafe float literal: {text!r}")
    return text


def _safe_vector_literal(stored: Any) -> str | None:
    """Return a validated ``"[f0, f1, ...]"`` literal for *stored*, or None.

    Security gate (ADR-0019): returns None — signalling "do NOT issue the
    similar_to call" — when *stored* is not a list, or when ANY element fails the
    strict float validation in :func:`_fmt_float`. A poisoned/corrupt stored value
    is therefore never interpolated into query text.
    """
    if not isinstance(stored, list):
        return None
    parts: list[str] = []
    for element in stored:
        try:
            parts.append(_fmt_float(element))
        except (TypeError, ValueError):
            return None
    return "[" + ", ".join(parts) + "]"


def _similar_to_query(vector_literal: str) -> str:
    """Return the self-similarity replay query for an already-validated literal."""
    return (
        f'{{ q(func: similar_to(embedding, {_SELF_SIMILARITY_K}, '
        f'"{vector_literal}")) {{ uid }} }}'
    )


def _own_uid_in_results(body: Any, own_uid: Any) -> bool:
    """Return True iff *own_uid* appears among the ``similar_to`` result rows."""
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    rows = data.get("q")
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and row.get("uid") == own_uid for row in rows)


def _integrity_message(schema_ok: bool, self_clause: str) -> str:
    """Compose the fixed, single-line, path-free summary from two fixed clauses."""
    schema_clause = _SCHEMA_MATCH_CLAUSE if schema_ok else _SCHEMA_DRIFT_CLAUSE
    return f"Index integrity: {schema_clause}; {self_clause}."


def _unreachable_result(
    file_options: tuple[tuple[str, str], ...], message: str
) -> IndexIntegrityResult:
    """Build the reachable=False result for a first-call network failure."""
    return IndexIntegrityResult(
        reachable=False,
        schema_ok=None,
        file_options=file_options,
        live_options=None,
        self_similarity_ok=None,
        message=message,
    )
