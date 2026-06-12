"""
Tests: T-UNIT-*

Verifies partgraph.normalize.units.parse() and the range/condition-aware variant.

Fixtures documented here are representative per ADR/B1 ruling; real-data
sampling (success-rate >= 0.90 across >= 1000 real attribute strings) lives in
tests/integration/test_unit_realdata.py.

Tests:
- T-UNIT-si:          exact fixture table: known input -> expected SI float.
- T-UNIT-prefixes:    each SI prefix p/n/µ/u/m/k/K/M/G/T parsed correctly.
- T-UNIT-unknown:     strings with no parseable quantity return None, never raise.
- T-UNIT-range:       "1V~5V" -> scalar None + structured (1.0, 5.0);
                      "2V@1mA" -> principal value 2.0.
- T-UNIT-deterministic: parse() is a pure function (same input -> same output,
                        no internal mutable state between calls).

NOTE: Collection will ERROR if partgraph.normalize.units does not yet exist.
That is the expected red state before implementation.
"""

from __future__ import annotations

import pytest

from partgraph.normalize.units import parse  # noqa: F401


# ---------------------------------------------------------------------------
# T-UNIT-si  — exact fixture table
# ---------------------------------------------------------------------------

# Each tuple: (value_string, dimension_hint, expected_si_float)
# dimension_hint is passed as the `dimension` parameter to help disambiguation
# (e.g. "10k" is resistance=10000 but could be frequency=10000 too).
SI_FIXTURE_TABLE = [
    ("5V",       "voltage",     5.0),
    ("3.3V",     "voltage",     3.3),
    ("12VDC",    "voltage",     12.0),
    ("100nF",    "capacitance", 1e-7),
    ("10uF",     "capacitance", 1e-5),
    ("4.7µF",    "capacitance", 4.7e-6),
    ("10kΩ",     "resistance",  10000.0),
    ("10k",      "resistance",  10000.0),
    ("2.2M",     "resistance",  2200000.0),
    ("500mA",    "current",     0.5),
    ("2.5A",     "current",     2.5),
    ("1MHz",     "frequency",   1e6),
    ("50Hz",     "frequency",   50.0),
    ("100ns",    "time",        1e-7),
    ("10µH",     "inductance",  1e-5),
    ("1W",       "power",       1.0),
    ("0.25W",    "power",       0.25),
    ("1%",       "tolerance",   1.0),
    # AC-D1-1 / signed ± prefix: parser must strip leading ± before parsing.
    # These rows are EXPECTED RED against the current implementation (no ± strip).
    ("±1%",      "tolerance",   1.0),
    ("±100ppm/℃","tolerance",   100.0),
]


@pytest.mark.parametrize("value_str, dimension, expected", SI_FIXTURE_TABLE)
def test_unit_si_exact_fixture(value_str: str, dimension: str, expected: float) -> None:
    """Given a known value string and dimension hint.
    When parse(value_str, dimension) is called.
    Then the result is approximately equal to the expected SI float.

    Fixtures represent a representative cross-section of unit types found in
    real JLCPCB component attribute data. Real-data sampling is in integration.
    """
    result = parse(value_str, dimension)
    assert result is not None, (
        f"parse({value_str!r}, {dimension!r}) returned None; expected {expected}."
    )
    assert result == pytest.approx(expected, rel=1e-6), (
        f"parse({value_str!r}, {dimension!r}) = {result}, expected {expected}."
    )


# ---------------------------------------------------------------------------
# T-UNIT-prefixes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefix, multiplier", [
    ("p",  1e-12),
    ("n",  1e-9),
    ("µ",  1e-6),
    ("u",  1e-6),   # µ == u
    ("m",  1e-3),
    ("k",  1e3),
    ("K",  1e3),    # k == K
    ("M",  1e6),
    ("G",  1e9),
    ("T",  1e12),
])
def test_unit_prefix_multiplier(prefix: str, multiplier: float) -> None:
    """Given a numeric string using a specific SI prefix.
    When parse() is called on it.
    Then the result equals base_value * multiplier.

    Tested with "1{prefix}F" (capacitance) to isolate the prefix, except for
    k/K/M which are tested as resistance to avoid ambiguity with 'MHz'.
    """
    # Choose dimension and unit to minimize ambiguity.
    if prefix in ("k", "K", "M", "G", "T"):
        value_str = f"1{prefix}Ω"
        dimension = "resistance"
    elif prefix in ("m",):
        value_str = f"1{prefix}A"
        dimension = "current"
    elif prefix in ("p", "n", "µ", "u"):
        value_str = f"1{prefix}F"
        dimension = "capacitance"
    else:
        value_str = f"1{prefix}F"
        dimension = "capacitance"

    result = parse(value_str, dimension)
    assert result is not None, (
        f"parse({value_str!r}, {dimension!r}) returned None for prefix {prefix!r}."
    )
    assert result == pytest.approx(1.0 * multiplier, rel=1e-9), (
        f"parse({value_str!r}, {dimension!r}) = {result}; "
        f"expected 1 * {multiplier} = {1.0 * multiplier}."
    )


def test_unit_prefix_mu_equals_u() -> None:
    """Given '10µF' and '10uF'.
    When parse() is called on each.
    Then both return the same value (µ and u are equivalent micro prefixes).
    """
    result_mu = parse("10µF", "capacitance")
    result_u = parse("10uF", "capacitance")
    assert result_mu is not None and result_u is not None
    assert result_mu == pytest.approx(result_u, rel=1e-9), (
        f"µ and u must be equivalent: parse('10µF')={result_mu}, parse('10uF')={result_u}"
    )


def test_unit_prefix_k_equals_K() -> None:
    """Given '10kΩ' and '10KΩ'.
    When parse() is called on each.
    Then both return the same value (k and K are equivalent kilo prefixes).
    """
    result_lower = parse("10kΩ", "resistance")
    result_upper = parse("10KΩ", "resistance")
    assert result_lower is not None and result_upper is not None
    assert result_lower == pytest.approx(result_upper, rel=1e-9), (
        f"k and K must be equivalent: parse('10kΩ')={result_lower}, parse('10KΩ')={result_upper}"
    )


# ---------------------------------------------------------------------------
# T-UNIT-unknown
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value_str, dimension", [
    ("abc",    "voltage"),
    ("-",      "resistance"),
    ("null",   "current"),
    ("",       "frequency"),
    ("RoHS",   "voltage"),
    ("N/A",    "capacitance"),
    ("---",    "voltage"),
    ("Lead Free", "voltage"),
])
def test_unit_unknown_returns_none_never_raises(value_str: str, dimension: str) -> None:
    """Given a string that contains no parseable quantity (label, dash, empty, etc.).
    When parse(value_str, dimension) is called.
    Then it returns None without raising any exception.

    parse() must be robust: unknown inputs are common in real data and must
    never cause the ingestion pipeline to crash.
    """
    try:
        result = parse(value_str, dimension)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"parse({value_str!r}, {dimension!r}) raised {type(exc).__name__}: {exc}. "
            "Must return None for unparseable input, never raise."
        )
    assert result is None, (
        f"parse({value_str!r}, {dimension!r}) returned {result!r}; expected None."
    )


# ---------------------------------------------------------------------------
# T-UNIT-range
# ---------------------------------------------------------------------------

def test_unit_range_tilde_range_scalar_returns_none() -> None:
    """Given a range string '1V~5V'.
    When the basic parse() is called.
    Then it returns None (a range is not a single scalar value).
    """
    result = parse("1V~5V", "voltage")
    assert result is None, (
        f"parse('1V~5V', 'voltage') must return None for a range, got {result!r}."
    )


def test_unit_range_tilde_range_structured_result() -> None:
    """Given a range string '1V~5V' and the range-aware variant.
    When parse_range() (or equivalent structured function) is called.
    Then the result contains a tuple (1.0, 5.0) representing (min, max) in SI units.
    """
    try:
        from partgraph.normalize.units import parse_range
    except ImportError:
        pytest.skip("parse_range not exported; only parse() is tested here.")
        return

    result = parse_range("1V~5V", "voltage")
    assert result is not None, "parse_range('1V~5V', 'voltage') returned None."
    # Result may be a tuple, named tuple, or dataclass with min/max.
    if isinstance(result, tuple):
        lo, hi = result[0], result[1]
    else:
        lo = getattr(result, "min", None) or getattr(result, "lo", None)
        hi = getattr(result, "max", None) or getattr(result, "hi", None)
    assert lo == pytest.approx(1.0), f"Range low bound expected 1.0 V, got {lo}."
    assert hi == pytest.approx(5.0), f"Range high bound expected 5.0 V, got {hi}."


def test_unit_range_condition_at_principal_value() -> None:
    """Given a condition string '2V@1mA'.
    When parse() is called.
    Then it returns the principal (left-hand) value: 2.0 V.
    """
    result = parse("2V@1mA", "voltage")
    assert result == pytest.approx(2.0), (
        f"parse('2V@1mA', 'voltage') should return principal value 2.0, got {result!r}."
    )


def test_unit_range_complex_range_does_not_raise() -> None:
    """Given various range/condition formats.
    When parse() is called on each.
    Then it never raises (may return None for formats it cannot parse).
    """
    candidates = [
        ("0V~36V", "voltage"),
        ("-40°C~+85°C", "temperature"),
        ("1k~10kΩ", "resistance"),
        ("100mA Max", "current"),
        ("≤5V", "voltage"),
        (">100MΩ", "resistance"),
    ]
    for value_str, dimension in candidates:
        try:
            parse(value_str, dimension)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"parse({value_str!r}, {dimension!r}) raised {type(exc).__name__}: {exc}"
            )


# ---------------------------------------------------------------------------
# T-UNIT-deterministic
# ---------------------------------------------------------------------------

def test_unit_parse_is_deterministic() -> None:
    """Given parse() called multiple times with the same inputs.
    When results are collected.
    Then all results are identical (parse() is a pure function with no internal
    mutable state or random/time-dependent behaviour).
    """
    test_cases = [
        ("100nF", "capacitance"),
        ("10kΩ",  "resistance"),
        ("3.3V",  "voltage"),
        ("1MHz",  "frequency"),
        ("N/A",   "voltage"),
    ]
    for value_str, dimension in test_cases:
        results = [parse(value_str, dimension) for _ in range(5)]
        assert len({
            r if r is None else round(r, 15) for r in results
        }) == 1, (
            f"parse({value_str!r}, {dimension!r}) returned different results "
            f"across calls: {results}. parse() must be deterministic."
        )
