"""
Tests: SEARCH-1..12 — partgraph.query.parser

Specifies the behavior of parse_query() which converts a free-text component
search string into a ParsedQuery frozen dataclass.

Module under test: partgraph.query.parser
  - parse_query(s: str) -> ParsedQuery   (pure/total/never-raises)
  - ParsedQuery.quantities: list[Quantity(predicate, value, raw)]
  - ParsedQuery.package: str | None
  - ParsedQuery.text_tokens: list[str]
  - ParsedQuery.raw_query: str

Design decisions pinned by dispatcher:
  - "10k"  -> resistance=10000; "10k" excluded from text_tokens.
  - "100nF" -> capacitance=1e-7.
  - "1%"  -> tolerance_pct=1.0.
  - "1.2V" -> voltage_max=1.2.
  - "0402" -> package="0402" (NOT a quantity, NOT a text token).
  - "MAX232" -> text_tokens=["MAX232"] (no quantities, no package).
  - "10k 0402 1%" -> resistance=10000, tolerance_pct=1.0, package="0402", text_tokens=[].
  - "1.2V MAX232" -> voltage_max=1.2, text_tokens=["MAX232"].
  - "10kΩ"/"100µF"/"100μF" — unicode symbols parsed identically to ASCII.
  - ""/"   "/"!!!" — no quantities, no package, text_tokens empty-or-dropped, NEVER raises.
  - "5Z" — units.parse("5Z")==5.0 but "Z" is not a known unit symbol -> text token.
  - "1V~5V" range — no scalar quantity produced (degrade to text/drop).
  - "10000" (bare unitless) -> text token, NOT resistance.

NOTE: Collection will ERROR on import of partgraph.query.parser because that
module does not exist yet. That is the correct red state before PR3 implementation.
"""

from __future__ import annotations

import pytest

from partgraph.query.parser import ParsedQuery, Quantity, parse_query  # noqa: F401 — red until impl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quantities_by_predicate(pq: ParsedQuery) -> dict[str, float]:
    """Return {predicate: value} from a ParsedQuery for easy assertions."""
    return {q.predicate: q.value for q in pq.quantities}


# ---------------------------------------------------------------------------
# SEARCH-1: "10k" -> resistance=10000; "10k" excluded from text_tokens
# ---------------------------------------------------------------------------

def test_search_1_10k_parses_to_resistance() -> None:
    """Given the query string "10k".
    When parse_query is called.
    Then quantities contains exactly one entry with predicate="resistance" and
    value=10000.0, and text_tokens does NOT contain "10k".
    """
    pq = parse_query("10k")

    quantities = _quantities_by_predicate(pq)
    assert "resistance" in quantities, (
        f"Expected predicate 'resistance' in quantities for '10k'. Got: {quantities}"
    )
    assert quantities["resistance"] == pytest.approx(10000.0), (
        f"Expected resistance=10000.0 for '10k'. Got: {quantities['resistance']}"
    )
    assert "10k" not in pq.text_tokens, (
        f"'10k' must not appear in text_tokens; it is a resistance quantity. "
        f"text_tokens={pq.text_tokens}"
    )


# ---------------------------------------------------------------------------
# SEARCH-2: "100nF" -> capacitance=1e-7
# ---------------------------------------------------------------------------

def test_search_2_100nf_parses_to_capacitance() -> None:
    """Given the query string "100nF".
    When parse_query is called.
    Then quantities contains predicate="capacitance" with value=1e-7 (100 * 1e-9).
    """
    pq = parse_query("100nF")

    quantities = _quantities_by_predicate(pq)
    assert "capacitance" in quantities, (
        f"Expected predicate 'capacitance' in quantities for '100nF'. Got: {quantities}"
    )
    assert quantities["capacitance"] == pytest.approx(1e-7), (
        f"Expected capacitance=1e-7 for '100nF'. Got: {quantities['capacitance']}"
    )
    assert "100nF" not in pq.text_tokens, (
        f"'100nF' must not appear in text_tokens. text_tokens={pq.text_tokens}"
    )


# ---------------------------------------------------------------------------
# SEARCH-3: "1%" -> tolerance_pct=1.0
# ---------------------------------------------------------------------------

def test_search_3_1pct_parses_to_tolerance_pct() -> None:
    """Given the query string "1%".
    When parse_query is called.
    Then quantities contains predicate="tolerance_pct" with value=1.0.
    """
    pq = parse_query("1%")

    quantities = _quantities_by_predicate(pq)
    assert "tolerance_pct" in quantities, (
        f"Expected predicate 'tolerance_pct' in quantities for '1%'. Got: {quantities}"
    )
    assert quantities["tolerance_pct"] == pytest.approx(1.0), (
        f"Expected tolerance_pct=1.0 for '1%'. Got: {quantities['tolerance_pct']}"
    )


# ---------------------------------------------------------------------------
# SEARCH-4: "1.2V" -> voltage_max=1.2 (ADR-VOLT: bare "V" -> voltage_max)
# ---------------------------------------------------------------------------

def test_search_4_1_2v_parses_to_voltage_max() -> None:
    """Given the query string "1.2V".
    When parse_query is called.
    Then quantities contains predicate="voltage_max" with value=1.2.
    (ADR-VOLT: bare "V" suffix -> voltage_max predicate.)
    """
    pq = parse_query("1.2V")

    quantities = _quantities_by_predicate(pq)
    assert "voltage_max" in quantities, (
        f"Expected predicate 'voltage_max' in quantities for '1.2V'. Got: {quantities}"
    )
    assert quantities["voltage_max"] == pytest.approx(1.2), (
        f"Expected voltage_max=1.2 for '1.2V'. Got: {quantities['voltage_max']}"
    )
    assert "1.2V" not in pq.text_tokens, (
        f"'1.2V' must not appear in text_tokens. text_tokens={pq.text_tokens}"
    )


# ---------------------------------------------------------------------------
# SEARCH-5: "0402" -> package="0402" (not quantity, not text token)
# ---------------------------------------------------------------------------

def test_search_5_0402_parsed_as_package_not_quantity_not_text() -> None:
    """Given the query string "0402".
    When parse_query is called.
    Then package="0402", quantities is empty, and text_tokens does not contain "0402".
    (Package codes are a distinct classification tier; they must NOT be misclassified
    as numeric quantities or generic text tokens.)
    """
    pq = parse_query("0402")

    assert pq.package == "0402", (
        f"Expected package='0402' for '0402'. Got: {pq.package!r}"
    )
    assert pq.quantities == [], (
        f"Expected no quantities for '0402'. Got: {pq.quantities}"
    )
    assert "0402" not in pq.text_tokens, (
        f"'0402' must not appear in text_tokens (it is a package). "
        f"text_tokens={pq.text_tokens}"
    )


# ---------------------------------------------------------------------------
# SEARCH-6: "MAX232" -> text_tokens=["MAX232"] (no quantities, no package)
# ---------------------------------------------------------------------------

def test_search_6_max232_is_text_token() -> None:
    """Given the query string "MAX232".
    When parse_query is called.
    Then text_tokens=["MAX232"], quantities is empty, and package is None.
    """
    pq = parse_query("MAX232")

    assert "MAX232" in pq.text_tokens, (
        f"Expected 'MAX232' in text_tokens. Got: {pq.text_tokens}"
    )
    assert pq.quantities == [], (
        f"Expected no quantities for 'MAX232'. Got: {pq.quantities}"
    )
    assert pq.package is None, (
        f"Expected package=None for 'MAX232'. Got: {pq.package!r}"
    )


# ---------------------------------------------------------------------------
# SEARCH-7: "10k 0402 1%" -> resistance=10000, tolerance_pct=1.0, package="0402",
#            text_tokens=[]
# ---------------------------------------------------------------------------

def test_search_7_composite_10k_0402_1pct() -> None:
    """Given the query string "10k 0402 1%".
    When parse_query is called.
    Then:
      - quantities contains resistance=10000.0 and tolerance_pct=1.0
      - package="0402"
      - text_tokens=[] (no unrecognised tokens remain)
    """
    pq = parse_query("10k 0402 1%")

    quantities = _quantities_by_predicate(pq)
    assert "resistance" in quantities, (
        f"Expected resistance in quantities for '10k 0402 1%'. Got: {quantities}"
    )
    assert quantities["resistance"] == pytest.approx(10000.0), (
        f"Expected resistance=10000.0. Got: {quantities['resistance']}"
    )
    assert "tolerance_pct" in quantities, (
        f"Expected tolerance_pct in quantities. Got: {quantities}"
    )
    assert quantities["tolerance_pct"] == pytest.approx(1.0), (
        f"Expected tolerance_pct=1.0. Got: {quantities['tolerance_pct']}"
    )
    assert pq.package == "0402", (
        f"Expected package='0402'. Got: {pq.package!r}"
    )
    assert pq.text_tokens == [], (
        f"Expected text_tokens=[] for fully-parsed query. Got: {pq.text_tokens}"
    )


# ---------------------------------------------------------------------------
# SEARCH-8: "1.2V MAX232" -> voltage_max=1.2, text_tokens=["MAX232"]
# ---------------------------------------------------------------------------

def test_search_8_composite_1_2v_max232() -> None:
    """Given the query string "1.2V MAX232".
    When parse_query is called.
    Then:
      - quantities contains voltage_max=1.2
      - text_tokens=["MAX232"]
      - package=None
    """
    pq = parse_query("1.2V MAX232")

    quantities = _quantities_by_predicate(pq)
    assert "voltage_max" in quantities, (
        f"Expected voltage_max in quantities for '1.2V MAX232'. Got: {quantities}"
    )
    assert quantities["voltage_max"] == pytest.approx(1.2), (
        f"Expected voltage_max=1.2. Got: {quantities['voltage_max']}"
    )
    assert "MAX232" in pq.text_tokens, (
        f"Expected 'MAX232' in text_tokens. Got: {pq.text_tokens}"
    )
    assert pq.package is None, (
        f"Expected package=None. Got: {pq.package!r}"
    )


# ---------------------------------------------------------------------------
# SEARCH-9: Unicode variants "10kΩ" and "100µF" / "100μF"
# ---------------------------------------------------------------------------

def test_search_9a_10k_omega_parses_to_resistance() -> None:
    """Given the query string "10kΩ" (with Omega symbol).
    When parse_query is called.
    Then quantities contains resistance=10000.0 (identical to "10k").
    """
    pq = parse_query("10kΩ")

    quantities = _quantities_by_predicate(pq)
    assert "resistance" in quantities, (
        f"Expected resistance in quantities for '10kΩ'. Got: {quantities}"
    )
    assert quantities["resistance"] == pytest.approx(10000.0), (
        f"Expected resistance=10000.0 for '10kΩ'. Got: {quantities['resistance']}"
    )


def test_search_9b_100_micro_f_micro_sign_parses_to_capacitance() -> None:
    """Given the query string "100µF" (MICRO SIGN U+00B5).
    When parse_query is called.
    Then quantities contains capacitance=1e-4 (100 * 1e-6).
    """
    pq = parse_query("100µF")  # µ = MICRO SIGN

    quantities = _quantities_by_predicate(pq)
    assert "capacitance" in quantities, (
        f"Expected capacitance for '100µF'. Got: {quantities}"
    )
    assert quantities["capacitance"] == pytest.approx(1e-4), (
        f"Expected capacitance=1e-4 for '100µF'. Got: {quantities['capacitance']}"
    )


def test_search_9c_100_micro_f_greek_mu_parses_to_capacitance() -> None:
    """Given the query string "100μF" (GREEK SMALL LETTER MU U+03BC).
    When parse_query is called.
    Then quantities contains capacitance=1e-4 (same as µ variant).
    """
    pq = parse_query("100μF")  # μ = GREEK SMALL LETTER MU

    quantities = _quantities_by_predicate(pq)
    assert "capacitance" in quantities, (
        f"Expected capacitance for '100μF'. Got: {quantities}"
    )
    assert quantities["capacitance"] == pytest.approx(1e-4), (
        f"Expected capacitance=1e-4 for '100μF'. Got: {quantities['capacitance']}"
    )


# ---------------------------------------------------------------------------
# SEARCH-10: Edge inputs "" / "   " / "!!!" — never raises, no quantities,
#             no package, text_tokens empty or only punctuation-dropped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", ["", "   ", "!!!"])
def test_search_10_degenerate_inputs_never_raise(query: str) -> None:
    """Given a degenerate input (empty string, whitespace, or punctuation only).
    When parse_query is called.
    Then no exception is raised and the result is a ParsedQuery with no quantities
    and no package. (text_tokens may be empty; punctuation tokens may be dropped.)
    """
    # Must not raise under any input — this is a contract requirement.
    pq = parse_query(query)

    assert isinstance(pq, ParsedQuery), (
        f"parse_query({query!r}) must return ParsedQuery, not raise. Got: {pq!r}"
    )
    assert pq.quantities == [], (
        f"Degenerate input {query!r}: expected no quantities. Got: {pq.quantities}"
    )
    assert pq.package is None, (
        f"Degenerate input {query!r}: expected package=None. Got: {pq.package!r}"
    )


def test_search_10_raw_query_preserved() -> None:
    """Given the query string "MAX232".
    When parse_query is called.
    Then raw_query is the original input string unchanged.
    """
    pq = parse_query("MAX232")
    assert pq.raw_query == "MAX232", (
        f"Expected raw_query='MAX232'. Got: {pq.raw_query!r}"
    )


# ---------------------------------------------------------------------------
# SEARCH-11: "5Z" — "Z" is not a recognised unit symbol -> text token
#             (units.parse("5Z")==5.0 but parser requires a known dimension)
# ---------------------------------------------------------------------------

def test_search_11_5z_not_a_quantity_is_text_token() -> None:
    """Given the query string "5Z".
    When parse_query is called.
    Then "5Z" is NOT parsed as a numeric quantity (no recognised dimension),
    and it appears in text_tokens (or is silently dropped — but must NOT produce
    a Quantity).

    Rationale: units.parse("5Z") returns 5.0 via the fallback path, but "Z" is
    not a recognised EE unit symbol so the parser must not emit a quantity.
    A bare magnitude without a known dimension is ambiguous and unsafe to promote.
    """
    pq = parse_query("5Z")

    assert pq.quantities == [], (
        f"'5Z' must not produce any quantity (no known dimension for 'Z'). "
        f"Got: {pq.quantities}"
    )


# ---------------------------------------------------------------------------
# SEARCH-11b: "10000" (bare unitless integer) -> text token, NOT resistance
# ---------------------------------------------------------------------------

def test_search_11b_bare_unitless_number_is_text_token_not_quantity() -> None:
    """Given the query string "10000" (a bare integer with no unit suffix).
    When parse_query is called.
    Then quantities is empty (no quantity emitted without a unit suffix) and
    "10000" is treated as a text token or silently dropped — NOT assigned to any
    predicate such as resistance.

    PIN: bare unitless number like "10000" -> text token, NOT resistance.
    """
    pq = parse_query("10000")

    assert pq.quantities == [], (
        f"'10000' (bare number, no unit) must not produce a quantity. "
        f"Got: {pq.quantities}"
    )
    assert pq.package is None, (
        f"'10000' must not be parsed as a package. Got: {pq.package!r}"
    )


# ---------------------------------------------------------------------------
# SEARCH-12: "1V~5V" range -> no scalar quantity (degrade to text/drop)
# ---------------------------------------------------------------------------

def test_search_12_voltage_range_produces_no_scalar_quantity() -> None:
    """Given the query string "1V~5V".
    When parse_query is called.
    Then quantities is empty (range notation is not a single scalar quantity).
    The token may be dropped or retained as text, but it MUST NOT produce a
    Quantity with predicate voltage_max or voltage_min.

    Rationale: ADR-NEAREST and ADR-PARAM deal with scalar comparisons; a range
    input cannot be assigned to a single predicate without context that the parser
    does not have.
    """
    pq = parse_query("1V~5V")

    assert pq.quantities == [], (
        f"'1V~5V' range must not produce any scalar Quantity. Got: {pq.quantities}"
    )


# ---------------------------------------------------------------------------
# Structural / dataclass contract tests
# ---------------------------------------------------------------------------

def test_parsed_query_is_frozen() -> None:
    """Given a ParsedQuery returned by parse_query.
    When we try to mutate a field.
    Then a FrozenInstanceError (or equivalent AttributeError) is raised, confirming
    that ParsedQuery is a frozen dataclass.
    """
    pq = parse_query("10k")
    try:
        pq.package = "something"  # type: ignore[misc]
        raise AssertionError("ParsedQuery must be frozen; mutation should have raised.")
    except (AttributeError, TypeError):
        pass  # expected — frozen dataclass or __setattr__ raising


def test_quantity_has_required_fields() -> None:
    """Given a Quantity from a parsed query.
    When we inspect its fields.
    Then it has predicate (str), value (float), and raw (str).
    """
    pq = parse_query("10k")
    assert pq.quantities, "Expected at least one Quantity for '10k'."
    q = pq.quantities[0]
    assert isinstance(q.predicate, str) and q.predicate, (
        f"Quantity.predicate must be a non-empty str. Got: {q.predicate!r}"
    )
    assert isinstance(q.value, float), (
        f"Quantity.value must be float. Got: {type(q.value)}"
    )
    assert isinstance(q.raw, str) and q.raw, (
        f"Quantity.raw must be a non-empty str (original token). Got: {q.raw!r}"
    )


def test_parse_query_never_raises_on_arbitrary_unicode() -> None:
    """Given arbitrary unicode strings that could appear in real catalogue data.
    When parse_query is called on each.
    Then no exception is raised (total function contract).
    """
    tricky_inputs = [
        "≤5V",
        "±1%",
        ">100MΩ",
        "100ppm/°C",
        "2V@1mA",
        "N/A",
        "—",
        "\x00\xff",
        "A" * 200,
    ]
    for inp in tricky_inputs:
        try:
            parse_query(inp)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"parse_query({inp!r}) must never raise. Got: {exc!r}"
            ) from exc


# ---------------------------------------------------------------------------
# A1 — DoS bounds (SECURITY — Concern 4 FAIL)
# PIN: MAX_QUERY_LEN=500 (in parser), MAX_TOKENS=10 (in parser).
# These tests define the contract the implementation MUST satisfy.
# ---------------------------------------------------------------------------

def test_query_parser_very_long_input_bounded_token_count() -> None:
    """Given a repeated token that produces 200 tokens of "10k ".
    When parse_query is called on the 2 000-char input.
    Then:
      - No exception is raised (total-function contract preserved).
      - len(text_tokens) + len(quantities) <= 10
        (PIN: MAX_TOKENS=10 — the parser caps token emission to prevent DoS).

    Security rationale: an unbounded tokenisation loop on attacker-controlled
    input is a CPU/memory DoS vector. Capping at MAX_TOKENS=10 closes it.
    """
    # "10k " * 200 -> 200 tokens; parser must cap output at MAX_TOKENS=10.
    long_input = "10k " * 200
    try:
        pq = parse_query(long_input)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"parse_query on very-long input must not raise. Got: {exc!r}"
        ) from exc

    total_tokens = len(pq.text_tokens) + len(pq.quantities)
    assert total_tokens <= 10, (
        f"PIN: MAX_TOKENS=10 — total output tokens must be <= 10. "
        f"Got len(text_tokens)={len(pq.text_tokens)} + "
        f"len(quantities)={len(pq.quantities)} = {total_tokens}. "
        "Implement token-count cap in parser to satisfy DoS bound."
    )


def test_query_parser_10000_char_input_no_raise() -> None:
    """Given a 10 000-character input of the single letter "x" repeated.
    When parse_query is called.
    Then:
      - No exception is raised (total-function contract preserved).
      - A ParsedQuery is returned.
      - The implementation must not allocate O(N) tokens for arbitrarily long
        single-token inputs: the parser must honour MAX_QUERY_LEN=500 by
        truncating or rejecting beyond that threshold.

    Security rationale: a 10 000-char single token could cause unbounded regex
    backtracking or string allocation. Capping at MAX_QUERY_LEN=500 closes it.
    Note: what matters is no-raise + returns ParsedQuery; we do NOT assert the
    exact truncation strategy — only that the output is a valid ParsedQuery.
    """
    huge_input = "x" * 10000
    try:
        pq = parse_query(huge_input)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"parse_query('x'*10000) must not raise. Got: {exc!r}"
        ) from exc

    assert isinstance(pq, ParsedQuery), (
        f"parse_query('x'*10000) must return a ParsedQuery. Got: {type(pq)!r}"
    )
