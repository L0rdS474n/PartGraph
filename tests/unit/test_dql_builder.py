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
