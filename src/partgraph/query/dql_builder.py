"""Injection-safe DQL builders for component search and detail queries.

This module turns a :class:`~partgraph.query.parser.ParsedQuery` (and a single
normalised MPN, for the detail view) into a ``(query_text, variables)`` pair
ready for ``txn.query(query_text, variables=variables)``.

Security model (ADR-INJECT):
- Free-text tokens, the package code and the ``--manufacturer``/``--category``
  filter terms are *never* interpolated into the query string. They are bound as
  Dgraph ``$``-variables (``$te``/``$rx``/``$ft``, ``$pkg``, ``$mfr``, ``$cat``),
  so hostile characters stay inside the variable value and can never alter the
  query structure. Manufacturer/category are matched with ``allofterms`` on the
  bound ``$``-variable — never ``regexp`` on user input (ADR-0016).
- Numeric parameter bounds and the ``--max-price`` ceiling are emitted as *float
  literals* (Dgraph variables are strings and cannot type as floats for
  ``ge``/``le``). Every literal is produced by :func:`_fmt_float`, which forces a
  locale-invariant representation and validates it against a strict numeric
  charset before it can reach the query.
- The ``--min-stock``/``--in-stock`` threshold is emitted as a validated *integer
  literal* by :func:`_fmt_int` (``ge(stock, 5)`` — never ``ge(stock, 5.0)``,
  which Dgraph rejects for the integer ``stock`` predicate; ADR-0016). The
  ``--basic``/``--extended`` flag is emitted as the fixed boolean literal
  ``eq(is_basic, true)``/``eq(is_basic, false)`` — never derived from or bound to
  user text.
- The package code is re-validated against ``^[A-Z0-9][A-Z0-9\\-]{0,19}\\Z`` and a
  failure raises :class:`ValueError` (defence in depth on top of the parser). The
  manufacturer/category terms use a separate *permissive* validator
  (:func:`_validate_filter_term`) — they legitimately contain spaces and exceed
  20 characters, so the strict package charset cannot be reused; that validator
  rejects empty/whitespace-only values and caps the length at
  :data:`MAX_FILTER_TERM_LEN` (DoS defence-in-depth, ADR-0007 style).

DoS model (ADR-0007 / ADR-0020): the lexical :func:`build_search_dql` clamps the
caller-supplied ``limit`` to :data:`MAX_RESULT_LIMIT` (the RESULT bound) so a
single request can never stream the whole database. The semantic
:func:`build_semantic_dql` clamps its neighbour count ``k`` to the larger
:data:`SEMANTIC_CANDIDATE_CAP` (the CANDIDATE bound): the CLI oversamples the
candidate pool so the client-side cosine re-rank has enough neighbours to
truncate back down to :data:`MAX_RESULT_LIMIT`, yet a single request still can
never ask Dgraph for an unbounded neighbour set.

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
    "MAX_FILTER_TERM_LEN",
    "MAX_RESULT_LIMIT",
    "SEMANTIC_CANDIDATE_CAP",
    "SEMANTIC_CANDIDATE_FLOOR",
    "SEMANTIC_OVERSAMPLE_FACTOR",
    "build_search_dql",
    "build_semantic_dql",
    "build_show_dql",
    "validate_package",
]

#: Maximum number of rows any single block may return, and the number of rows a
#: search RESULT may ever contain (ADR-0007 DoS bound). Still the RESULT bound
#: for BOTH the lexical and the semantic path; the ranker truncates a
#: cosine-reranked semantic result back down to this value (ADR-0020).
MAX_RESULT_LIMIT = 200

#: Upper bound on the semantic neighbour count ``k`` (the CANDIDATE bound;
#: ADR-0020). ``build_semantic_dql`` clamps ``k`` to this — NOT to
#: :data:`MAX_RESULT_LIMIT` — so the CLI's oversampled candidate pool (up to this
#: many nearest neighbours) can pass straight through to ``similar_to`` while a
#: single request still can never ask Dgraph for an unbounded neighbour set. The
#: worst-case response (1500 x 384 float32 ~= 6 MB) stays far under the 256 MiB
#: gRPC ceiling (cli.py ``_GRPC_MAX_MESSAGE_BYTES``); see ADR-0020.
SEMANTIC_CANDIDATE_CAP = 1500

#: Floor for the CLI's oversampled candidate count: even a tiny ``--limit`` asks
#: Dgraph for at least this many neighbours so the client-side cosine re-rank has
#: a meaningful pool to choose the top results from (ADR-0020).
SEMANTIC_CANDIDATE_FLOOR = 200

#: Multiplier the CLI applies to ``--limit`` when sizing the semantic candidate
#: pool: ``candidate_k = min(max(limit * SEMANTIC_OVERSAMPLE_FACTOR,
#: SEMANTIC_CANDIDATE_FLOOR), SEMANTIC_CANDIDATE_CAP)`` (ADR-0020).
SEMANTIC_OVERSAMPLE_FACTOR = 20

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

#: Strict charset a formatted integer literal must match before use in a query.
#: Only non-negative integers reach the query (``stock`` is a count), so no sign
#: is permitted here (validate-before-emit, mirroring :data:`_FLOAT_LITERAL_RE`).
_INT_LITERAL_RE = re.compile(r"[0-9]+")

#: Package validation regex (ADR-INJECT). Mirrors the parser's final check.
#: Anchored with ``\Z``, never ``$``: Python's ``$`` also matches just before a
#: trailing newline, so ``^...$`` would admit ``"SOIC-16\n"`` into query text.
_PACKAGE_VALID_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,19}\Z")

#: Maximum length of a ``--manufacturer``/``--category`` free-text filter term
#: (ADR-0016 / ADR-0007-style DoS bound). Must comfortably exceed real names
#: ("STMicroelectronics", "RS-232 Interface IC") yet reject pathological input;
#: the value is bound as a ``$``-variable regardless, so this is a usability +
#: defence-in-depth cap, not an injection guard. PUBLIC (ADR-0017): the CLI
#: imports it directly rather than reaching for a private name.
MAX_FILTER_TERM_LEN = 128

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


def _fmt_int(value: int) -> str:
    """Return a safe, non-negative integer literal for *value*.

    The ``stock`` predicate is an integer in the deployed schema and Dgraph
    rejects a *float* literal for an integer comparison (LIVE-CONFIRMED:
    ``ge(stock, 5.0)`` errors; ``ge(stock, 5)`` works). This helper therefore
    emits a bare integer literal and refuses anything that is not a whole,
    non-negative integer so a fractional/negative/non-numeric value can never
    reach the query text. The emitted text is re-validated against a strict
    digit charset before use (validate-before-emit, mirroring :func:`_fmt_float`).

    Raises:
        ValueError: If *value* is a bool, carries a fractional part, is negative,
            or cannot be interpreted as an integer.
        TypeError: If *value* is of a type ``int()`` cannot coerce.
    """
    # A bool is an int subclass; reject it so ``True`` can never become ``1``.
    if isinstance(value, bool):
        raise ValueError(f"Integer literal must not be a bool: {value!r}.")
    # Guard the fractional-float case explicitly: int(5.5) would silently
    # truncate to 5, so a value like 5.5 must be rejected before coercion.
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"Integer literal must be a whole number, got {value!r}.")
        ivalue = int(value)
    else:
        ivalue = int(value)  # non-numeric str/other raises ValueError/TypeError.
    if ivalue < 0:
        raise ValueError(f"Integer literal must be non-negative, got {ivalue}.")
    text = str(ivalue)
    if not _INT_LITERAL_RE.fullmatch(text):  # pragma: no cover — defensive
        raise ValueError(f"Unsafe integer literal: {text!r}.")
    return text


def validate_package(package: str) -> str:
    """Return *package* unchanged if it passes the injection-guard regex.

    PUBLIC (ADR-0017): the CLI imports this directly (``--package`` boundary
    re-validation) rather than reaching for a private name. The charset
    (``^[A-Z0-9][A-Z0-9\\-]{0,19}\\Z``) and logic are unchanged from the
    original private ``_validate_package``.

    Raises:
        ValueError: If *package* does not match ``^[A-Z0-9][A-Z0-9\\-]{0,19}\\Z``.
    """
    if not _PACKAGE_VALID_RE.match(package):
        raise ValueError(
            f"Invalid package code {package!r}: must match "
            r"^[A-Z0-9][A-Z0-9\-]{0,19}\Z (ADR-INJECT)."
        )
    return package


def _validate_filter_term(value: str, *, field: str) -> str:
    """Return *value* unchanged if it is a usable free-text filter term.

    A *permissive* validator distinct from :func:`validate_package`:
    manufacturer and category names legitimately contain spaces and can exceed
    20 characters, so the strict package charset cannot be reused. The value is
    always bound as a Dgraph ``$``-variable (never inlined), so this is a
    usability + DoS guard rather than an injection guard — it rejects an
    empty/whitespace-only term and caps the length at :data:`MAX_FILTER_TERM_LEN`
    (defence-in-depth bound, ADR-0016 / ADR-0007 style).

    Args:
        value: The raw filter term (bound verbatim; never uppercased/stripped).
        field: Human-readable field name used in the raised message.

    Raises:
        ValueError: If *value* is empty/whitespace-only or exceeds the length cap.
    """
    if not value.strip():
        raise ValueError(f"{field} filter must be a non-empty value.")
    if len(value) > MAX_FILTER_TERM_LEN:
        raise ValueError(
            f"{field} filter must be at most {MAX_FILTER_TERM_LEN} characters."
        )
    return value


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


def _scalar_filter_terms(
    *,
    min_stock: int | None,
    is_basic: bool | None,
    max_price: float | None,
) -> list[str]:
    """Return the root-level scalar filter terms for the structured filters.

    Each term is an injection-safe literal added to the block ``@filter`` clause
    (ADR-0016):
    - ``min_stock`` -> ``ge(stock, <int>)`` via :func:`_fmt_int` (integer literal;
      ``--in-stock`` is threaded as ``min_stock=1``).
    - ``is_basic`` -> the fixed boolean literal ``eq(is_basic, true|false)``
      (tri-state: ``None`` omits the term entirely).
    - ``max_price`` -> ``le(price_usd, <float>)`` via :func:`_fmt_float`.

    Raises:
        ValueError: If ``min_stock`` is not a whole, non-negative integer, or if
            ``max_price`` cannot be formatted as a safe float literal.
    """
    terms: list[str] = []
    if min_stock is not None:
        terms.append(f"ge(stock, {_fmt_int(min_stock)})")
    if is_basic is not None:
        terms.append(f"eq(is_basic, {'true' if is_basic else 'false'})")
    if max_price is not None:
        terms.append(f"le(price_usd, {_fmt_float(max_price)})")
    return terms


def _render_fields(  # noqa: PLR0913 — keyword-only selection descriptor; cohesive
    indent: str,
    *,
    has_package: bool,
    package_var: str,
    has_manufacturer: bool = False,
    manufacturer_var: str = "$mfr",
    has_category: bool = False,
    category_var: str = "$cat",
    include_embedding: bool = False,
) -> str:
    """Return the shared selection set rendered for every search block.

    ``include_embedding`` is set ONLY by the semantic path
    (:func:`build_semantic_dql`): it appends a bare ``embedding`` field (the raw
    384-float vector) after ``datasheet`` so the ranker can compute cosine
    similarity client-side (ADR-0020). It defaults to ``False``, so the lexical
    :func:`build_search_dql` never selects ``embedding`` and its response stays
    byte-identical (the 384-float vector never travels the lexical path).

    Scalar fields are fixed and always selected, including a bare ``price_usd``
    (positioned immediately after ``is_basic``) so every row carries a price for
    ``--json`` / ``--sort price`` (ADR-0017). ``price_usd`` is a plain selected
    field, NOT a promoted predicate: it is never query-parsed into a ge/le
    bracket, so it is deliberately absent from :data:`_PROMOTED_PREDICATES` (which
    drives ADR-PARAM bracket semantics for query-derived quantities).

    ``made_by`` and ``in_category`` are BOTH selected unconditionally (so every
    row can report its manufacturer/category even with no filter active), each
    gaining its ``@filter(allofterms(name, $mfr|$cat))`` clause only when its own
    filter is active; ``in_package`` gains ``@filter(eq(name, $pkg))`` only when a
    package filter is active.

    Layout (ADR-0016, retained): when a manufacturer/category filter is active
    the *constrained* edges lead the selection (order ``in_package``,
    ``made_by``, ``in_category``) so the active constraint immediately follows the
    block's ``@cascade`` clause; the no-filter and package-only paths keep the
    historical trailing layout. Filter/cascade BEHAVIOUR is unchanged — only the
    selection surface is extended (bare ``price_usd`` + unconditional
    ``in_category``).
    """

    def made_by_line() -> str:
        if has_manufacturer:
            return f"{indent}made_by @filter(allofterms(name, {manufacturer_var})) {{ name }}"
        return f"{indent}made_by {{ name }}"

    def in_category_line() -> str:
        if has_category:
            return f"{indent}in_category @filter(allofterms(name, {category_var})) {{ name }}"
        return f"{indent}in_category {{ name }}"

    def in_package_line() -> str:
        if has_package:
            return f"{indent}in_package @filter(eq(name, {package_var})) {{ name }}"
        return f"{indent}in_package {{ name }}"

    # The manufacturer/category filters switch on the leading-constraints layout
    # (the constrained edge leads the block); package alone keeps the historical
    # trailing layout for backward-compatible output.
    lead_with_constraints = has_manufacturer or has_category

    lines: list[str] = []
    if lead_with_constraints:
        if has_package:
            lines.append(in_package_line())
        if has_manufacturer:
            lines.append(made_by_line())
        if has_category:
            lines.append(in_category_line())

    lines.extend(
        [
            f"{indent}uid",
            f"{indent}mpn",
            f"{indent}mpn_norm",
            f"{indent}stock",
            f"{indent}is_basic",
            f"{indent}price_usd",
        ]
    )
    lines.extend(f"{indent}{pred}" for pred in _PROMOTED_PREDICATES)

    if lead_with_constraints:
        # Only the edges NOT already emitted in the leading group remain. made_by
        # and in_category are unconditional, so a non-leading one still trails
        # here as a plain (unfiltered) selection.
        if not has_manufacturer:
            lines.append(made_by_line())
        if not has_category:
            lines.append(in_category_line())
        if not has_package:
            lines.append(in_package_line())
    else:
        # Historical (pre-filter) trailing layout, extended with the now
        # unconditional in_category { name } between made_by and in_package.
        lines.append(made_by_line())
        lines.append(in_category_line())
        lines.append(in_package_line())

    lines.append(f"{indent}datasheet {{ url }}")
    if include_embedding:
        # Semantic path only (ADR-0020): a bare ``embedding`` selection so the
        # ranker reads the raw 384-float vector, computes cosine similarity and
        # discards it. It is a HARDCODED bare field, never a ``$``-variable, so
        # it adds no injection surface (the vector literal's per-element
        # _fmt_float validation is unchanged; AC-HY-16).
        lines.append(f"{indent}embedding")
    return "\n".join(lines)


def _cascade_clause(
    *, has_package: bool, has_manufacturer: bool, has_category: bool
) -> str:
    """Return the ``@cascade(...)`` clause covering the active edge filters.

    ``@cascade`` drops parts whose listed edge prunes to empty, so each active
    edge filter (package / manufacturer / category) acts as a real constraint
    without inlining its value. The cascade is scoped to exactly the filtered
    edges so unrelated optional edges (an unfiltered made_by / datasheet) never
    additionally prune otherwise-matching parts. Only ``in_package`` is present
    when just a package filter is active, keeping the no-extra-filter query
    byte-identical to the pre-filter output (ADR-0016).
    """
    names: list[str] = []
    if has_package:
        names.append("in_package")
    if has_manufacturer:
        names.append("made_by")
    if has_category:
        names.append("in_category")
    return f" @cascade({', '.join(names)})" if names else ""


def _build_block(  # noqa: PLR0913 — keyword-only block descriptor; cohesive unit
    *,
    name: str,
    text_term: str | None,
    param_terms: list[str],
    scalar_terms: list[str],
    has_package: bool,
    package_var: str,
    has_manufacturer: bool,
    manufacturer_var: str,
    has_category: bool,
    category_var: str,
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
        scalar_terms: Structured scalar filter terms (``ge(stock, N)`` /
            ``eq(is_basic, ...)`` / ``le(price_usd, ...)``); empty when none.
        has_package: Whether a package filter should be applied.
        package_var: The ``$``-variable holding the package name.
        has_manufacturer: Whether a manufacturer filter should be applied.
        manufacturer_var: The ``$``-variable holding the manufacturer term.
        has_category: Whether a category filter should be applied.
        category_var: The ``$``-variable holding the category term.
        first: The (already clamped) row cap for this block.
    """
    filter_terms: list[str] = []
    if text_term is not None:
        filter_terms.append(text_term)
    filter_terms.extend(param_terms)
    filter_terms.extend(scalar_terms)
    # A search hit is only useful when it is datasheet-backed: require at least
    # one datasheet edge so every surfaced row carries a datasheet URL.
    filter_terms.append("has(datasheet)")

    filter_clause = ""
    if filter_terms:
        filter_clause = " @filter(" + " AND ".join(filter_terms) + ")"

    cascade = _cascade_clause(
        has_package=has_package,
        has_manufacturer=has_manufacturer,
        has_category=has_category,
    )

    body = _render_fields(
        "    ",
        has_package=has_package,
        package_var=package_var,
        has_manufacturer=has_manufacturer,
        manufacturer_var=manufacturer_var,
        has_category=has_category,
        category_var=category_var,
    )
    return (
        f"  {name}(func: type(Part), first: {first}){filter_clause}{cascade} {{\n"
        f"{body}\n"
        f"  }}"
    )


def build_search_dql(  # noqa: PLR0913, PLR0915 — keyword-only structured filters
    parsed: ParsedQuery,
    *,
    limit: int = 20,
    manufacturer: str | None = None,
    package: str | None = None,
    category: str | None = None,
    min_stock: int | None = None,
    is_basic: bool | None = None,
    max_price: float | None = None,
) -> tuple[str, dict[str, str]]:
    """Build the multi-block search DQL and its variable map.

    Returns ``(query_text, variables)``. The query declares typed ``string``
    variables for every text token and (when present) the package / manufacturer
    / category filter terms; numeric bounds are inline float literals. The
    per-block ``first:`` cap is clamped to ``MAX_RESULT_LIMIT`` (ADR-0007).

    All structured-filter keyword arguments default to *off*, so a call with none
    of them is byte-identical to the pre-filter output (ADR-0016, backward
    compatible). Each active filter AND-composes into the SAME block ``@filter``/
    nested-edge/``@cascade`` clause (ADR-0016):

    Args:
        manufacturer: Manufacturer name -> ``made_by @filter(allofterms(name,
            $mfr))`` (case-insensitive recall; bound as ``$mfr``, never inlined).
        package: Package code -> ``in_package @filter(eq(name, $pkg))``. Shares
            ONE rendering path with the query-derived ``parsed.package`` (the two
            are byte-identical). The caller must upper-case before passing;
            re-validated against the package charset here.
        category: Category name -> ``in_category @filter(allofterms(name,
            $cat))`` (bound as ``$cat``, never inlined).
        min_stock: Minimum stock -> ``ge(stock, <int>)`` (integer literal).
        is_basic: Tri-state basic/extended flag -> ``eq(is_basic, true|false)``
            (fixed literal; ``None`` omits the term).
        max_price: Price ceiling (USD) -> ``le(price_usd, <float>)``.

    Raises:
        ValueError: If a package code fails the injection-guard regex, if a
            manufacturer/category term is empty/whitespace-only or over the length
            cap, or if ``min_stock`` is not a whole non-negative integer.
    """
    first = max(1, min(int(limit), MAX_RESULT_LIMIT))

    variables: dict[str, str] = {}
    var_decls: list[str] = []

    # Package: the NEW package= kwarg and the query-derived parsed.package share
    # ONE rendering path (AC-SF-4 requires byte-identical output). The explicit
    # kwarg wins when both are set; the CLI already rejects that collision, so in
    # practice at most one is ever present.
    effective_package = package if package is not None else parsed.package
    has_package = effective_package is not None
    package_var = "$pkg"
    if effective_package is not None:
        variables[package_var] = validate_package(effective_package)
        var_decls.append(f"{package_var}: string")

    has_manufacturer = manufacturer is not None
    manufacturer_var = "$mfr"
    if manufacturer is not None:
        variables[manufacturer_var] = _validate_filter_term(
            manufacturer, field="manufacturer"
        )
        var_decls.append(f"{manufacturer_var}: string")

    has_category = category is not None
    category_var = "$cat"
    if category is not None:
        variables[category_var] = _validate_filter_term(category, field="category")
        var_decls.append(f"{category_var}: string")

    param_terms = _param_filter_terms(parsed)
    scalar_terms = _scalar_filter_terms(
        min_stock=min_stock, is_basic=is_basic, max_price=max_price
    )

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

    def _mk_block(name: str, text_term: str | None) -> str:
        """Render one block, threading the shared filter descriptor for this call."""
        return _build_block(
            name=name,
            text_term=text_term,
            param_terms=param_terms,
            scalar_terms=scalar_terms,
            has_package=has_package,
            package_var=package_var,
            has_manufacturer=has_manufacturer,
            manufacturer_var=manufacturer_var,
            has_category=has_category,
            category_var=category_var,
            first=first,
        )

    blocks: list[str] = []
    if text_tokens:
        blocks.append(_mk_block("exact", exact_term))
        blocks.append(_mk_block("trig", trig_term))
        blocks.append(_mk_block("fts", fts_term))
    else:
        # No free text: a single parametric/package block under the "exact" name
        # so rank_results treats these rows as the top tier. No trig/fts blocks
        # are emitted (rank_results tolerates their absence); emitting an
        # ``eq(mpn_norm, "")`` placeholder would wrongly match parts with an
        # empty mpn_norm and pollute the results with a blank row.
        blocks.append(_mk_block("exact", None))

    header = ""
    if var_decls:
        header = "query search(" + ", ".join(var_decls) + ") "

    query_text = header + "{\n" + "\n".join(blocks) + "\n}"
    return query_text, variables


def build_semantic_dql(  # noqa: PLR0913 — keyword-only structured-filter kwargs
    vector: list[float],
    k: int,
    *,
    parsed: ParsedQuery | None = None,
    manufacturer: str | None = None,
    category: str | None = None,
    min_stock: int | None = None,
    is_basic: bool | None = None,
    max_price: float | None = None,
) -> tuple[str, dict[str, str]]:
    """Build the semantic (vector-similarity) search DQL and its variable map.

    Returns ``(query_text, variables)`` for a single ``semantic`` block rooted on
    ``similar_to(embedding, k, "[...]")``. The query selects the same render
    fields as :func:`build_search_dql` so the ranker/renderer treat semantic rows
    uniformly, PLUS a bare ``embedding`` field (the raw 384-float vector) so the
    ranker can compute cosine similarity client-side (ADR-0020).
    :func:`build_search_dql` never selects ``embedding``, so the lexical response
    stays byte-identical (the vector never travels the lexical path).

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

    DoS (ADR-0007 / ADR-0020): ``k`` is clamped to
    ``[1, SEMANTIC_CANDIDATE_CAP]`` (the CANDIDATE bound, 1500) so a single
    request can never ask Dgraph for an unbounded neighbour set. This is the
    candidate pool the CLI oversamples into; the smaller
    :data:`MAX_RESULT_LIMIT` (the RESULT bound, 200) is applied later by the
    ranker AFTER the client-side cosine re-rank — the builder deliberately does
    NOT use :data:`MAX_RESULT_LIMIT` for this clamp, and is the DoS backstop that
    never trusts the caller's ``k``.

    The structured-filter keyword arguments (``manufacturer``/``category``/
    ``min_stock``/``is_basic``/``max_price``) compose EXACTLY as they do for
    :func:`build_search_dql`, extending the SAME ``similar_to(...)`` block's
    ``@filter``/nested-edge/``@cascade`` clause — never a Python post-filter — so
    the lexical and semantic paths share one filter contract (ADR-0016). Package
    for the hybrid query is supplied via ``parsed.package`` (there is no separate
    ``package`` kwarg here).

    Args:
        vector: The query embedding; must be length :data:`EMBED_DIM` (384).
        k: Requested number of nearest neighbours (clamped to the DoS bound).
        parsed: Optional parsed query supplying hybrid package / parametric
            filters layered on top of the vector search.
        manufacturer: Manufacturer name -> ``made_by @filter(allofterms(name,
            $mfr))`` (bound as ``$mfr``, never inlined).
        category: Category name -> ``in_category @filter(allofterms(name,
            $cat))`` (bound as ``$cat``, never inlined).
        min_stock: Minimum stock -> ``ge(stock, <int>)`` (integer literal).
        is_basic: Tri-state basic/extended flag -> ``eq(is_basic, true|false)``
            (fixed literal; ``None`` omits the term).
        max_price: Price ceiling (USD) -> ``le(price_usd, <float>)``.

    Raises:
        ValueError: If *vector* is not length 384, if any element is not a finite
            float literal, if a package code fails the injection-guard regex, if a
            manufacturer/category term is empty/whitespace-only or over the length
            cap, or if ``min_stock`` is not a whole non-negative integer.
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

    # Clamp k into [1, SEMANTIC_CANDIDATE_CAP] (the CANDIDATE bound; ADR-0020).
    # The builder is the DoS backstop and never trusts the caller: even an
    # un-oversampled or hostile k can never ask Dgraph for an unbounded neighbour
    # set. MAX_RESULT_LIMIT stays the RESULT bound (applied later by the ranker),
    # NOT this candidate clamp.
    clamped_k = max(1, min(int(k), SEMANTIC_CANDIDATE_CAP))

    variables: dict[str, str] = {}
    var_decls: list[str] = []

    has_package = parsed is not None and parsed.package is not None
    package_var = "$pkg"
    if parsed is not None and parsed.package is not None:
        variables[package_var] = validate_package(parsed.package)
        var_decls.append(f"{package_var}: string")

    has_manufacturer = manufacturer is not None
    manufacturer_var = "$mfr"
    if manufacturer is not None:
        variables[manufacturer_var] = _validate_filter_term(
            manufacturer, field="manufacturer"
        )
        var_decls.append(f"{manufacturer_var}: string")

    has_category = category is not None
    category_var = "$cat"
    if category is not None:
        variables[category_var] = _validate_filter_term(category, field="category")
        var_decls.append(f"{category_var}: string")

    # Hybrid parametric filters (float literals — injection-safe, ADR-PARAM).
    param_terms = _param_filter_terms(parsed) if parsed is not None else []
    # Structured scalar filters compose exactly as in build_search_dql (ADR-0016).
    scalar_terms = _scalar_filter_terms(
        min_stock=min_stock, is_basic=is_basic, max_price=max_price
    )

    filter_terms: list[str] = list(param_terms)
    filter_terms.extend(scalar_terms)
    # A semantic hit is only useful when datasheet-backed (same as PR3 blocks).
    filter_terms.append("has(datasheet)")

    filter_clause = " @filter(" + " AND ".join(filter_terms) + ")"
    # Each active edge filter (package / manufacturer / category) is @cascade-d so
    # a part whose filtered edge prunes to empty is dropped — the filter acts as a
    # real constraint without inlining its value (mirrors the PR3 search blocks).
    cascade = _cascade_clause(
        has_package=has_package,
        has_manufacturer=has_manufacturer,
        has_category=has_category,
    )

    body = _render_fields(
        "    ",
        has_package=has_package,
        package_var=package_var,
        has_manufacturer=has_manufacturer,
        manufacturer_var=manufacturer_var,
        has_category=has_category,
        category_var=category_var,
        include_embedding=True,
    )

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
