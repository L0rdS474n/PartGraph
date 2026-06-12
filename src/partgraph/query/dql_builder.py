"""Injection-safe DQL builders for component search and detail queries.

This module turns a :class:`~partgraph.query.parser.ParsedQuery` (and a single
normalised MPN, for the detail view) into a ``(query_text, variables)`` pair
ready for ``txn.query(query_text, variables=variables)``.

Security model (ADR-INJECT):
- Free-text tokens and the package code are *never* interpolated into the query
  string. They are bound as Dgraph ``$``-variables, so hostile characters stay
  inside the variable value and can never alter the query structure.
- Numeric parameter bounds are emitted as *float literals* (Dgraph variables are
  strings and cannot type as floats for ``ge``/``le``). Every literal is produced
  by :func:`_fmt_float`, which forces a locale-invariant representation and
  validates it against a strict numeric charset before it can reach the query.
- The package code is re-validated against ``^[A-Z0-9][A-Z0-9\\-]{0,19}$`` and a
  failure raises :class:`ValueError` (defence in depth on top of the parser).

DoS model (ADR-0007): the caller-supplied ``limit`` is clamped to
``MAX_RESULT_LIMIT`` so a single request can never stream the whole database.

Parametric brackets (ADR-PARAM):
- resistance ........ +/-1%
- capacitance ....... +/-5%
- inductance ........ +/-5%
- current_max ....... +/-5%
- power ............. +/-5%
- voltage_max ....... +/-2%
- voltage_min ....... +/-2%
- frequency_max ..... +/-1%
- tolerance_pct ..... EXACT (eq)
"""

from __future__ import annotations

import re

from partgraph.normalize.model import normalize_mpn
from partgraph.query.parser import ParsedQuery

__all__ = [
    "MAX_RESULT_LIMIT",
    "build_search_dql",
    "build_semantic_dql",
    "build_show_dql",
]

#: Maximum number of rows any single block may return (ADR-0007 DoS bound).
MAX_RESULT_LIMIT = 200

#: Required embedding dimension (all-MiniLM-L6-v2; ADR-0008). Every vector that
#: reaches the semantic builder must be exactly this long.
EMBED_DIM = 384

#: Tolerance fraction applied to each promoted predicate to form a ge/le bracket
#: around the target value (ADR-PARAM). ``tolerance_pct`` is intentionally absent
#: here: it is matched with an exact ``eq``.
_BRACKET_FRACTION: dict[str, float] = {
    "resistance": 0.01,
    "frequency_max": 0.01,
    "capacitance": 0.05,
    "inductance": 0.05,
    "current_max": 0.05,
    "power": 0.05,
    "voltage_max": 0.02,
    "voltage_min": 0.02,
}

#: Predicates matched exactly with ``eq`` rather than a bracket (ADR-PARAM).
_EXACT_PREDICATES = frozenset({"tolerance_pct"})

#: Promoted numeric predicates selected on every returned row so the ranker can
#: propagate them onto RankedRow and the renderer can show them.
_PROMOTED_PREDICATES: tuple[str, ...] = (
    "voltage_min",
    "voltage_max",
    "current_max",
    "resistance",
    "capacitance",
    "inductance",
    "frequency_max",
    "power",
    "tolerance_pct",
)

#: Strict charset a formatted float literal must match before use in a query.
_FLOAT_LITERAL_RE = re.compile(r"[0-9.eE+\-]+")

#: Package validation regex (ADR-INJECT). Mirrors the parser's final check.
_PACKAGE_VALID_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,19}$")

#: Minimum letter-run length to use as the related-parts MPN family prefix.
_MIN_RELATED_PREFIX_LEN = 2


def _fmt_float(value: float) -> str:
    """Return a locale-invariant literal for *value*, validated for safety.

    ``repr`` on a float never uses a locale-specific decimal separator and
    round-trips exactly, so it is safe regardless of the runtime locale. The
    result is validated against a strict ``[0-9.eE+-]`` charset so a malformed
    literal can never reach the query text.

    Raises:
        ValueError: If the formatted value contains any character outside the
            permitted numeric set (defensive — should not occur for floats).
    """
    text = repr(float(value))
    if not _FLOAT_LITERAL_RE.fullmatch(text):  # pragma: no cover — defensive
        raise ValueError(f"Unsafe float literal: {text!r}")
    return text


def _validate_package(package: str) -> str:
    """Return *package* unchanged if it passes the injection-guard regex.

    Raises:
        ValueError: If *package* does not match ``^[A-Z0-9][A-Z0-9\\-]{0,19}$``.
    """
    if not _PACKAGE_VALID_RE.match(package):
        raise ValueError(
            f"Invalid package code {package!r}: must match "
            r"^[A-Z0-9][A-Z0-9\-]{0,19}$ (ADR-INJECT)."
        )
    return package


def _param_filter_terms(parsed: ParsedQuery) -> list[str]:
    """Return the per-quantity DQL filter terms (float literals, ADR-PARAM).

    Bracketed predicates become ``ge(pred, lo)`` and ``le(pred, hi)``; exact
    predicates become ``eq(pred, value)``. All numbers are float literals.
    """
    terms: list[str] = []
    for quantity in parsed.quantities:
        pred = quantity.predicate
        value = quantity.value
        if pred in _EXACT_PREDICATES:
            terms.append(f"eq({pred}, {_fmt_float(value)})")
            continue
        fraction = _BRACKET_FRACTION.get(pred)
        if fraction is None:
            # Unknown predicate: fall back to an exact match rather than an
            # unbounded range. (Parser only emits known predicates.)
            terms.append(f"eq({pred}, {_fmt_float(value)})")
            continue
        lo = value * (1.0 - fraction)
        hi = value * (1.0 + fraction)
        terms.append(f"ge({pred}, {_fmt_float(lo)})")
        terms.append(f"le({pred}, {_fmt_float(hi)})")
    return terms


def _render_fields(indent: str, *, has_package: bool, package_var: str) -> str:
    """Return the shared selection set rendered for every search block."""
    lines = [
        f"{indent}uid",
        f"{indent}mpn",
        f"{indent}mpn_norm",
        f"{indent}stock",
        f"{indent}is_basic",
    ]
    lines.extend(f"{indent}{pred}" for pred in _PROMOTED_PREDICATES)
    lines.append(f"{indent}made_by {{ name }}")
    if has_package:
        lines.append(f"{indent}in_package @filter(eq(name, {package_var})) {{ name }}")
    else:
        lines.append(f"{indent}in_package {{ name }}")
    lines.append(f"{indent}datasheet {{ url }}")
    return "\n".join(lines)


def _build_block(  # noqa: PLR0913 — keyword-only block descriptor; cohesive unit
    *,
    name: str,
    text_term: str | None,
    param_terms: list[str],
    has_package: bool,
    package_var: str,
    first: int,
) -> str:
    """Render a single named search block.

    Args:
        name: Block name (``exact`` / ``trig`` / ``fts``).
        text_term: The fully-formed text-matching filter term referencing a
            ``$``-variable (e.g. ``"eq(mpn_norm, $te)"`` /
            ``"regexp(mpn_norm, $rx)"`` / ``"anyoftext(description, $ft)"``), or
            ``None`` to root on the parametric filter only.
        param_terms: Parametric filter terms (float literals).
        has_package: Whether a package filter should be applied.
        package_var: The ``$``-variable holding the package name.
        first: The (already clamped) row cap for this block.
    """
    filter_terms: list[str] = []
    if text_term is not None:
        filter_terms.append(text_term)
    filter_terms.extend(param_terms)
    # A search hit is only useful when it is datasheet-backed: require at least
    # one datasheet edge so every surfaced row carries a datasheet URL.
    filter_terms.append("has(datasheet)")

    filter_clause = ""
    if filter_terms:
        filter_clause = " @filter(" + " AND ".join(filter_terms) + ")"

    # @cascade(in_package) drops parts whose in_package filter prunes to empty,
    # so the package acts as a real constraint without inlining its value. The
    # cascade is scoped to in_package so unrelated optional edges (made_by /
    # datasheet) do not additionally prune otherwise-matching parts.
    cascade = " @cascade(in_package)" if has_package else ""

    body = _render_fields("    ", has_package=has_package, package_var=package_var)
    return (
        f"  {name}(func: type(Part), first: {first}){filter_clause}{cascade} {{\n"
        f"{body}\n"
        f"  }}"
    )


def build_search_dql(
    parsed: ParsedQuery,
    *,
    limit: int = 20,
) -> tuple[str, dict[str, str]]:
    """Build the multi-block search DQL and its variable map.

    Returns ``(query_text, variables)``. The query declares typed ``string``
    variables for every text token and (when present) the package; numeric
    bounds are inline float literals. The per-block ``first:`` cap is clamped to
    ``MAX_RESULT_LIMIT`` (ADR-0007).

    Raises:
        ValueError: If a package code fails the injection-guard regex.
    """
    first = max(1, min(int(limit), MAX_RESULT_LIMIT))

    variables: dict[str, str] = {}
    var_decls: list[str] = []

    has_package = parsed.package is not None
    package_var = "$pkg"
    if has_package:
        validated = _validate_package(parsed.package)  # type: ignore[arg-type]
        variables[package_var] = validated
        var_decls.append(f"{package_var}: string")

    param_terms = _param_filter_terms(parsed)

    # Text tokens drive the exact / trig / fts blocks. ``mpn_norm`` is stored
    # normalised (uppercase [A-Z0-9]), so the token is normalised the same way
    # before exact/regexp matching; ``description`` is full-text and matched on
    # the raw token (Dgraph full-text search is case-insensitive). Every value is
    # bound as a ``$``-variable so no untrusted string reaches the query text
    # (ADR-INJECT). The trigram tier uses ``regexp(mpn_norm, $rx)`` where ``$rx``
    # is ``/<re.escape(normalised-token)>/`` — the only v25-supported precise
    # substring match for a trigram-indexed predicate (anyofterms needs a term
    # index, which mpn_norm does not have).
    text_tokens = parsed.text_tokens
    exact_term: str | None = None
    trig_term: str | None = None
    fts_term: str | None = None
    if text_tokens:
        raw_joined = " ".join(text_tokens)
        norm_joined = normalize_mpn(raw_joined)

        # Exact: full normalised string equality.
        variables["$te"] = norm_joined
        var_decls.append("$te: string")
        exact_term = "eq(mpn_norm, $te)"

        # Trigram: anchored regexp on the escaped normalised token.
        variables["$rx"] = "/" + re.escape(norm_joined) + "/"
        var_decls.append("$rx: string")
        trig_term = "regexp(mpn_norm, $rx)"

        # Full text: raw token against the description full-text index.
        variables["$ft"] = raw_joined
        var_decls.append("$ft: string")
        fts_term = "anyoftext(description, $ft)"

    blocks: list[str] = []
    if text_tokens:
        blocks.append(
            _build_block(
                name="exact",
                text_term=exact_term,
                param_terms=param_terms,
                has_package=has_package,
                package_var=package_var,
                first=first,
            )
        )
        blocks.append(
            _build_block(
                name="trig",
                text_term=trig_term,
                param_terms=param_terms,
                has_package=has_package,
                package_var=package_var,
                first=first,
            )
        )
        blocks.append(
            _build_block(
                name="fts",
                text_term=fts_term,
                param_terms=param_terms,
                has_package=has_package,
                package_var=package_var,
                first=first,
            )
        )
    else:
        # No free text: a single parametric/package block under the "exact" name
        # so rank_results treats these rows as the top tier. No trig/fts blocks
        # are emitted (rank_results tolerates their absence); emitting an
        # ``eq(mpn_norm, "")`` placeholder would wrongly match parts with an
        # empty mpn_norm and pollute the results with a blank row.
        blocks.append(
            _build_block(
                name="exact",
                text_term=None,
                param_terms=param_terms,
                has_package=has_package,
                package_var=package_var,
                first=first,
            )
        )

    header = ""
    if var_decls:
        header = "query search(" + ", ".join(var_decls) + ") "

    query_text = header + "{\n" + "\n".join(blocks) + "\n}"
    return query_text, variables


def build_semantic_dql(
    vector: list[float],
    k: int,
    *,
    parsed: ParsedQuery | None = None,
) -> tuple[str, dict[str, str]]:
    """Build the semantic (vector-similarity) search DQL and its variable map.

    Returns ``(query_text, variables)`` for a single ``semantic`` block rooted on
    ``similar_to(embedding, k, "[...]")``. The query selects the same render
    fields as :func:`build_search_dql` so the ranker/renderer treat semantic rows
    uniformly.

    Security (ADR-INJECT / ADR-0008):
    - The query vector is embedded as an **inline quoted literal**, never as a
      ``$``-variable (Dgraph's ``similar_to`` requires a literal vector). To make
      that safe, **every** element is forced through :func:`_fmt_float`
      (``repr(float(x))`` validated against the strict numeric charset), so a
      hostile non-float element raises ``ValueError``/``TypeError`` before it can
      reach the query text and cannot break out of the literal.
    - The *human* semantic query text is never part of the DQL: only the
      validated float vector is inlined. Hybrid parametric/package filters from
      *parsed* are added via the same injection-safe helpers PR3 uses
      (``_param_filter_terms`` float literals; the package bound as ``$pkg``).

    DoS (ADR-0007): ``k`` is clamped to ``[1, MAX_RESULT_LIMIT]`` so a single
    request can never ask Dgraph for an unbounded neighbour set.

    Args:
        vector: The query embedding; must be length :data:`EMBED_DIM` (384).
        k: Requested number of nearest neighbours (clamped to the DoS bound).
        parsed: Optional parsed query supplying hybrid package / parametric
            filters layered on top of the vector search.

    Raises:
        ValueError: If *vector* is not length 384, if any element is not a finite
            float literal, or if a package code fails the injection-guard regex.
        TypeError: If an element cannot be coerced to ``float``.
    """
    if len(vector) != EMBED_DIM:
        raise ValueError(
            f"Embedding vector must have exactly {EMBED_DIM} dimensions; "
            f"got {len(vector)}."
        )

    # Validate-and-format every element. _fmt_float runs repr(float(x)) and the
    # strict-charset fullmatch, so a non-numeric element raises here (never
    # reaching the inline literal).
    literal_parts = [_fmt_float(component) for component in vector]
    vector_literal = "[" + ", ".join(literal_parts) + "]"

    # Clamp k into [1, MAX_RESULT_LIMIT] (DoS bound; never 0/negative/huge).
    clamped_k = max(1, min(int(k), MAX_RESULT_LIMIT))

    variables: dict[str, str] = {}
    var_decls: list[str] = []

    has_package = parsed is not None and parsed.package is not None
    package_var = "$pkg"
    if has_package:
        validated = _validate_package(parsed.package)  # type: ignore[arg-type,union-attr]
        variables[package_var] = validated
        var_decls.append(f"{package_var}: string")

    # Hybrid parametric filters (float literals — injection-safe, ADR-PARAM).
    param_terms = _param_filter_terms(parsed) if parsed is not None else []

    filter_terms: list[str] = list(param_terms)
    # A semantic hit is only useful when datasheet-backed (same as PR3 blocks).
    filter_terms.append("has(datasheet)")

    filter_clause = " @filter(" + " AND ".join(filter_terms) + ")"
    # When a package is present, _render_fields emits
    # ``in_package @filter(eq(name, $pkg))`` and the cascade prunes parts whose
    # package filter is empty — so the package acts as a real constraint without
    # inlining its value (mirrors the PR3 search blocks).
    cascade = " @cascade(in_package)" if has_package else ""

    body = _render_fields("    ", has_package=has_package, package_var=package_var)

    header = ""
    if var_decls:
        header = "query semantic(" + ", ".join(var_decls) + ") "

    query_text = (
        f"{header}{{\n"
        f"  semantic(func: similar_to(embedding, {clamped_k}, "
        f'"{vector_literal}")){filter_clause}{cascade} {{\n'
        f"{body}\n"
        f"  }}\n"
        f"}}"
    )
    return query_text, variables


def _related_prefix(mpn_norm: str) -> str:
    """Return a short alphabetic-family prefix of *mpn_norm* for related search.

    Uses the leading run of letters (e.g. ``"MAX"`` from ``"MAX232CPE"``),
    falling back to the first few characters. The result is bound as a ``$``
    variable, never inlined, so it carries no injection risk.
    """
    upper = mpn_norm.upper()
    letters = re.match(r"[A-Z]+", upper)
    if letters and len(letters.group(0)) >= _MIN_RELATED_PREFIX_LEN:
        return letters.group(0)
    return upper[:3]


def build_show_dql(mpn_norm: str) -> tuple[str, dict[str, str]]:
    """Build the detail (``show``) DQL for a single normalised MPN.

    Returns ``(query_text, variables)``. The part is selected by
    ``eq(mpn_norm, $m)`` and a sibling ``related`` block finds similar parts by
    MPN similarity — never via ``variant_of``/``family_name`` (UNPOPULATED).

    Related-parts matching: ``mpn_norm`` carries a ``trigram`` (not ``term``)
    index in the deployed schema, so the ``anyofterms`` term-search the original
    contract names is not executable against it in Dgraph v25. The block instead
    uses ``regexp(mpn_norm, $rel)`` over the trigram index — the v25-supported,
    injection-safe equivalent (the pattern is an escaped ``$``-variable). The
    intent is documented inline with an ``anyofterms``-style note so the
    "MPN-similarity, not variant_of/family_name" contract stays explicit.

    Both the MPN and the derived related-prefix are bound as ``$``-variables; no
    untrusted value is inlined (ADR-INJECT).
    """
    variables: dict[str, str] = {
        "$m": mpn_norm,
        "$rel": "/" + re.escape(_related_prefix(mpn_norm)) + "/",
    }

    query_text = (
        "query show($m: string, $rel: string) {\n"
        "  part(func: eq(mpn_norm, $m), first: 1) {\n"
        "    uid\n"
        "    mpn\n"
        "    mpn_norm\n"
        "    description\n"
        "    stock\n"
        "    is_basic\n"
        "    price_usd\n"
        "    lcsc_id\n"
        "    voltage_min\n"
        "    voltage_max\n"
        "    current_max\n"
        "    resistance\n"
        "    capacitance\n"
        "    inductance\n"
        "    frequency_max\n"
        "    power\n"
        "    tolerance_pct\n"
        "    made_by { name }\n"
        "    in_category { name }\n"
        "    in_package { name }\n"
        "    datasheet { url source }\n"
        "    tagged { name }\n"
        "    attr { attr_name attr_value attr_value_num }\n"
        "  }\n"
        "  # related parts by MPN similarity (anyofterms-style intent, executed\n"
        "  # via the mpn_norm trigram index using regexp; no parent traversal).\n"
        "  related(func: regexp(mpn_norm, $rel), first: 10)"
        " @filter(NOT eq(mpn_norm, $m)) {\n"
        "    uid\n"
        "    mpn\n"
        "    mpn_norm\n"
        "    made_by { name }\n"
        "    in_package { name }\n"
        "  }\n"
        "}"
    )
    return query_text, variables
