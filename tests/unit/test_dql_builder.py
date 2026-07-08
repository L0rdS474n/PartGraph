"""
Tests: SEARCH-DQL-1..6 — partgraph.query.dql_builder

Specifies the behavior of build_search_dql() and build_show_dql() which produce
DQL query strings and variable dicts for Dgraph execution.

Module under test: partgraph.query.dql_builder
  - build_search_dql(parsed: ParsedQuery, *, limit: int = 20)
      -> (query_text: str, variables: dict[str, str])
  - build_show_dql(mpn_norm: str)
      -> (query_text: str, variables: dict[str, str])

Design decisions pinned by dispatcher:
  - ADR-PARAM brackets: resistance ±1%, capacitance/inductance/current_max/
    power ±5%, voltage_max/voltage_min ±2%, frequency_max ±1%,
    tolerance_pct EXACT (eq).
  - ADR-INJECT: numeric values = float literals (safe); text tokens bind via
    Dgraph $vars; package token validated ^[A-Z0-9][A-Z0-9\\-]{0,19}$ before use.
  - Multi-block shape: exact / trigram / fts named blocks each select uid, mpn,
    mpn_norm, datasheet{url}, made_by{name}, in_package{name}, stock, is_basic,
    plus promoted numeric predicates.
  - build_show_dql: eq(mpn_norm,$m) + made_by, in_category, in_package,
    datasheet{url source}, tagged, attr{attr_name attr_value attr_value_num};
    related-parts block via anyofterms(mpn_norm, <prefix>) (NOT variant_of).

NOTE: Collection will ERROR on import of partgraph.query.dql_builder because that
module does not exist yet. That is the correct red state before PR3 implementation.
"""

from __future__ import annotations

import re

import pytest

from partgraph.query.dql_builder import build_search_dql, build_show_dql  # noqa: F401
from partgraph.query.parser import ParsedQuery, Quantity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_parsed(
    *,
    quantities: list[Quantity] | None = None,
    package: str | None = None,
    text_tokens: list[str] | None = None,
    raw_query: str = "",
) -> ParsedQuery:
    """Build a ParsedQuery without going through the real parser."""
    return ParsedQuery(
        quantities=quantities or [],
        package=package,
        text_tokens=text_tokens or [],
        raw_query=raw_query,
    )


def _q(predicate: str, value: float, raw: str) -> Quantity:
    return Quantity(predicate=predicate, value=value, raw=raw)


# ---------------------------------------------------------------------------
# SEARCH-DQL-1: resistance=10000 -> bracket [9900, 10100] as float literals
# (ADR-PARAM: resistance ±1%)
# ---------------------------------------------------------------------------

def test_dql_builder_resistance_bracket_float_literals() -> None:
    """Given a ParsedQuery with resistance=10000.0.
    When build_search_dql is called.
    Then the query text contains ge() and le() bounds at [9900.0, 10100.0]
    expressed as float literals — not as $vars — satisfying ADR-INJECT and
    ADR-PARAM (resistance ±1%).
    """
    parsed = _make_parsed(quantities=[_q("resistance", 10000.0, "10k")])
    query_text, _variables = build_search_dql(parsed)

    # The bounds 9900 and 10100 must appear as numeric literals (not variable refs).
    # Accept integer or float form: 9900 / 9900.0 / 10100 / 10100.0
    assert re.search(r"\b9900\.?\d*\b", query_text), (
        f"Expected lower bound 9900 as literal in query. Got:\n{query_text}"
    )
    assert re.search(r"\b10100\.?\d*\b", query_text), (
        f"Expected upper bound 10100 as literal in query. Got:\n{query_text}"
    )


def test_dql_builder_resistance_bounds_not_in_variables() -> None:
    """Given a ParsedQuery with resistance=10000.0.
    When build_search_dql is called.
    Then the variables dict does NOT contain the bound values (they are literals,
    not $var references — ADR-INJECT: numeric values = float literals).
    """
    parsed = _make_parsed(quantities=[_q("resistance", 10000.0, "10k")])
    _query_text, variables = build_search_dql(parsed)

    for val in variables.values():
        assert "9900" not in val and "10100" not in val, (
            f"Resistance bounds must be literals, not $vars. Found in variables: {variables}"
        )


def test_dql_builder_resistance_uses_ge_le_filter() -> None:
    """Given a ParsedQuery with resistance=10000.0.
    When build_search_dql is called.
    Then the query text uses ge() and le() filter functions (range filter pattern).
    """
    parsed = _make_parsed(quantities=[_q("resistance", 10000.0, "10k")])
    query_text, _variables = build_search_dql(parsed)

    assert "ge(" in query_text, (
        f"Expected ge() filter for resistance lower bound. Got:\n{query_text}"
    )
    assert "le(" in query_text, (
        f"Expected le() filter for resistance upper bound. Got:\n{query_text}"
    )


# ---------------------------------------------------------------------------
# SEARCH-DQL-2: text token "MAX232" -> declared $-var; literal NOT in query text
# (ADR-INJECT: text tokens bind via Dgraph $vars)
# ---------------------------------------------------------------------------

def test_dql_builder_text_token_bound_as_var_not_inline() -> None:
    """Given a ParsedQuery with text_tokens=["MAX232"].
    When build_search_dql is called.
    Then:
      - The variables dict contains an entry whose value is exactly "MAX232".
      - The query text does NOT contain the literal string "MAX232" directly
        (it is referenced only via its $var name for injection safety).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, variables = build_search_dql(parsed)

    # variables must contain "MAX232" as a value
    token_vars = [k for k, v in variables.items() if v == "MAX232"]
    assert token_vars, (
        f"Expected 'MAX232' bound as a $var in variables. Got: {variables}"
    )

    # The literal "MAX232" must NOT appear raw in the query text
    assert "MAX232" not in query_text, (
        f"Literal 'MAX232' must not appear in query text (use $var). Got:\n{query_text}"
    )


def test_dql_builder_text_token_var_name_has_dollar_prefix() -> None:
    """Given a ParsedQuery with text_tokens=["MAX232"].
    When build_search_dql is called.
    Then the variable key for the text token starts with "$" (Dgraph convention).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    _query_text, variables = build_search_dql(parsed)

    token_vars = [k for k, v in variables.items() if v == "MAX232"]
    assert token_vars, f"Expected MAX232 bound as a $var. Got: {variables}"
    key = token_vars[0]
    assert key.startswith("$"), (
        f"Variable key must start with '$'. Got: {key!r}"
    )


def test_dql_builder_text_token_var_referenced_in_query() -> None:
    """Given a ParsedQuery with text_tokens=["MAX232"].
    When build_search_dql is called.
    Then the variable key (e.g. "$t0") appears in the query text,
    confirming it is actually used in the DQL.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, variables = build_search_dql(parsed)

    token_vars = [k for k, v in variables.items() if v == "MAX232"]
    assert token_vars, f"Expected MAX232 bound as a $var. Got: {variables}"
    var_name = token_vars[0]
    assert var_name in query_text, (
        f"Variable {var_name!r} must appear in query text. Got:\n{query_text}"
    )


# ---------------------------------------------------------------------------
# SEARCH-DQL-3: package "0402" bound as $var in in_package @filter(eq(name,$p))
# (ADR-INJECT: package token validated before use)
# ---------------------------------------------------------------------------

def test_dql_builder_package_bound_as_var_in_filter() -> None:
    """Given a ParsedQuery with package="0402".
    When build_search_dql is called.
    Then:
      - variables contains an entry whose value is "0402".
      - The query text contains in_package and eq( and the $var name referencing it.
    """
    parsed = _make_parsed(package="0402")
    query_text, variables = build_search_dql(parsed)

    pkg_vars = [k for k, v in variables.items() if v == "0402"]
    assert pkg_vars, (
        f"Expected '0402' bound as a $var in variables. Got: {variables}"
    )
    var_name = pkg_vars[0]
    assert "in_package" in query_text, (
        f"Expected 'in_package' predicate in query text. Got:\n{query_text}"
    )
    assert "eq(" in query_text, (
        f"Expected eq() filter for package. Got:\n{query_text}"
    )
    assert var_name in query_text, (
        f"Package $var {var_name!r} must appear in query text. Got:\n{query_text}"
    )


# ---------------------------------------------------------------------------
# SEARCH-DQL-4: invalid package "0402; drop" -> ValueError (ADR-INJECT injection guard)
# ---------------------------------------------------------------------------

def test_dql_builder_invalid_package_raises_value_error() -> None:
    """Given a ParsedQuery with package="0402; drop" (hostile injection payload).
    When build_search_dql is called.
    Then a ValueError is raised (ADR-INJECT: package token validated
    ^[A-Z0-9][A-Z0-9\\-]{0,19}$ before use; this token fails that regex).
    """
    parsed = _make_parsed(package="0402; drop")
    with pytest.raises(ValueError):
        build_search_dql(parsed)


def test_dql_builder_package_with_lowercase_raises_value_error() -> None:
    """Given a ParsedQuery with package="sot23" (lowercase, fails regex).
    When build_search_dql is called.
    Then a ValueError is raised (ADR-INJECT validation: ^[A-Z0-9][A-Z0-9\\-]{0,19}$).
    """
    parsed = _make_parsed(package="sot23")
    with pytest.raises(ValueError):
        build_search_dql(parsed)


def test_dql_builder_package_too_long_raises_value_error() -> None:
    """Given a ParsedQuery with a package name of 21 uppercase chars (over 20 limit).
    When build_search_dql is called.
    Then a ValueError is raised (ADR-INJECT: max 20 chars after first char).
    """
    parsed = _make_parsed(package="A" * 21)
    with pytest.raises(ValueError):
        build_search_dql(parsed)


# ---------------------------------------------------------------------------
# SEARCH-DQL-5: multi-block shape (exact / trigram / fts blocks)
# ---------------------------------------------------------------------------

def test_dql_builder_query_has_exact_block() -> None:
    """Given any non-empty ParsedQuery.
    When build_search_dql is called.
    Then the query text contains a named block for exact MPN matching.
    (The block name must contain "exact" or equivalent discriminator.)
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    # Accept "exact" as a block name or as an annotation substring.
    assert "exact" in query_text.lower(), (
        f"Expected an 'exact' named block in multi-block DQL. Got:\n{query_text}"
    )


def test_dql_builder_query_has_trigram_block() -> None:
    """Given any non-empty ParsedQuery.
    When build_search_dql is called.
    Then the query text contains a named block for trigram/anyofterms MPN search.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    assert "trig" in query_text.lower() or "anyofterms" in query_text, (
        f"Expected a trigram block (containing 'trig' or 'anyofterms') in DQL. "
        f"Got:\n{query_text}"
    )


def test_dql_builder_query_has_fulltext_block() -> None:
    """Given any non-empty ParsedQuery.
    When build_search_dql is called.
    Then the query text contains a named block for full-text search (fts or alloftext).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    assert "fts" in query_text.lower() or "alloftext" in query_text or "fullmatch" in query_text, (
        f"Expected an fts/fulltext block in DQL. Got:\n{query_text}"
    )


def test_dql_builder_query_selects_required_fields() -> None:
    """Given any non-empty ParsedQuery.
    When build_search_dql is called.
    Then each named block selects: uid, mpn, mpn_norm, datasheet{url},
    made_by{name}, in_package{name}, stock, is_basic.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    required_fields = ["uid", "mpn", "mpn_norm", "datasheet", "url", "made_by",
                       "in_package", "stock", "is_basic"]
    for field in required_fields:
        assert field in query_text, (
            f"Expected field '{field}' in search DQL. Got:\n{query_text}"
        )


# ---------------------------------------------------------------------------
# SEARCH-DQL-6: build_show_dql("MAX232") -> eq(mpn_norm,$m) + detail fields
#               + related-parts via anyofterms (NOT variant_of)
# ---------------------------------------------------------------------------

def test_dql_builder_show_dql_uses_eq_mpn_norm_var() -> None:
    """Given mpn_norm="MAX232".
    When build_show_dql is called.
    Then the query text uses eq(mpn_norm, $m) (or equivalent $-var) and variables
    contains the entry mapping that var to "MAX232".
    """
    query_text, variables = build_show_dql("MAX232")

    assert "mpn_norm" in query_text, (
        f"Expected 'mpn_norm' in show DQL. Got:\n{query_text}"
    )
    assert "eq(" in query_text, (
        f"Expected eq() filter in show DQL. Got:\n{query_text}"
    )

    # The literal "MAX232" must NOT appear raw in the query text (inject safety).
    assert "MAX232" not in query_text, (
        f"Literal 'MAX232' must not appear in show DQL text (use $var). Got:\n{query_text}"
    )

    # variables must map some key to "MAX232"
    assert "MAX232" in variables.values(), (
        f"variables must contain 'MAX232' as a $var value. Got: {variables}"
    )


def test_dql_builder_show_dql_selects_detail_fields() -> None:
    """Given mpn_norm="MAX232".
    When build_show_dql is called.
    Then the query text selects: made_by, in_category, in_package,
    datasheet{url source}, tagged, and attr{attr_name attr_value attr_value_num}.
    """
    query_text, _variables = build_show_dql("MAX232")

    required_fields = [
        "made_by", "in_category", "in_package",
        "datasheet", "url", "source",
        "tagged",
        "attr_name", "attr_value", "attr_value_num",
    ]
    for field in required_fields:
        assert field in query_text, (
            f"Expected field '{field}' in show DQL. Got:\n{query_text}"
        )


def test_dql_builder_show_dql_has_related_parts_via_anyofterms_not_variant_of() -> None:
    """Given mpn_norm="MAX232".
    When build_show_dql is called.
    Then the query text includes a related-parts block that uses anyofterms on
    mpn_norm (MPN trigram similarity), and does NOT use variant_of or family_name
    traversal (family_name/PartFamily are UNPOPULATED — dispatcher Q1 decision).
    """
    query_text, _variables = build_show_dql("MAX232")

    # Related parts must use anyofterms on mpn_norm.
    assert "anyofterms" in query_text, (
        f"Expected 'anyofterms' for related-parts block in show DQL. Got:\n{query_text}"
    )

    # Must NOT traverse variant_of or family_name (UNPOPULATED — Q1 decision).
    assert "variant_of" not in query_text, (
        f"show DQL must NOT use 'variant_of' (UNPOPULATED per Q1). Got:\n{query_text}"
    )
    assert "family_name" not in query_text, (
        f"show DQL must NOT use 'family_name' (UNPOPULATED per Q1). Got:\n{query_text}"
    )


def test_dql_builder_show_dql_variables_has_dollar_prefix_keys() -> None:
    """Given mpn_norm="MAX232".
    When build_show_dql is called.
    Then all keys in the returned variables dict start with "$" (Dgraph convention).
    """
    _query_text, variables = build_show_dql("MAX232")

    for key in variables:
        assert key.startswith("$"), (
            f"All variable keys must start with '$'. Got: {key!r}"
        )


# ---------------------------------------------------------------------------
# Tolerance_pct uses EXACT (eq) — not a range bracket
# (ADR-PARAM: tolerance_pct EXACT)
# ---------------------------------------------------------------------------

def test_dql_builder_tolerance_pct_uses_exact_eq_not_range() -> None:
    """Given a ParsedQuery with tolerance_pct=1.0.
    When build_search_dql is called.
    Then the query text uses eq() for tolerance_pct (not ge/le range bracket).
    (ADR-PARAM: tolerance_pct filter is EXACT.)
    """
    parsed = _make_parsed(quantities=[_q("tolerance_pct", 1.0, "1%")])
    query_text, _variables = build_search_dql(parsed)

    # eq() must appear for tolerance
    assert "tolerance_pct" in query_text, (
        f"Expected 'tolerance_pct' in query. Got:\n{query_text}"
    )
    # Range bracket check: the query should NOT apply ±5% or similar bracket to tolerance.
    # We verify by checking the tolerance filter region uses eq.
    # Simplest proxy: if tolerance_pct appears, the nearby filter must be eq, not a
    # ge/le pair bracketing the tolerance value.
    tol_idx = query_text.index("tolerance_pct")
    nearby = query_text[max(0, tol_idx - 60): tol_idx + 60]
    assert "eq(" in nearby, (
        f"tolerance_pct must be filtered with eq() (exact match). "
        f"Nearby context: {nearby!r}"
    )


# ---------------------------------------------------------------------------
# A2 — DoS bounds (SECURITY — Concern 4 FAIL)
# PIN: MAX_RESULT_LIMIT=200 (in dql_builder).
# ---------------------------------------------------------------------------

def test_dql_builder_limit_cap_enforced() -> None:
    """Given a ParsedQuery and an absurdly large limit=99999.
    When build_search_dql(parsed, limit=99999) is called.
    Then:
      - The query text does NOT contain "first: 99999" (or the literal 99999).
      - The effective cap present in the query text is <= 200
        (PIN: MAX_RESULT_LIMIT=200 — builds must clamp the caller-supplied limit).

    Security rationale: an unbounded first: clause in DQL would allow a single
    attacker request to stream the entire database. MAX_RESULT_LIMIT=200 closes
    this DoS vector by clamping at the builder layer, regardless of what the
    caller passes.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, limit=99999)

    # 1. The raw caller value must not appear in the query.
    assert "99999" not in query_text, (
        "build_search_dql(limit=99999) must NOT emit 'first: 99999'. "
        "PIN: MAX_RESULT_LIMIT=200 — the implementation must clamp the limit."
    )

    # 2. Extract the actual first: value(s) and assert each is <= 200.
    # Accept both "first: N" and "first:N" forms; capture the integer.
    first_values = re.findall(r"first\s*:\s*(\d+)", query_text)
    assert first_values, (
        f"Expected at least one 'first: N' clause in the query text. Got:\n{query_text}"
    )
    for raw_val in first_values:
        cap = int(raw_val)
        assert cap <= 200, (
            f"PIN MAX_RESULT_LIMIT=200: effective cap in query is {cap}, must be <= 200. "
            f"Query text:\n{query_text}"
        )


def test_dql_builder_float_format_locale_safe() -> None:
    """Given a ParsedQuery with resistance=10000.0 (produces 9900/10100 bounds).
    When build_search_dql is called.
    Then all numeric values in the query text match the pattern ^[0-9.eE+\\-]+$:
      - No comma decimal separator (locale-safe).
      - No space inside numeric literals.
      - No thousand-separator commas inside numbers.

    Security / correctness rationale: if the running locale uses "," as the
    decimal separator (e.g. de_DE), Python's default float-to-str could emit
    "9.900,0" — silently breaking the DQL syntax. The implementation must force
    locale-invariant formatting (e.g. f"{value:.6g}" not str(value) with locale).
    """
    parsed = _make_parsed(quantities=[_q("resistance", 10000.0, "10k")])
    query_text, _variables = build_search_dql(parsed)

    # Extract all candidate numeric literals: sequences of digits with optional
    # decimal/exponent parts. A comma-separator would break this pattern.
    numeric_tokens = re.findall(r"\b\d[\d.eE+\-]*\b", query_text)
    for token in numeric_tokens:
        assert re.fullmatch(r"[0-9.eE+\-]+", token), (
            f"Numeric token {token!r} in query text contains non-locale-safe chars "
            f"(expected only [0-9.eE+-]). Full query:\n{query_text}"
        )


# ===========================================================================
# AC-SD: build_semantic_dql — PR4 semantic search DQL builder
# ===========================================================================
#
# Imports the new function from dql_builder (will be red until implemented).
# The function signature is:
#   build_semantic_dql(vector: list[float], k: int, *, parsed: ParsedQuery | None = None)
#       -> (query_text: str, variables: dict[str, str])
#
# Pinned contracts:
#   - vector is embedded INLINE as a literal string (NOT as a $var).
#   - The literal is built via repr(float) validated by _FLOAT_LITERAL_RE.
#   - k is clamped to SEMANTIC_CANDIDATE_CAP=1500 and min 1 (UPDATED —
#     hybrid semantic search PR: was MAX_RESULT_LIMIT=200; see AC-HY-2).
#   - vector must be length 384; otherwise ValueError naming 384.
#   - hostile non-float elements -> ValueError (literal can't break out).
#   - variables dict has NO "vector" key (inline literal, not $var).
#   - selects: uid mpn mpn_norm stock is_basic promoted made_by{name}
#     in_package{name} datasheet{url} embedding (UPDATED — hybrid semantic
#     search PR adds the bare 'embedding' field so the ranker can compute
#     cosine similarity; see AC-HY-3).
#   - hybrid: parsed with quantities/package -> filter carried into similar_to block.
# ===========================================================================

try:
    from partgraph.query.dql_builder import build_semantic_dql  # noqa: F401
except ImportError:
    build_semantic_dql = None  # type: ignore[assignment] — expected red


_EMBED_DIM = 384


def _unit_vector(dim: int = _EMBED_DIM) -> list[float]:
    """Return a length-dim unit vector (deterministic)."""
    return [1.0 / dim] * dim


# ---------------------------------------------------------------------------
# AC-SD-1: inline literal + no $vector in variables
# ---------------------------------------------------------------------------

def test_ac_sd_1_similar_to_inline_literal_no_vector_var() -> None:
    """AC-SD-1: Given a 384-dim vector and k=10.
    When build_semantic_dql(vector, k=10) is called.
    Then:
    - The query text contains similar_to(embedding, 10, "[...]") as an inline literal.
    - The variables dict has NO "vector" key (vectors must NOT be $vars).
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    vector = _unit_vector()
    query_text, variables = build_semantic_dql(vector, 10)

    assert "similar_to" in query_text, (
        f"AC-SD-1: query must contain 'similar_to'. Got:\n{query_text}"
    )
    assert "embedding" in query_text, (
        f"AC-SD-1: similar_to must reference 'embedding' predicate. Got:\n{query_text}"
    )
    # The inline vector literal must appear between quotes in the query.
    assert '"[' in query_text or '"[' in query_text, (
        f"AC-SD-1: vector must appear as inline quoted literal. Got:\n{query_text}"
    )

    # No $vector variable in the variables dict.
    vector_vars = [k for k in variables if "vector" in k.lower()]
    assert not vector_vars, (
        f"AC-SD-1: variables dict must NOT contain a vector key. "
        f"Found: {vector_vars!r} in variables: {variables}"
    )


# ---------------------------------------------------------------------------
# AC-SD-2: hostile non-float element -> ValueError
# ---------------------------------------------------------------------------

def test_ac_sd_2_hostile_non_float_raises_value_error() -> None:
    """AC-SD-2: Given a vector containing a non-float hostile element.
    When build_semantic_dql is called.
    Then a ValueError is raised (literal validation prevents injection).
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    hostile_vector: list = [0.1] * (_EMBED_DIM - 1) + ['0.5", 1, "evil']
    with pytest.raises((ValueError, TypeError)):
        build_semantic_dql(hostile_vector, 10)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC-SD-3: k clamping
# ---------------------------------------------------------------------------

def test_ac_sd_3_k_clamped_to_max_result_limit() -> None:
    """AC-SD-3 (REWRITTEN — hybrid semantic search PR, AC-HY-2): Given
    k=99999 (above the NEW SEMANTIC_CANDIDATE_CAP=1500).
    When build_semantic_dql(vector, 99999) is called.
    Then the effective k in the query text is EXACTLY 1500 (not 99999, and
    not silently clamped further down to the old MAX_RESULT_LIMIT=200 —
    build_semantic_dql's internal clamp bound moved from MAX_RESULT_LIMIT to
    SEMANTIC_CANDIDATE_CAP so an oversampled candidate_k up to 1500 can pass
    straight through).

    CHANGED FROM PRE-HYBRID (documented, not silent): the pre-hybrid version
    of this test asserted k <= MAX_RESULT_LIMIT (200). MAX_RESULT_LIMIT
    stays the RESULT bound (unchanged, still 200) but is no longer the bound
    build_semantic_dql's internal k-clamp uses.
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    from partgraph.query.dql_builder import SEMANTIC_CANDIDATE_CAP
    vector = _unit_vector()
    query_text, _ = build_semantic_dql(vector, 99999)

    assert "99999" not in query_text, (
        "AC-SD-3: k=99999 must be clamped. '99999' must not appear in query."
    )
    # Extract k from similar_to(embedding, <k>, ...)
    k_matches = re.findall(r"similar_to\([^,]+,\s*(\d+)", query_text)
    assert k_matches, (
        f"AC-SD-3: expected a similar_to(embedding, k, ...) clause. "
        f"Got:\n{query_text}"
    )
    for k_val_str in k_matches:
        k_val = int(k_val_str)
        assert k_val <= SEMANTIC_CANDIDATE_CAP, (
            f"AC-SD-3: k in query ({k_val}) must be <= "
            f"SEMANTIC_CANDIDATE_CAP={SEMANTIC_CANDIDATE_CAP}."
        )
    # k=99999 must clamp EXACTLY to the new cap (not some smaller value, e.g.
    # not the OLD MAX_RESULT_LIMIT=200 — that would be the pre-hybrid bug).
    assert int(k_matches[0]) == SEMANTIC_CANDIDATE_CAP, (
        f"AC-SD-3: k=99999 must clamp EXACTLY to "
        f"SEMANTIC_CANDIDATE_CAP={SEMANTIC_CANDIDATE_CAP}. Got {k_matches[0]}."
    )


def test_ac_sd_3_k_zero_clamped_to_1() -> None:
    """AC-SD-3: Given k=0 (below minimum).
    When build_semantic_dql(vector, 0) is called.
    Then the effective k in the query text is >= 1 (never 0).
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    vector = _unit_vector()
    query_text, _ = build_semantic_dql(vector, 0)

    k_matches = re.findall(r"similar_to\([^,]+,\s*(\d+)", query_text)
    for k_val_str in k_matches:
        k_val = int(k_val_str)
        assert k_val >= 1, (
            f"AC-SD-3: k in query ({k_val}) must be >= 1 (never 0 or negative)."
        )


# ---------------------------------------------------------------------------
# AC-SD-4: wrong vector length -> ValueError naming 384
# ---------------------------------------------------------------------------

def test_ac_sd_4_wrong_vector_length_raises_value_error_naming_384() -> None:
    """AC-SD-4: Given a vector of length 10 (not 384).
    When build_semantic_dql(vector, 10) is called.
    Then a ValueError is raised whose message names 384.
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    short_vector = [0.1] * 10
    with pytest.raises(ValueError, match="384"):
        build_semantic_dql(short_vector, 10)


# ---------------------------------------------------------------------------
# AC-SD-5: selects same render fields as PR3
# ---------------------------------------------------------------------------

def test_ac_sd_5_selects_required_render_fields() -> None:
    """AC-SD-5: Given a 384-dim vector and k=5.
    When build_semantic_dql(vector, k=5) is called.
    Then the query text contains the same set of render fields as PR3 search:
    uid, mpn, mpn_norm, stock, is_basic, promoted predicates (at minimum
    voltage_max/resistance), made_by{name}, in_package{name}, datasheet{url}.
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    vector = _unit_vector()
    query_text, _ = build_semantic_dql(vector, 5)

    required_fields = [
        "uid", "mpn", "mpn_norm", "stock", "is_basic",
        "made_by", "name",
        "in_package",
        "datasheet", "url",
    ]
    for field_name in required_fields:
        assert field_name in query_text, (
            f"AC-SD-5: semantic query must select '{field_name}'. Got:\n{query_text}"
        )


# ---------------------------------------------------------------------------
# AC-SD-6: hybrid — parsed with quantities/package -> filter terms in query
# ---------------------------------------------------------------------------

def test_ac_sd_6_hybrid_parsed_with_package_carries_filter() -> None:
    """AC-SD-6: Given a 384-dim vector, k=5, and a parsed query with a package.
    When build_semantic_dql(vector, k=5, parsed=parsed_with_package) is called.
    Then the query text contains an in_package filter term.
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    vector = _unit_vector()
    parsed = _make_parsed(package="DIP16")
    query_text, _ = build_semantic_dql(vector, 5, parsed=parsed)

    assert "in_package" in query_text, (
        f"AC-SD-6: hybrid query with package must carry in_package filter. "
        f"Got:\n{query_text}"
    )


def test_ac_sd_6_hybrid_parsed_with_resistance_carries_parametric_filter() -> None:
    """AC-SD-6: Given a 384-dim vector, k=5, and a parsed query with resistance=10000.
    When build_semantic_dql(vector, k=5, parsed=parsed_with_resistance) is called.
    Then the query text contains ge/le filter terms for resistance.
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    vector = _unit_vector()
    parsed = _make_parsed(quantities=[_q("resistance", 10000.0, "10k")])
    query_text, _ = build_semantic_dql(vector, 5, parsed=parsed)

    assert "ge(" in query_text or "ge (" in query_text, (
        f"AC-SD-6: hybrid query with resistance must carry ge() filter. "
        f"Got:\n{query_text}"
    )
    assert "le(" in query_text or "le (" in query_text, (
        f"AC-SD-6: hybrid query with resistance must carry le() filter. "
        f"Got:\n{query_text}"
    )


def test_ac_sd_6_hybrid_has_datasheet_filter_present() -> None:
    """AC-SD-6: Given a hybrid query with any parsed input.
    When build_semantic_dql is called.
    Then the query text contains has(datasheet) (same as PR3 search block contract).
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    vector = _unit_vector()
    parsed = _make_parsed(text_tokens=["rs232"])
    query_text, _ = build_semantic_dql(vector, 5, parsed=parsed)

    assert "has(datasheet)" in query_text, (
        f"AC-SD-6: semantic query must contain has(datasheet) filter. "
        f"Got:\n{query_text}"
    )


# ===========================================================================
# AC-SF: issue #15 PR1 — structured search filters (build_search_dql /
# build_semantic_dql new KEYWORD ARGUMENTS)
#
# New kwargs under test (threaded from new `partgraph search` CLI flags; see
# tests/unit/test_cli_search.py for the CLI-level end-to-end tests, and
# tests/integration/test_gate_pr5.py for the read-only live gate):
#
#   build_search_dql(parsed, *, limit=20,
#                     manufacturer: str | None = None,   # AC-SF-1/2/3
#                     package: str | None = None,        # AC-SF-4 (NEW kwarg;
#                                                         # distinct from
#                                                         # parsed.package, but
#                                                         # renders identically)
#                     category: str | None = None,       # AC-SF-6/18
#                     min_stock: int | None = None,      # AC-SF-7/8
#                     is_basic: bool | None = None,      # AC-SF-11/12
#                     max_price: float | None = None)    # AC-SF-14
#       -> (query_text, variables)      # unchanged return shape
#
#   build_semantic_dql(vector, k, *, parsed=None,
#                       manufacturer: str | None = None)  # AC-SF-16
#       -> (query_text, variables)
#
# PIN (AC-SF-1): manufacturer's bound variable is named "$mfr" exactly (the
# task's own acceptance text names it). category's bound variable is named
# "$cat" exactly (AC-SF-6 names it). The package/min_stock/is_basic/max_price
# kwarg NAMES themselves are this test suite's own reasonable, first-pinned
# choice (the ACs describe the CLI flag names, not the builder kwarg names) —
# an implementer who prefers different kwarg names must update these tests
# accordingly and record that as a deliberate, documented deviation.
#
# NOTE: build_search_dql/build_semantic_dql ALREADY EXIST (imported at module
# level above without a try/except guard) — calling them with these NEW kwargs
# today raises TypeError("unexpected keyword argument") at the call site inside
# each test. That is the correct RED state: a per-test failure, not a
# collection-time ImportError for the whole file.
# ===========================================================================

def _cascade_predicates(query_text: str) -> list[str]:
    """Return the comma-separated predicate names inside the first @cascade(...).

    Used so cascade assertions are robust to whichever order/whitespace the
    implementation emits multiple cascaded predicate names in (e.g.
    "@cascade(in_package, made_by)" vs "@cascade(made_by, in_package)") —
    tests check SET MEMBERSHIP, never exact clause text.
    """
    match = re.search(r"@cascade\(([^)]*)\)", query_text)
    if not match:
        return []
    return [name.strip() for name in match.group(1).split(",") if name.strip()]


# ---------------------------------------------------------------------------
# AC-SF-1: manufacturer -> made_by @filter(allofterms(name,$mfr)) + cascade
# ---------------------------------------------------------------------------

def test_ac_sf_1_root_func_type_part_unchanged_with_manufacturer() -> None:
    """AC-SF-1: Given a ParsedQuery and manufacturer="Texas Instruments".
    When build_search_dql(parsed, manufacturer=...) is called.
    Then the root func: type(Part) selector is unchanged (regression guard —
    the manufacturer filter is an ADDED @filter/selection, not a new root).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, manufacturer="Texas Instruments")

    assert "func: type(Part)" in query_text, (
        f"AC-SF-1: root func: type(Part) must be unchanged. Got:\n{query_text}"
    )


def test_ac_sf_1_manufacturer_adds_made_by_allofterms_filter() -> None:
    """AC-SF-1: Given manufacturer="Texas Instruments".
    When build_search_dql is called.
    Then the selection carries
    "made_by @filter(allofterms(name, $mfr)) { name }" (the SAME made_by field
    that is always selected, now with an added @filter clause).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, manufacturer="Texas Instruments")

    assert re.search(
        r"made_by\s*@filter\(\s*allofterms\(\s*name\s*,\s*\$mfr\s*\)\s*\)\s*\{\s*name\s*\}",
        query_text,
    ), (
        f"AC-SF-1: expected 'made_by @filter(allofterms(name, $mfr)) {{ name }}'. "
        f"Got:\n{query_text}"
    )


def test_ac_sf_1_manufacturer_binds_mfr_variable() -> None:
    """AC-SF-1: Given manufacturer="Texas Instruments".
    When build_search_dql is called.
    Then variables["$mfr"] == "Texas Instruments" (exact variable name pinned
    by the acceptance criteria).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    _query_text, variables = build_search_dql(parsed, manufacturer="Texas Instruments")

    assert variables.get("$mfr") == "Texas Instruments", (
        f"AC-SF-1: expected variables['$mfr'] == 'Texas Instruments'. Got: {variables}"
    )


def test_ac_sf_1_manufacturer_string_never_in_query_text() -> None:
    """AC-SF-1: Given manufacturer="Texas Instruments".
    When build_search_dql is called.
    Then the literal manufacturer string never appears in the query TEXT (only
    as a $var value) — ADR-INJECT.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, manufacturer="Texas Instruments")

    assert "Texas Instruments" not in query_text, (
        f"AC-SF-1: manufacturer string must never be inlined in query text. "
        f"Got:\n{query_text}"
    )


def test_ac_sf_1_manufacturer_extends_cascade_to_include_made_by() -> None:
    """AC-SF-1: Given manufacturer="Texas Instruments" (no package).
    When build_search_dql is called.
    Then @cascade is extended to include "made_by" (so a part whose made_by
    filter prunes to empty is dropped, exactly as @cascade(in_package) already
    does for the package filter).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, manufacturer="Texas Instruments")

    cascade_names = _cascade_predicates(query_text)
    assert "made_by" in cascade_names, (
        f"AC-SF-1: expected @cascade to include 'made_by'. Found: {cascade_names}. "
        f"Query:\n{query_text}"
    )


# ---------------------------------------------------------------------------
# Gate 3 (Security MUST): manufacturer value validation.
#
# A NEW permissive validator (distinct from _PACKAGE_VALID_RE — manufacturer
# names legitimately contain spaces and exceed 20 chars) must reject empty /
# whitespace-only values and enforce a NAMED length cap (DoS defense-in-depth,
# ADR-0007-style bound). The exact cap constant is deliberately NOT pinned
# here (a later gate chooses it) — only that a 500-char value is REJECTED and
# a normal ~40-char value ("Texas Instruments") is ACCEPTED.
# ---------------------------------------------------------------------------

def test_ac_sf_1_manufacturer_empty_string_raises_value_error() -> None:
    """Gate-3 Security MUST: Given manufacturer="" (empty string).
    When build_search_dql is called.
    Then a ValueError is raised.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    with pytest.raises(ValueError):
        build_search_dql(parsed, manufacturer="")


def test_ac_sf_1_manufacturer_whitespace_only_raises_value_error() -> None:
    """Gate-3 Security MUST: Given manufacturer="   " (whitespace-only).
    When build_search_dql is called.
    Then a ValueError is raised.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    with pytest.raises(ValueError):
        build_search_dql(parsed, manufacturer="   ")


def test_ac_sf_1_manufacturer_oversized_raises_value_error() -> None:
    """Gate-3 Security MUST: Given a 500-char manufacturer value (DoS
    defense-in-depth — the exact cap constant is NOT pinned here, only that
    500 chars is rejected).
    When build_search_dql is called.
    Then a ValueError is raised.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    with pytest.raises(ValueError):
        build_search_dql(parsed, manufacturer="A" * 500)


def test_ac_sf_1_manufacturer_normal_length_value_is_accepted() -> None:
    """Gate-3 Security MUST: Given a normal manufacturer value
    ("Texas Instruments", ~17 chars).
    When build_search_dql is called.
    Then NO exception is raised, and the value is bound as $mfr (the new
    permissive validator must not reject legitimate manufacturer names).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, variables = build_search_dql(parsed, manufacturer="Texas Instruments")

    assert variables.get("$mfr") == "Texas Instruments", (
        f"Gate-3: a normal manufacturer value must be accepted, not rejected. "
        f"Got variables: {variables}"
    )
    assert "made_by" in query_text


# ---------------------------------------------------------------------------
# AC-SF-2: case-insensitive recall via allofterms (not eq)
# ---------------------------------------------------------------------------

def test_ac_sf_2_manufacturer_mixed_case_uses_allofterms_not_eq() -> None:
    """AC-SF-2: Given manufacturer="texas instruments" (any casing).
    When build_search_dql is called.
    Then the made_by filter clause uses allofterms (not eq) on the bound
    variable, so the SAME query would match any differently-cased manufacturer
    node in Dgraph (LIVE-CONFIRMED: a lowercase-bound $var matches "Texas
    Instruments" / "TEXAS INSTRUMENTS" / "texas instruments" nodes via
    allofterms).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, variables = build_search_dql(parsed, manufacturer="texas instruments")

    mb_idx = query_text.index("made_by")
    nearby = query_text[mb_idx : mb_idx + 120]
    assert "allofterms(" in nearby, (
        f"AC-SF-2: made_by filter must use allofterms for case-insensitive "
        f"recall. Nearby: {nearby!r}"
    )
    assert "eq(name" not in nearby, (
        f"AC-SF-2: made_by filter must NOT use eq() (would be case-sensitive, "
        f"exact-match only). Nearby: {nearby!r}"
    )
    assert variables.get("$mfr") == "texas instruments"


# ---------------------------------------------------------------------------
# AC-SF-3 / AC-SF-18: injection safety for manufacturer/category
# ---------------------------------------------------------------------------

def test_ac_sf_3_manufacturer_hostile_value_only_in_variables_not_query_text() -> None:
    """AC-SF-3: Given a hostile manufacturer value 'TI") OR eq(x,"'.
    When build_search_dql(parsed, manufacturer=hostile) is called.
    Then the hostile string appears ONLY as a $var value, never inlined in the
    query text, and the made_by filter uses allofterms — never regexp — on
    this user-controlled input.
    """
    hostile = 'TI") OR eq(x,"'
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, variables = build_search_dql(parsed, manufacturer=hostile)

    assert hostile in variables.values(), (
        f"AC-SF-3: expected the hostile manufacturer value bound as a $var. "
        f"Got: {variables}"
    )
    assert hostile not in query_text, (
        f"AC-SF-3: hostile manufacturer value must never be inlined. "
        f"Got:\n{query_text}"
    )

    mb_idx = query_text.index("made_by")
    nearby = query_text[mb_idx : mb_idx + 120]
    assert "regexp(" not in nearby, (
        f"AC-SF-3: made_by filter must never use regexp() on user input. "
        f"Nearby: {nearby!r}"
    )
    assert "allofterms(" in nearby


def test_ac_sf_18_category_hostile_value_only_in_variables_not_query_text() -> None:
    """AC-SF-18: Given a hostile category value 'RS232 ICs") OR eq(x,"'.
    When build_search_dql(parsed, category=hostile) is called.
    Then the hostile string appears ONLY as a $var value, never inlined, and
    the in_category filter uses allofterms — never regexp.
    """
    hostile = 'RS232 ICs") OR eq(x,"'
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, variables = build_search_dql(parsed, category=hostile)

    assert hostile in variables.values(), (
        f"AC-SF-18: expected the hostile category value bound as a $var. "
        f"Got: {variables}"
    )
    assert hostile not in query_text, (
        f"AC-SF-18: hostile category value must never be inlined. Got:\n{query_text}"
    )

    cat_idx = query_text.index("in_category")
    nearby = query_text[cat_idx : cat_idx + 120]
    assert "regexp(" not in nearby, (
        f"AC-SF-18: in_category filter must never use regexp() on user input. "
        f"Nearby: {nearby!r}"
    )
    assert "allofterms(" in nearby


def test_ac_sf_3_18_is_basic_is_fixed_literal_never_bound_as_variable() -> None:
    """AC-SF-3/18: Given is_basic=True.
    When build_search_dql(parsed, is_basic=True) is called.
    Then "true"/"false" never appear as a $var VALUE (is_basic is a fixed
    literal, not user-controlled input — nothing to bind).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    _query_text, variables = build_search_dql(parsed, is_basic=True)

    assert "true" not in variables.values(), (
        f"AC-SF-3/18: is_basic must be a fixed literal, never a $var value. "
        f"Got variables: {variables}"
    )


# ---------------------------------------------------------------------------
# AC-SF-4: package= kwarg identical to the existing query-derived-package path
# ---------------------------------------------------------------------------

def test_ac_sf_4_package_kwarg_identical_to_existing_parsed_package_path() -> None:
    """AC-SF-4: Given the SAME package value "SOIC-16" supplied either via
    parsed.package (query-derived, existing path) or via the NEW package=
    kwarg.
    When build_search_dql is called both ways (same text_tokens, no other
    kwargs).
    Then the two (query_text, variables) results are byte-identical — the new
    kwarg reuses the exact same in_package @filter(eq(name,$pkg)) { name } +
    @cascade(in_package) rendering as the existing path.
    """
    parsed_via_query = _make_parsed(text_tokens=["MAX232"], package="SOIC-16")
    query_text_a, variables_a = build_search_dql(parsed_via_query)

    parsed_via_flag = _make_parsed(text_tokens=["MAX232"])
    query_text_b, variables_b = build_search_dql(parsed_via_flag, package="SOIC-16")

    assert query_text_a == query_text_b, (
        f"AC-SF-4: --package kwarg must render IDENTICALLY to the existing "
        f"query-derived package path.\n--- query-derived ---\n{query_text_a}\n"
        f"--- package= kwarg ---\n{query_text_b}"
    )
    assert variables_a == variables_b, (
        f"AC-SF-4: variables must match between the two package paths. "
        f"query-derived={variables_a} package=kwarg={variables_b}"
    )


def test_ac_sf_4_package_kwarg_invalid_value_raises_value_error() -> None:
    """AC-SF-4 (defense in depth, mirrors test_dql_builder_invalid_package_raises_value_error):
    Given a hostile/invalid package value passed via the NEW package= kwarg
    (not parsed.package).
    When build_search_dql is called.
    Then a ValueError is raised — the same ADR-INJECT guard (^[A-Z0-9][A-Z0-9-]{0,19}$)
    applies to the new kwarg path.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    with pytest.raises(ValueError):
        build_search_dql(parsed, package="0402; drop")


def test_ac_sf_4_package_kwarg_lowercase_raises_value_error() -> None:
    """AC-SF-4: Given a LOWERCASE package value "soic-16" via the package=
    kwarg.
    When build_search_dql is called.
    Then a ValueError is raised.

    Design rationale (pinned by this test suite): the existing parsed.package
    path is always already-uppercase (parser.py's _try_package uppercases
    before returning), and _validate_package requires uppercase input. To
    "match the existing path exactly" (AC-SF-4), the new package= kwarg makes
    the SAME assumption: the CALLER (cli.py) is responsible for
    "soic-16" -> "SOIC-16" upper-casing BEFORE calling build_search_dql — the
    kwarg does not itself uppercase. The end-to-end uppercasing behavior for
    the actual `--package "soic-16"` CLI flag is verified separately at the
    CLI layer (see test_cli_search.py: test_ac_sf_4_package_flag_lowercase_uppercased_and_bound).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    with pytest.raises(ValueError):
        build_search_dql(parsed, package="soic-16")


# ---------------------------------------------------------------------------
# AC-SF-6: category -> in_category @filter(allofterms(name,$cat)) + cascade
# ---------------------------------------------------------------------------

def test_ac_sf_6_category_adds_in_category_allofterms_filter() -> None:
    """AC-SF-6: Given category="RS232 ICs".
    When build_search_dql is called.
    Then the selection carries
    "in_category @filter(allofterms(name, $cat)) { name }".
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, category="RS232 ICs")

    assert re.search(
        r"in_category\s*@filter\(\s*allofterms\(\s*name\s*,\s*\$cat\s*\)\s*\)\s*\{\s*name\s*\}",
        query_text,
    ), (
        f"AC-SF-6: expected 'in_category @filter(allofterms(name, $cat)) {{ name }}'. "
        f"Got:\n{query_text}"
    )


def test_ac_sf_6_category_binds_cat_variable() -> None:
    """AC-SF-6: Given category="RS232 ICs".
    When build_search_dql is called.
    Then variables["$cat"] == "RS232 ICs" (exact variable name pinned by the
    acceptance criteria).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    _query_text, variables = build_search_dql(parsed, category="RS232 ICs")

    assert variables.get("$cat") == "RS232 ICs", (
        f"AC-SF-6: expected variables['$cat'] == 'RS232 ICs'. Got: {variables}"
    )


def test_ac_sf_6_category_string_never_in_query_text() -> None:
    """AC-SF-6: Given category="RS232 ICs".
    When build_search_dql is called.
    Then the literal category string never appears in the query text.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, category="RS232 ICs")

    assert "RS232 ICs" not in query_text, (
        f"AC-SF-6: category string must never be inlined. Got:\n{query_text}"
    )


def test_ac_sf_6_category_extends_cascade_to_include_in_category() -> None:
    """AC-SF-6: Given category="RS232 ICs" (no package, no manufacturer).
    When build_search_dql is called.
    Then @cascade is extended to include "in_category".
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, category="RS232 ICs")

    cascade_names = _cascade_predicates(query_text)
    assert "in_category" in cascade_names, (
        f"AC-SF-6: expected @cascade to include 'in_category'. Found: {cascade_names}. "
        f"Query:\n{query_text}"
    )


# ---------------------------------------------------------------------------
# Gate 3 (Security MUST): category value validation (same permissive
# validator contract as manufacturer — see the AC-SF-1 validation group above).
# ---------------------------------------------------------------------------

def test_ac_sf_6_category_empty_string_raises_value_error() -> None:
    """Gate-3 Security MUST: Given category="" (empty string).
    When build_search_dql is called.
    Then a ValueError is raised.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    with pytest.raises(ValueError):
        build_search_dql(parsed, category="")


def test_ac_sf_6_category_whitespace_only_raises_value_error() -> None:
    """Gate-3 Security MUST: Given category="   " (whitespace-only).
    When build_search_dql is called.
    Then a ValueError is raised.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    with pytest.raises(ValueError):
        build_search_dql(parsed, category="   ")


def test_ac_sf_6_category_oversized_raises_value_error() -> None:
    """Gate-3 Security MUST: Given a 500-char category value (DoS
    defense-in-depth — the exact cap constant is NOT pinned here, only that
    500 chars is rejected).
    When build_search_dql is called.
    Then a ValueError is raised.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    with pytest.raises(ValueError):
        build_search_dql(parsed, category="A" * 500)


def test_ac_sf_6_category_normal_length_value_is_accepted() -> None:
    """Gate-3 Security MUST: Given a normal category value ("RS232 ICs").
    When build_search_dql is called.
    Then NO exception is raised, and the value is bound as $cat.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, variables = build_search_dql(parsed, category="RS232 ICs")

    assert variables.get("$cat") == "RS232 ICs", (
        f"Gate-3: a normal category value must be accepted, not rejected. "
        f"Got variables: {variables}"
    )
    assert "in_category" in query_text


# ---------------------------------------------------------------------------
# AC-SF-7 / AC-SF-8: stock -> ge(stock, N) INT literal (never float) + _fmt_int
# ---------------------------------------------------------------------------

def test_ac_sf_7_min_stock_1_emits_ge_stock_1_int_literal() -> None:
    """AC-SF-7: Given min_stock=1 (the --in-stock flag's translated value).
    When build_search_dql(parsed, min_stock=1) is called.
    Then the query text contains ge(stock, 1) as an INT literal.
    [LIVE-CONFIRMED: ge(stock,5.0) errors in Dgraph; ge(stock,5) works — the
    same applies to 1 — so a float-style "1.0" must never appear.]
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, min_stock=1)

    assert re.search(r"ge\(\s*stock\s*,\s*1\s*\)", query_text), (
        f"AC-SF-7: expected ge(stock, 1). Got:\n{query_text}"
    )
    assert not re.search(r"ge\(\s*stock\s*,\s*1\.0\s*\)", query_text), (
        f"AC-SF-7: stock literal must be an INT, never '1.0' (Dgraph errors on "
        f"a float stock literal — LIVE-CONFIRMED). Got:\n{query_text}"
    )


def test_ac_sf_8_min_stock_5_emits_ge_stock_5_int_literal() -> None:
    """AC-SF-8: Given min_stock=5 (the --min-stock 5 flag).
    When build_search_dql(parsed, min_stock=5) is called.
    Then the query text contains ge(stock, 5) as an INT literal, never
    ge(stock, 5.0).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, min_stock=5)

    assert re.search(r"ge\(\s*stock\s*,\s*5\s*\)", query_text), (
        f"AC-SF-8: expected ge(stock, 5). Got:\n{query_text}"
    )
    assert not re.search(r"ge\(\s*stock\s*,\s*5\.0\s*\)", query_text), (
        f"AC-SF-8: stock literal must be an INT, never '5.0'. Got:\n{query_text}"
    )


def test_ac_sf_8_fmt_int_formats_positive_integer() -> None:
    """AC-SF-8: Given the value 5.
    When the NEW _fmt_int(value) helper is called.
    Then it returns the string "5" (int literal, no trailing ".0").

    Local (function-scoped) import: _fmt_int does not exist yet, so importing
    it at MODULE level would raise a collection-time ImportError for the whole
    file. Importing it here defers the failure to test-execution time, so only
    this test (and its siblings below) fail — not the entire test module.
    """
    from partgraph.query.dql_builder import _fmt_int  # noqa: PLC0415 — RED until added

    assert _fmt_int(5) == "5", f"Expected _fmt_int(5) == '5'. Got: {_fmt_int(5)!r}"


def test_ac_sf_8_fmt_int_rejects_float_value() -> None:
    """AC-SF-8: Given the non-integer value 5.5.
    When _fmt_int(5.5) is called.
    Then it raises (ValueError or TypeError) — stock must never be a
    fractional literal.
    """
    from partgraph.query.dql_builder import _fmt_int  # noqa: PLC0415

    with pytest.raises((ValueError, TypeError)):
        _fmt_int(5.5)


def test_ac_sf_8_fmt_int_rejects_non_numeric_string() -> None:
    """AC-SF-8: Given the non-numeric string "foo".
    When _fmt_int("foo") is called.
    Then it raises (ValueError or TypeError).
    """
    from partgraph.query.dql_builder import _fmt_int  # noqa: PLC0415

    with pytest.raises((ValueError, TypeError)):
        _fmt_int("foo")  # type: ignore[arg-type]


def test_ac_sf_8_fmt_int_rejects_negative_value() -> None:
    """AC-SF-8: Given a negative value (-1).
    When _fmt_int(-1) is called.
    Then it raises (ValueError or TypeError) — stock can never be negative.
    """
    from partgraph.query.dql_builder import _fmt_int  # noqa: PLC0415

    with pytest.raises((ValueError, TypeError)):
        _fmt_int(-1)


# ---------------------------------------------------------------------------
# AC-SF-11 / AC-SF-12: is_basic -> eq(is_basic, true/false) fixed literal
# ---------------------------------------------------------------------------

def test_ac_sf_11_basic_true_emits_eq_is_basic_true_literal() -> None:
    """AC-SF-11: Given is_basic=True (the --basic flag).
    When build_search_dql(parsed, is_basic=True) is called.
    Then the query text contains eq(is_basic, true) as a fixed boolean literal
    (Dgraph DQL boolean-literal syntax is lowercase true/false).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, is_basic=True)

    assert re.search(r"eq\(\s*is_basic\s*,\s*true\s*\)", query_text), (
        f"AC-SF-11: expected eq(is_basic, true). Got:\n{query_text}"
    )


def test_ac_sf_12_extended_emits_eq_is_basic_false_literal() -> None:
    """AC-SF-12: Given is_basic=False (the --extended flag).
    When build_search_dql(parsed, is_basic=False) is called.
    Then the query text contains eq(is_basic, false) as a fixed boolean
    literal.

    Note: is_basic is a tri-state kwarg (None=no filter, True=--basic,
    False=--extended) so False here is unambiguous (not confused with "kwarg
    omitted").
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, is_basic=False)

    assert re.search(r"eq\(\s*is_basic\s*,\s*false\s*\)", query_text), (
        f"AC-SF-12: expected eq(is_basic, false). Got:\n{query_text}"
    )


def test_ac_sf_11_12_is_basic_none_default_omits_is_basic_filter() -> None:
    """AC-SF-11/12: Given is_basic is NOT passed (default).
    When build_search_dql(parsed) is called (no is_basic kwarg at all).
    Then the query text contains no eq(is_basic, ...) filter term (regression
    guard: the tri-state default must not silently filter results).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    assert not re.search(r"eq\(\s*is_basic\s*,", query_text), (
        f"AC-SF-11/12: no is_basic filter must be present by default. "
        f"Got:\n{query_text}"
    )


# ---------------------------------------------------------------------------
# AC-SF-14: max_price -> le(price_usd, <float>) via existing _fmt_float
# ---------------------------------------------------------------------------

def test_ac_sf_14_max_price_emits_le_price_usd_float_literal() -> None:
    """AC-SF-14: Given max_price=0.5 (the --max-price 0.5 flag).
    When build_search_dql(parsed, max_price=0.5) is called.
    Then the query text contains le(price_usd, 0.5) as a float literal (via
    the existing _fmt_float convention — locale-safe, injection-safe).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, variables = build_search_dql(parsed, max_price=0.5)

    assert re.search(r"le\(\s*price_usd\s*,\s*0\.5\s*\)", query_text), (
        f"AC-SF-14: expected le(price_usd, 0.5). Got:\n{query_text}"
    )
    assert "0.5" not in variables.values(), (
        f"AC-SF-14: max_price must be a literal, never bound as a $var. "
        f"Got: {variables}"
    )


# ---------------------------------------------------------------------------
# AC-SF-15: composition — MPN + parametric + package + manufacturer +
#           min_stock in ONE query, all AND-composed.
# ---------------------------------------------------------------------------

def test_ac_sf_15_composes_mpn_parametric_package_manufacturer_min_stock() -> None:
    """AC-SF-15: Given a ParsedQuery equivalent to "MAX232 0402 1%"
    (text_tokens=["MAX232"], package="0402", quantities=[tolerance_pct=1.0]),
    PLUS manufacturer="Texas Instruments" and min_stock=10.
    When build_search_dql is called ONCE.
    Then the single returned query carries: the MPN text terms, the
    tolerance_pct eq() parametric term, the in_package eq() filter, the
    made_by allofterms() filter, ge(stock, 10), and a @cascade clause naming
    BOTH in_package and made_by.
    """
    parsed = _make_parsed(
        quantities=[_q("tolerance_pct", 1.0, "1%")],
        package="0402",
        text_tokens=["MAX232"],
    )
    query_text, variables = build_search_dql(
        parsed, manufacturer="Texas Instruments", min_stock=10
    )

    # MPN terms (existing exact/trig/fts text-matching contract, unaffected).
    assert "mpn_norm" in query_text

    # Parametric eq() for tolerance_pct (ADR-PARAM: exact match).
    assert re.search(r"eq\(\s*tolerance_pct\s*,\s*1\.0?\s*\)", query_text), (
        f"AC-SF-15: expected eq(tolerance_pct, 1.0) parametric term. "
        f"Got:\n{query_text}"
    )

    # Package filter (query-derived path, still eq()).
    assert "in_package" in query_text
    pkg_idx = query_text.index("in_package")
    assert "eq(" in query_text[pkg_idx : pkg_idx + 80]

    # Manufacturer filter (new, allofterms()).
    assert "made_by" in query_text
    mb_idx = query_text.index("made_by")
    assert "allofterms(" in query_text[mb_idx : mb_idx + 120]

    # min_stock filter: INT literal, never float.
    assert re.search(r"ge\(\s*stock\s*,\s*10\s*\)", query_text), (
        f"AC-SF-15: expected ge(stock, 10). Got:\n{query_text}"
    )
    assert not re.search(r"ge\(\s*stock\s*,\s*10\.0\s*\)", query_text), (
        f"AC-SF-15: stock literal must be an INT, never '10.0'. Got:\n{query_text}"
    )

    # Cascade extended over BOTH in_package and made_by.
    cascade_names = _cascade_predicates(query_text)
    assert "in_package" in cascade_names and "made_by" in cascade_names, (
        f"AC-SF-15: expected @cascade to include BOTH in_package and made_by. "
        f"Found: {cascade_names}. Query:\n{query_text}"
    )

    assert variables.get("$mfr") == "Texas Instruments"
    assert variables.get("$pkg") == "0402"


# ---------------------------------------------------------------------------
# AC-SF-16: build_semantic_dql — manufacturer extends the similar_to(...) block
# ---------------------------------------------------------------------------

def test_ac_sf_16_semantic_dql_manufacturer_extends_similar_to_filter() -> None:
    """AC-SF-16: Given a 384-dim vector, k=5, and manufacturer="Texas Instruments".
    When build_semantic_dql(vector, k=5, manufacturer="Texas Instruments") is
    called.
    Then the SAME similar_to(...) block's selection carries the made_by
    allofterms filter (extending the existing block — NOT a separate Python
    post-filter step).
    """
    vector = _unit_vector()
    query_text, variables = build_semantic_dql(vector, 5, manufacturer="Texas Instruments")

    assert "similar_to" in query_text
    assert "made_by" in query_text
    mb_idx = query_text.index("made_by")
    nearby = query_text[mb_idx : mb_idx + 120]
    assert "allofterms(" in nearby, (
        f"AC-SF-16: made_by filter in the semantic block must use allofterms. "
        f"Nearby: {nearby!r}"
    )
    assert variables.get("$mfr") == "Texas Instruments"


def test_ac_sf_16_semantic_dql_manufacturer_string_never_in_query_text() -> None:
    """AC-SF-16: Given manufacturer="Texas Instruments" on build_semantic_dql.
    When called.
    Then the literal manufacturer string never appears in the query text
    (bound as $mfr only).
    """
    vector = _unit_vector()
    query_text, _variables = build_semantic_dql(vector, 5, manufacturer="Texas Instruments")

    assert "Texas Instruments" not in query_text, (
        f"AC-SF-16: manufacturer string must never be inlined. Got:\n{query_text}"
    )


def test_ac_sf_16_semantic_dql_manufacturer_extends_cascade() -> None:
    """AC-SF-16: Given manufacturer="Texas Instruments" on build_semantic_dql
    (no package).
    When called.
    Then @cascade is extended to include "made_by" (mirrors the hybrid
    package cascade already present for build_semantic_dql).
    """
    vector = _unit_vector()
    query_text, _variables = build_semantic_dql(vector, 5, manufacturer="Texas Instruments")

    cascade_names = _cascade_predicates(query_text)
    assert "made_by" in cascade_names, (
        f"AC-SF-16: expected @cascade to include 'made_by'. Found: {cascade_names}. "
        f"Query:\n{query_text}"
    )


# ---------------------------------------------------------------------------
# Gate 3 (Security MUST): manufacturer value validation on build_semantic_dql
# (same permissive-validator contract as build_search_dql).
# ---------------------------------------------------------------------------

def test_ac_sf_16_semantic_dql_manufacturer_empty_string_raises_value_error() -> None:
    """Gate-3 Security MUST: Given manufacturer="" on build_semantic_dql.
    When called.
    Then a ValueError is raised.
    """
    vector = _unit_vector()
    with pytest.raises(ValueError):
        build_semantic_dql(vector, 5, manufacturer="")


def test_ac_sf_16_semantic_dql_manufacturer_whitespace_only_raises_value_error() -> None:
    """Gate-3 Security MUST: Given manufacturer="   " on build_semantic_dql.
    When called.
    Then a ValueError is raised.
    """
    vector = _unit_vector()
    with pytest.raises(ValueError):
        build_semantic_dql(vector, 5, manufacturer="   ")


def test_ac_sf_16_semantic_dql_manufacturer_oversized_raises_value_error() -> None:
    """Gate-3 Security MUST: Given a 500-char manufacturer value on
    build_semantic_dql (DoS defense-in-depth — cap constant not pinned here).
    When called.
    Then a ValueError is raised.
    """
    vector = _unit_vector()
    with pytest.raises(ValueError):
        build_semantic_dql(vector, 5, manufacturer="A" * 500)


# ===========================================================================
# Gate 3 (scope decision): "let all filters compose with --semantic" — ALL of
# build_search_dql's new kwargs (not just manufacturer) must also exist on
# build_semantic_dql: category, min_stock, is_basic, max_price. Each extends
# the SAME similar_to(...) block's @filter/selection (not a Python
# post-filter), mirroring AC-SF-16's manufacturer contract exactly.
# ===========================================================================

def test_ac_sf_16_semantic_dql_category_extends_in_category_filter() -> None:
    """Gate-3: Given category="RS232 ICs" on build_semantic_dql.
    When build_semantic_dql(vector, k=5, category="RS232 ICs") is called.
    Then the SAME similar_to(...) block's selection carries
    in_category @filter(allofterms(name, $cat)) — extending the existing
    block, not a separate Python post-filter step.
    """
    vector = _unit_vector()
    query_text, variables = build_semantic_dql(vector, 5, category="RS232 ICs")

    assert "similar_to" in query_text
    assert "in_category" in query_text
    cat_idx = query_text.index("in_category")
    nearby = query_text[cat_idx : cat_idx + 120]
    assert "allofterms(" in nearby, (
        f"Gate-3: in_category filter in the semantic block must use "
        f"allofterms. Nearby: {nearby!r}"
    )
    assert variables.get("$cat") == "RS232 ICs"
    assert "RS232 ICs" not in query_text, (
        f"Gate-3: category string must never be inlined. Got:\n{query_text}"
    )


def test_ac_sf_16_semantic_dql_category_extends_cascade() -> None:
    """Gate-3: Given category="RS232 ICs" on build_semantic_dql (no package,
    no manufacturer).
    When called.
    Then @cascade is extended to include "in_category".
    """
    vector = _unit_vector()
    query_text, _variables = build_semantic_dql(vector, 5, category="RS232 ICs")

    cascade_names = _cascade_predicates(query_text)
    assert "in_category" in cascade_names, (
        f"Gate-3: expected @cascade to include 'in_category'. "
        f"Found: {cascade_names}. Query:\n{query_text}"
    )


def test_ac_sf_16_semantic_dql_min_stock_emits_ge_stock_int_literal() -> None:
    """Gate-3: Given min_stock=5 on build_semantic_dql.
    When build_semantic_dql(vector, k=5, min_stock=5) is called.
    Then the query text contains ge(stock, 5) as an INT literal (never
    ge(stock, 5.0) — same LIVE-CONFIRMED contract as build_search_dql).
    """
    vector = _unit_vector()
    query_text, _variables = build_semantic_dql(vector, 5, min_stock=5)

    assert "similar_to" in query_text
    assert re.search(r"ge\(\s*stock\s*,\s*5\s*\)", query_text), (
        f"Gate-3: expected ge(stock, 5) inside the semantic query. "
        f"Got:\n{query_text}"
    )
    assert not re.search(r"ge\(\s*stock\s*,\s*5\.0\s*\)", query_text), (
        f"Gate-3: stock literal must be an INT, never '5.0'. Got:\n{query_text}"
    )


def test_ac_sf_16_semantic_dql_is_basic_emits_eq_is_basic_true() -> None:
    """Gate-3: Given is_basic=True on build_semantic_dql.
    When build_semantic_dql(vector, k=5, is_basic=True) is called.
    Then the query text contains eq(is_basic, true) as a fixed boolean
    literal.
    """
    vector = _unit_vector()
    query_text, _variables = build_semantic_dql(vector, 5, is_basic=True)

    assert "similar_to" in query_text
    assert re.search(r"eq\(\s*is_basic\s*,\s*true\s*\)", query_text), (
        f"Gate-3: expected eq(is_basic, true) inside the semantic query. "
        f"Got:\n{query_text}"
    )


def test_ac_sf_16_semantic_dql_max_price_emits_le_price_usd_float() -> None:
    """Gate-3: Given max_price=0.5 on build_semantic_dql.
    When build_semantic_dql(vector, k=5, max_price=0.5) is called.
    Then the query text contains le(price_usd, 0.5) as a float literal.
    """
    vector = _unit_vector()
    query_text, variables = build_semantic_dql(vector, 5, max_price=0.5)

    assert "similar_to" in query_text
    assert re.search(r"le\(\s*price_usd\s*,\s*0\.5\s*\)", query_text), (
        f"Gate-3: expected le(price_usd, 0.5) inside the semantic query. "
        f"Got:\n{query_text}"
    )
    assert "0.5" not in variables.values(), (
        f"Gate-3: max_price must be a literal, never bound as a $var. "
        f"Got: {variables}"
    )


# ===========================================================================
# Gate 3 (Architecture SHOULD-1): no-filter default -> no @filter guards.
#
# manufacturer/category are OPT-IN filters: when omitted, made_by must carry
# NO @filter (plain "made_by { name }" as today) and in_category must be
# CONDITIONALLY selected — absent entirely from the query when --category is
# not used (mirrors in_package's existing conditional-selection pattern), so
# a no-filter query stays byte-identical to the pre-PR1 output.
# ===========================================================================

def test_ac_sf_1_manufacturer_omitted_made_by_has_no_filter() -> None:
    """Gate-3 Architecture SHOULD-1: Given manufacturer is NOT passed (default).
    When build_search_dql(parsed) is called (no manufacturer kwarg at all).
    Then the query text still selects plain "made_by { name }" (unconditional
    field, as today) with NO "made_by @filter(...)" attached.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    assert "made_by { name }" in query_text, (
        f"Gate-3: made_by must still be selected unconditionally when no "
        f"manufacturer filter is active. Got:\n{query_text}"
    )
    assert not re.search(r"made_by\s*@filter\(", query_text), (
        f"Gate-3: made_by must carry NO @filter when manufacturer is omitted "
        f"(regression guard against always-on filtering). Got:\n{query_text}"
    )


def test_ac_sf_35_category_omitted_in_category_present_without_filter() -> None:
    """AC-SF-35 (UPDATED — INVERTED from the pre-PR2 test named
    test_ac_sf_6_category_omitted_in_category_absent_entirely): Given category
    is NOT passed (default).
    When build_search_dql(parsed) is called (no category kwarg at all).
    Then `in_category { name }` IS present — AC-SF-33 makes in_category an
    UNCONDITIONAL selection — with NO `in_category @filter(` clause attached.

    CHANGED FROM PRE-PR2 (documented, not silent): the original version of
    this test asserted the OPPOSITE — that "in_category" was absent entirely
    from the query text when --category was omitted (the PR1
    conditional-selection contract, mirroring in_package's conditional
    pattern). PR2 makes in_category UNCONDITIONAL (AC-SF-33), so the correct
    post-PR2 contract is inverted: present, but filter-free by default.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    assert "in_category { name }" in query_text, (
        f"AC-SF-35: in_category must be present (unconditional selection, "
        f"AC-SF-33) even when --category is omitted. Got:\n{query_text}"
    )
    assert not re.search(r"in_category\s*@filter\(", query_text), (
        f"AC-SF-35: in_category must carry NO @filter when --category is "
        f"not used. Got:\n{query_text}"
    )


def test_ac_sf_34_no_filter_default_output_structural_not_byte_golden() -> None:
    """AC-SF-34 (UPDATED — REWRITTEN from the pre-PR2 test named
    test_ac_sf_no_filter_default_output_byte_identical_to_pre_pr1): Given a
    ParsedQuery with text_tokens=["MAX232"], package="0402" (query-derived),
    raw_query="MAX232 0402" — and NO new PR1/PR2 kwargs at all.
    When build_search_dql(parsed) is called.
    Then the output satisfies STRUCTURAL invariants (not a byte-exact
    golden):
      - `price_usd` is selected as a bare field, positioned AFTER `is_basic`.
      - `in_category { name }` is present with NO `@filter(` attached
        (AC-SF-33/35).
      - Exactly one `datasheet { url }` per block (3 blocks — unchanged
        datasheet shape).
      - The package/text-token variable contract is unchanged.

    CHANGED FROM PRE-PR2 (documented, not silent): the original version of
    this test pinned a BYTE-IDENTICAL golden query string captured before
    PR1. PR2 makes `price_usd` and `in_category` UNCONDITIONAL selections
    (AC-SF-33), so that golden string is now stale by construction — its
    EXACT new bytes are not knowable before PR2 is implemented (the whole
    point of a RED-first test). This test is rewritten to assert the
    STRUCTURAL shape PR2 must produce instead of guessing a new byte-golden.
    Gate 4 may reintroduce an exact byte-golden once PR2's implementation
    settles the final formatting.

    REMOVED (Gate 4, evidence-based, documented not silent): a cross-edge
    "made_by -> in_category -> in_package" ORDER sub-assertion used to live
    here (AC-SF-37 follow-up (b)). It relied on unifying the selection layout
    by dropping `lead_with_constraints`, which Gate 4 correctly did NOT do —
    that follow-up was explicitly OPTIONAL ("unless a test pins the layout
    load-bearing"), and the merged AC-SF-15 test DOES pin the opposite
    (in_package must LEAD the body when package+manufacturer are active), so
    the guard condition is met and the reorder is correctly skipped. The
    assertion was also measurement-confounded: naive `.index("in_package")`
    on the whole query matches the earlier `@cascade(in_package)` block-header
    occurrence, not the selection-body line. See ADR-0017. The remaining
    assertions below (price_usd/in_category/datasheet-count/variables) still
    pin PR2's REAL, implemented selection changes.
    """
    parsed = _make_parsed(
        text_tokens=["MAX232"], package="0402", raw_query="MAX232 0402"
    )
    query_text, variables = build_search_dql(parsed)

    # price_usd is a bare selected field, positioned after is_basic.
    assert re.search(r"^\s*price_usd\s*$", query_text, re.MULTILINE), (
        f"AC-SF-34: expected a bare 'price_usd' selected field. Got:\n{query_text}"
    )
    idx_is_basic = query_text.index("is_basic")
    idx_price_usd = query_text.index("price_usd")
    assert idx_price_usd > idx_is_basic, (
        f"AC-SF-34: 'price_usd' must be present AFTER 'is_basic' in the "
        f"selection. is_basic@{idx_is_basic}, price_usd@{idx_price_usd}. "
        f"Got:\n{query_text}"
    )

    # in_category is present, unconditional, with NO @filter attached.
    assert "in_category { name }" in query_text, (
        f"AC-SF-34: expected unconditional 'in_category {{ name }}'. "
        f"Got:\n{query_text}"
    )
    assert not re.search(r"in_category\s*@filter\(", query_text), (
        f"AC-SF-34: in_category must carry NO @filter with no --category "
        f"flag. Got:\n{query_text}"
    )

    # NOTE: the former "edge order: made_by -> in_category -> in_package"
    # cross-edge assertion was REMOVED here (Gate 4, evidence-based) — see
    # the docstring above and ADR-0017; it pinned an optional reorder
    # (AC-SF-37 follow-up (b)) that Gate 4 correctly did NOT implement
    # because the merged AC-SF-15 test requires the opposite layout.

    # Exactly one `datasheet { url }` per block (3 blocks: exact/trig/fts).
    datasheet_count = query_text.count("datasheet { url }")
    assert datasheet_count == 3, (
        f"AC-SF-34: expected exactly one 'datasheet {{ url }}' per block "
        f"(3 blocks total). Got {datasheet_count} occurrences in:\n{query_text}"
    )

    # The package/text-token variable contract is unchanged (still $pkg/$te/$rx/$ft).
    assert variables == {
        "$pkg": "0402",
        "$te": "MAX232",
        "$rx": "/MAX232/",
        "$ft": "MAX232",
    }, f"AC-SF-34: variables dict must be unchanged. Got: {variables}"


# ===========================================================================
# AC-SF-33: issue #15 PR2 — price_usd bare field + unconditional in_category
#
# build_search_dql / build_semantic_dql selections must ALWAYS include a bare
# `price_usd` field and a plain `in_category { name }` (no @filter) even with
# NO category filter active; with --category the existing allofterms filter
# (AC-SF-6) is unchanged.
#
# NOTE: build_search_dql/build_semantic_dql already accept these calls with NO
# new kwargs — these tests RED via a plain ASSERTION failure against current
# output (not an import/kwarg TypeError), because the missing selection is a
# query-SHAPE gap, not a missing parameter. Still the correct RED state (not
# a collection error).
# ===========================================================================

def test_ac_sf_33_build_search_dql_selects_bare_price_usd_field() -> None:
    """AC-SF-33: Given a plain ParsedQuery with no structured filters.
    When build_search_dql(parsed) is called.
    Then the selection set contains a BARE `price_usd` field (its own line,
    not merely present as part of a `le(price_usd, ...)` filter clause).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    assert re.search(r"^\s*price_usd\s*$", query_text, re.MULTILINE), (
        f"AC-SF-33: expected a bare 'price_usd' selected field. Got:\n{query_text}"
    )


def test_ac_sf_33_build_search_dql_selects_plain_in_category_no_filter_by_default() -> None:
    """AC-SF-33: Given NO --category filter.
    When build_search_dql(parsed) is called.
    Then the selection contains plain `in_category { name }` with NO
    `in_category @filter(` clause (in_category becomes UNCONDITIONAL, unlike
    the pre-PR2 conditional-selection behavior pinned by the old AC-SF-6
    test).
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    assert "in_category { name }" in query_text, (
        f"AC-SF-33: expected unconditional 'in_category {{ name }}'. Got:\n{query_text}"
    )
    assert not re.search(r"in_category\s*@filter\(", query_text), (
        f"AC-SF-33: in_category must carry NO @filter when --category is not "
        f"used. Got:\n{query_text}"
    )


def test_ac_sf_33_build_search_dql_with_category_flag_keeps_allofterms_filter() -> None:
    """AC-SF-33 (unchanged — PASSES TODAY already, a regression guard): Given
    category="RS232 ICs".
    When build_search_dql(parsed, category=...) is called.
    Then in_category still carries the AC-SF-6 allofterms filter, unchanged:
    'in_category @filter(allofterms(name, $cat)) { name }'.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, category="RS232 ICs")

    assert "in_category @filter(allofterms(name, $cat)) { name }" in query_text, (
        f"AC-SF-33: --category filter clause must be unchanged. Got:\n{query_text}"
    )


def test_ac_sf_33_build_semantic_dql_selects_bare_price_usd_field() -> None:
    """AC-SF-33: Given a 384-dim vector and k=5, no category filter.
    When build_semantic_dql(vector, k=5) is called.
    Then the selection contains a bare `price_usd` field.
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    vector = _unit_vector()
    query_text, _variables = build_semantic_dql(vector, 5)

    assert re.search(r"^\s*price_usd\s*$", query_text, re.MULTILINE), (
        f"AC-SF-33: semantic query must select a bare 'price_usd' field. "
        f"Got:\n{query_text}"
    )


def test_ac_sf_33_build_semantic_dql_selects_plain_in_category_no_filter_by_default() -> None:
    """AC-SF-33: Given a 384-dim vector, k=5, no category filter.
    When build_semantic_dql(vector, k=5) is called.
    Then the selection contains plain `in_category { name }` with NO
    `in_category @filter(` clause.
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    vector = _unit_vector()
    query_text, _variables = build_semantic_dql(vector, 5)

    assert "in_category { name }" in query_text, (
        f"AC-SF-33: expected unconditional 'in_category {{ name }}' in the "
        f"semantic query. Got:\n{query_text}"
    )
    assert not re.search(r"in_category\s*@filter\(", query_text), (
        f"AC-SF-33: semantic in_category must carry NO @filter by default. "
        f"Got:\n{query_text}"
    )


def test_ac_sf_33_build_semantic_dql_with_category_flag_keeps_allofterms_filter() -> None:
    """AC-SF-33 (unchanged — PASSES TODAY already, a regression guard): Given
    a 384-dim vector, k=5, and category="RS232 ICs".
    When build_semantic_dql(vector, k=5, category=...) is called.
    Then in_category still carries the AC-SF-16 allofterms filter, unchanged.
    """
    if build_semantic_dql is None:
        pytest.skip("build_semantic_dql not yet implemented (expected red)")

    vector = _unit_vector()
    query_text, _variables = build_semantic_dql(vector, 5, category="RS232 ICs")

    assert "in_category @filter(allofterms(name, $cat)) { name }" in query_text, (
        f"AC-SF-33: semantic --category filter clause must be unchanged. "
        f"Got:\n{query_text}"
    )


# ===========================================================================
# AC-SF-36: issue #15 PR2 — public promotion of validate_package /
# MAX_FILTER_TERM_LEN (dropping the leading underscore so cli.py's two lazy
# imports of the PRIVATE names can become PUBLIC imports).
#
# NOTE: `validate_package` / `MAX_FILTER_TERM_LEN` do not exist yet (only the
# PRIVATE `_validate_package` / `_MAX_FILTER_TERM_LEN` do today). Every import
# below is LOCAL to its test function (never at module level) so a missing
# name only fails THAT test at call time (ImportError) rather than erroring
# collection of the whole file.
# ===========================================================================

def test_ac_sf_36_validate_package_public_name_importable() -> None:
    """AC-SF-36: Given the dql_builder module.
    When `from partgraph.query.dql_builder import validate_package` is
    attempted.
    Then the import succeeds (the PUBLIC name exists) and behaves exactly
    like the private predecessor.

    RED today via ImportError (only the private `_validate_package` exists
    pre-PR2).
    """
    from partgraph.query.dql_builder import validate_package  # noqa: PLC0415

    assert validate_package("SOIC-16") == "SOIC-16", (
        "AC-SF-36: validate_package('SOIC-16') must return 'SOIC-16' unchanged."
    )


def test_ac_sf_36_validate_package_rejects_bad_charset() -> None:
    """AC-SF-36: Given a package value with a disallowed character (space).
    When validate_package(...) is called.
    Then a ValueError is raised (same contract as the private predecessor).
    """
    from partgraph.query.dql_builder import validate_package  # noqa: PLC0415

    with pytest.raises(ValueError):
        validate_package("RS232 ICs")


def test_ac_sf_36_max_filter_term_len_public_name_importable() -> None:
    """AC-SF-36: Given the dql_builder module.
    When `from partgraph.query.dql_builder import MAX_FILTER_TERM_LEN` is
    attempted.
    Then the import succeeds and the value equals the pre-PR2 private
    constant's value (128).

    RED today via ImportError.
    """
    from partgraph.query.dql_builder import MAX_FILTER_TERM_LEN  # noqa: PLC0415

    assert MAX_FILTER_TERM_LEN == 128, (
        f"AC-SF-36: MAX_FILTER_TERM_LEN must be 128 (unchanged value). "
        f"Got: {MAX_FILTER_TERM_LEN!r}"
    )


def test_ac_sf_36_validate_package_and_max_filter_term_len_in_dunder_all() -> None:
    """AC-SF-36: Given the dql_builder module's __all__ list.
    When inspected.
    Then both 'validate_package' and 'MAX_FILTER_TERM_LEN' are present in
    __all__ (the public export contract).
    """
    import partgraph.query.dql_builder as dql_builder_mod

    assert "validate_package" in dql_builder_mod.__all__, (
        f"AC-SF-36: 'validate_package' must be in __all__. "
        f"Got: {dql_builder_mod.__all__}"
    )
    assert "MAX_FILTER_TERM_LEN" in dql_builder_mod.__all__, (
        f"AC-SF-36: 'MAX_FILTER_TERM_LEN' must be in __all__. "
        f"Got: {dql_builder_mod.__all__}"
    )


def test_ac_sf_36_cli_imports_public_names_not_underscore_privates() -> None:
    """AC-SF-36: Given the cli.py source text.
    When grepped for the two lazy import statements that currently read the
    PRIVATE names (`from partgraph.query.dql_builder import
    _MAX_FILTER_TERM_LEN` and `from partgraph.query.dql_builder import
    _validate_package`).
    Then NEITHER underscore-prefixed import remains in cli.py — both call
    sites must import the PUBLIC `MAX_FILTER_TERM_LEN` / `validate_package`
    names instead.

    RED today: cli.py (production code, frozen for this PR) still imports
    the two private names — this test fails until cli.py's imports are
    updated by the implementation phase.
    """
    import pathlib

    import partgraph.cli as cli_mod

    cli_source = pathlib.Path(cli_mod.__file__).read_text(encoding="utf-8")

    assert "import _validate_package" not in cli_source, (
        "AC-SF-36: cli.py must no longer import the private '_validate_package'."
    )
    assert "import _MAX_FILTER_TERM_LEN" not in cli_source, (
        "AC-SF-36: cli.py must no longer import the private '_MAX_FILTER_TERM_LEN'."
    )
    assert "import validate_package" in cli_source, (
        "AC-SF-36: cli.py must import the PUBLIC 'validate_package'."
    )
    assert "import MAX_FILTER_TERM_LEN" in cli_source, (
        "AC-SF-36: cli.py must import the PUBLIC 'MAX_FILTER_TERM_LEN'."
    )


# ===========================================================================
# AC-SF-37: issue #15 PR2 — single fixed edge layout (made_by -> in_category
# -> in_package), replacing the PR1 "lead_with_constraints" reorder.
#
# Regardless of which of manufacturer/package/category filters are active,
# the relative ORDER of the three edges in the selection must always be
# made_by, then in_category, then in_package. These tests assert against the
# CURRENT build_search_dql (no new kwargs) — RED today via assertion
# failure, since the present _render_fields still reorders constrained edges
# to the front when a manufacturer/category filter is active.
# ===========================================================================

def _edge_positions(query_text: str) -> dict[str, int]:
    """Return {edge_name: first character index} for made_by/in_category/in_package.

    Uses str.find (never str.index) so a NOT-YET-PRESENT edge (e.g.
    in_category before AC-SF-33 is implemented) yields -1 rather than
    raising KeyError — callers get a clear, readable assertion failure
    showing the -1 instead of an opaque KeyError.
    """
    return {
        edge: query_text.find(edge)
        for edge in ("made_by", "in_category", "in_package")
    }


def test_ac_sf_37_edge_order_made_by_in_category_in_package_no_filters() -> None:
    """AC-SF-37: Given NO manufacturer/package/category filters.
    When build_search_dql(parsed) is called.
    Then the edges appear in the fixed order made_by, in_category, in_package.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed)

    positions = _edge_positions(query_text)
    assert positions["made_by"] < positions["in_category"] < positions["in_package"], (
        f"AC-SF-37: expected fixed edge order made_by -> in_category -> "
        f"in_package. Positions: {positions}"
    )


def test_ac_sf_37_edge_order_unchanged_with_manufacturer_filter_active() -> None:
    """AC-SF-37: Given --manufacturer active (a PR1 'constrained' edge).
    When build_search_dql(parsed, manufacturer=...) is called.
    Then the edge order is STILL made_by, in_category, in_package — the PR1
    lead-with-constraints reorder must be gone.
    """
    parsed = _make_parsed(text_tokens=["MAX232"])
    query_text, _variables = build_search_dql(parsed, manufacturer="Texas Instruments")

    positions = _edge_positions(query_text)
    assert positions["made_by"] < positions["in_category"] < positions["in_package"], (
        f"AC-SF-37: edge order must stay made_by -> in_category -> in_package "
        f"even with --manufacturer active. Positions: {positions}"
    )



# Follow-up (b) [lead_with_constraints removal] intentionally NOT done — its
# guard "unless a test pins the layout" is met by the merged AC-SF-15, which
# requires in_package to lead the selection body; see ADR-0017. The two
# edge-order-unification tests that used to live here
# (test_ac_sf_37_edge_order_unchanged_with_category_and_package_filters_active
# and test_ac_sf_37_edge_order_identical_regardless_of_active_filter_combo)
# were REMOVED (Gate 4, evidence-based, documented not silent) rather than
# replaced with layout-order assertions.


# ===========================================================================
# Gate 3 (Security SHOULD): direct boundary pins on the newly-PUBLIC
# validate_package. AC-SF-36 already proves the public name is importable
# and preserves the "SOIC-16" happy path; these tests pin the SPECIFIC
# reject/accept boundary cases the promotion must preserve exactly, calling
# validate_package(...) DIRECTLY (never through build_search_dql/the CLI).
# ===========================================================================

def test_ac_sf_36_validate_package_rejects_overlength() -> None:
    """Gate 3 / AC-SF-36 (Security SHOULD): Given a 21-character package
    value (one over the 20-char cap: "^[A-Z0-9][A-Z0-9\\-]{0,19}$").
    When validate_package(...) is called directly.
    Then a ValueError is raised.
    """
    from partgraph.query.dql_builder import validate_package  # noqa: PLC0415

    with pytest.raises(ValueError):
        validate_package("A" * 21)


def test_ac_sf_36_validate_package_rejects_lowercase() -> None:
    """Gate 3 / AC-SF-36 (Security SHOULD): Given a lowercase package value
    ("soic-16" — the charset requires uppercase A-Z/0-9/- only).
    When validate_package(...) is called directly.
    Then a ValueError is raised.
    """
    from partgraph.query.dql_builder import validate_package  # noqa: PLC0415

    with pytest.raises(ValueError):
        validate_package("soic-16")


def test_ac_sf_36_validate_package_rejects_injection_shaped_payload() -> None:
    """Gate 3 / AC-SF-36 (Security SHOULD): Given an injection-shaped
    payload ("0402; drop" — contains a space and a semicolon, well outside
    the package charset).
    When validate_package(...) is called directly.
    Then a ValueError is raised (defence in depth — the value would be bound
    as a $var regardless, but the charset guard must still reject it
    outright before it ever reaches that point).
    """
    from partgraph.query.dql_builder import validate_package  # noqa: PLC0415

    with pytest.raises(ValueError):
        validate_package("0402; drop")


def test_ac_sf_36_validate_package_accepts_valid_value_returns_unchanged() -> None:
    """Gate 3 / AC-SF-36 (Security SHOULD): Given a valid package value
    ("SOIC-16").
    When validate_package(...) is called directly.
    Then it returns the value unchanged (str, not mutated) — confirms the
    promotion preserves the exact pre-promotion charset/length logic.
    """
    from partgraph.query.dql_builder import validate_package  # noqa: PLC0415

    assert validate_package("SOIC-16") == "SOIC-16", (
        "Gate3/AC-SF-36: validate_package('SOIC-16') must return 'SOIC-16' unchanged."
    )


# ===========================================================================
# AC-HY: hybrid semantic search robustness (Gate-1 ratified contract)
#
# New constants (dql_builder.py, added to __all__):
#   SEMANTIC_CANDIDATE_CAP = 1500   (build_semantic_dql's internal k-clamp
#                                    ceiling; was MAX_RESULT_LIMIT=200 — see
#                                    the AC-SD-3 rewrite above)
#   SEMANTIC_CANDIDATE_FLOOR = 200  (the CLI's oversample floor)
#   SEMANTIC_OVERSAMPLE_FACTOR = 20 (the CLI's oversample multiplier)
# MAX_RESULT_LIMIT=200 stays the RESULT bound, unchanged.
#
# The CLI computes candidate_k = min(max(limit * 20, 200), 1500) and passes
# it as build_semantic_dql's `k` argument — see AC-HY-1 in
# tests/unit/test_cli_search.py for the CLI-level oversample-formula spy
# test (this file only pins the builder's OWN internal clamp + the new
# 'embedding' selection field + the constants themselves).
#
# NOTE: SEMANTIC_CANDIDATE_CAP/FLOOR/OVERSAMPLE_FACTOR do not exist yet.
# Every import below is LOCAL to its test function (never at module level)
# so a missing name only fails THAT test at call time (ImportError) rather
# than erroring collection of the whole file.
# ===========================================================================

# ---------------------------------------------------------------------------
# AC-HY-2: new candidate-pool constants exist, are pinned, and raise the
# builder's internal cap from 200 to 1500 (without lowering the floor of 1).
# ---------------------------------------------------------------------------

def test_ac_hy_2_semantic_candidate_constants_exist_and_are_pinned() -> None:
    """AC-HY-2: Given the dql_builder module.
    When SEMANTIC_CANDIDATE_CAP / SEMANTIC_CANDIDATE_FLOOR /
    SEMANTIC_OVERSAMPLE_FACTOR are imported.
    Then they equal 1500 / 200 / 20 respectively; MAX_RESULT_LIMIT stays 200
    (the RESULT bound, unaffected by the new candidate-pool constants); and
    all three new names are listed in __all__ (the public export contract).
    """
    import partgraph.query.dql_builder as dql_builder_mod
    from partgraph.query.dql_builder import (
        MAX_RESULT_LIMIT,
        SEMANTIC_CANDIDATE_CAP,
        SEMANTIC_CANDIDATE_FLOOR,
        SEMANTIC_OVERSAMPLE_FACTOR,
    )

    assert SEMANTIC_CANDIDATE_CAP == 1500, (
        f"AC-HY-2: SEMANTIC_CANDIDATE_CAP must be 1500. Got: {SEMANTIC_CANDIDATE_CAP!r}"
    )
    assert SEMANTIC_CANDIDATE_FLOOR == 200, (
        f"AC-HY-2: SEMANTIC_CANDIDATE_FLOOR must be 200. Got: {SEMANTIC_CANDIDATE_FLOOR!r}"
    )
    assert SEMANTIC_OVERSAMPLE_FACTOR == 20, (
        f"AC-HY-2: SEMANTIC_OVERSAMPLE_FACTOR must be 20. Got: {SEMANTIC_OVERSAMPLE_FACTOR!r}"
    )
    assert MAX_RESULT_LIMIT == 200, (
        "AC-HY-2: MAX_RESULT_LIMIT must stay the RESULT bound (200), "
        "unaffected by the new candidate-pool constants."
    )
    for name in (
        "SEMANTIC_CANDIDATE_CAP", "SEMANTIC_CANDIDATE_FLOOR",
        "SEMANTIC_OVERSAMPLE_FACTOR",
    ):
        assert name in dql_builder_mod.__all__, (
            f"AC-HY-2: {name!r} must be exported via __all__. "
            f"Got: {dql_builder_mod.__all__}"
        )


def test_ac_hy_2_k_between_old_and_new_cap_is_not_clamped_down_to_200() -> None:
    """AC-HY-2: Given k=1000 (above the OLD MAX_RESULT_LIMIT=200 cap but
    below the NEW SEMANTIC_CANDIDATE_CAP=1500).
    When build_semantic_dql(vector, 1000) is called.
    Then the effective k baked into similar_to(embedding, k, ...) is EXACTLY
    1000 — NOT silently clamped down to 200 as the pre-hybrid contract did.
    """
    vector = _unit_vector()
    query_text, _ = build_semantic_dql(vector, 1000)

    k_matches = re.findall(r"similar_to\([^,]+,\s*(\d+)", query_text)
    assert k_matches and int(k_matches[0]) == 1000, (
        f"AC-HY-2: k=1000 must pass through unclamped (new cap is 1500). "
        f"Got k={k_matches[0] if k_matches else '<none found>'} in:\n{query_text}"
    )


def test_ac_hy_2_k_1500_at_cap_not_clamped_below() -> None:
    """AC-HY-2: Given k=1500 (exactly at the new SEMANTIC_CANDIDATE_CAP).
    When build_semantic_dql(vector, 1500) is called.
    Then the effective k is exactly 1500 (the cap is inclusive, not a strict
    upper-exclusive bound).
    """
    vector = _unit_vector()
    query_text, _ = build_semantic_dql(vector, 1500)

    k_matches = re.findall(r"similar_to\([^,]+,\s*(\d+)", query_text)
    assert k_matches and int(k_matches[0]) == 1500, (
        f"AC-HY-2: k=1500 (at the cap) must remain 1500. "
        f"Got k={k_matches[0] if k_matches else '<none found>'} in:\n{query_text}"
    )


# ---------------------------------------------------------------------------
# AC-HY-3: semantic block selects 'embedding'; lexical build_search_dql does
# NOT (byte-identical lexical response).
# ---------------------------------------------------------------------------

def test_ac_hy_3_semantic_selects_embedding_lexical_does_not() -> None:
    """AC-HY-3: Given a 384-dim vector, k=10 (semantic) and a plain
    ParsedQuery with text_tokens=["MAX232"] (lexical).
    When build_semantic_dql(vector, 10) and build_search_dql(parsed) are
    both called.
    Then:
    - The semantic query's selection set includes a bare `embedding` field
      IN ADDITION to the existing `similar_to(embedding, ...)` root-function
      reference (>=2 occurrences of the word) — the ranker needs the raw
      embedding to compute cosine similarity client-side.
    - The lexical build_search_dql query text contains NO "embedding"
      substring anywhere (byte-identical lexical response — regression
      guard: the lexical path must never carry the 384-float vector).
    """
    vector = _unit_vector()
    semantic_text, _ = build_semantic_dql(vector, 10)

    assert semantic_text.count("embedding") >= 2, (
        f"AC-HY-3: expected 'embedding' selected as a bare field IN "
        f"ADDITION to the similar_to(embedding, ...) root reference "
        f"(>=2 occurrences). Got {semantic_text.count('embedding')} "
        f"occurrence(s) in:\n{semantic_text}"
    )
    assert re.search(r"^\s*embedding\s*$", semantic_text, re.MULTILINE), (
        f"AC-HY-3: expected a bare 'embedding' selected field (its own "
        f"line). Got:\n{semantic_text}"
    )

    parsed = _make_parsed(text_tokens=["MAX232"])
    lexical_text, _ = build_search_dql(parsed)
    assert "embedding" not in lexical_text, (
        f"AC-HY-3: lexical build_search_dql must NEVER select 'embedding' "
        f"(byte-identical lexical response). Got:\n{lexical_text}"
    )


# ---------------------------------------------------------------------------
# AC-HY-16: adding the 'embedding' selection field introduces no new
# injection surface; per-element _fmt_float validation of the vector literal
# is unaffected.
# ---------------------------------------------------------------------------

def test_ac_hy_16_embedding_selection_adds_no_injection_surface() -> None:
    """AC-HY-16: Given (a) a hostile vector whose last element is a
    quote-breaking payload (mirrors AC-SD-2) and (b) a valid vector with
    EVERY structured filter active (manufacturer/category/min_stock/
    is_basic/max_price).
    When build_semantic_dql is called for each.
    Then:
    - (a) STILL raises ValueError/TypeError — adding the bare 'embedding'
      selection field must not weaken or bypass the existing per-element
      _fmt_float validation of the similar_to(...) vector literal.
    - (b) the bare 'embedding' selection field is present, is the exact,
      unmodified literal word 'embedding' on its own line, and is NEVER
      bound as (or derived from) a $var — it introduces no new injection
      surface regardless of which/how many structured filters are active.
    """
    hostile_vector: list = [0.1] * (_EMBED_DIM - 1) + ['0.5", 1, "evil']
    with pytest.raises((ValueError, TypeError)):
        build_semantic_dql(hostile_vector, 10)  # type: ignore[arg-type]

    vector = _unit_vector()
    query_text, variables = build_semantic_dql(
        vector,
        10,
        manufacturer="Texas Instruments",
        category="RS232 ICs",
        min_stock=5,
        is_basic=True,
        max_price=0.5,
    )

    bare_embedding_lines = [
        line for line in query_text.splitlines() if line.strip() == "embedding"
    ]
    assert bare_embedding_lines, (
        f"AC-HY-16: expected a bare 'embedding' selection line even with "
        f"every structured filter active. Got:\n{query_text}"
    )
    assert not any("embedding" in str(v) for v in variables.values()), (
        f"AC-HY-16: 'embedding' must never be (part of) a $var value. "
        f"Got variables: {variables}"
    )
    assert not any("embedding" in k for k in variables), (
        f"AC-HY-16: 'embedding' must never be a $var name. Got: {variables}"
    )
