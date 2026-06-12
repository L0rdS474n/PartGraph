"""Deterministic, locale-independent unit parsing for component attributes.

:func:`parse` converts a single attribute string such as ``"10kΩ"`` or
``"500mA"`` into a base-SI float (``10000.0`` / ``0.5``). It is a pure function:
the same input always yields the same output, with no dependence on locale,
clock, or mutable module state. Anything it cannot interpret returns ``None``
rather than raising, because unparseable values are common in real catalogue
data and must never crash the ingestion pipeline.

:func:`parse_range` handles ``"1V~5V"`` style ranges, returning a ``(min, max)``
tuple of SI floats.

Design:
- A leading signed decimal number is extracted.
- The immediate suffix is inspected for an SI prefix (case-sensitive, so ``m``
  is milli and ``M`` is mega). Exactly one prefix character is consumed.
- Any remaining characters (the unit symbol and trailing noise such as ``DC``
  or `` Max``) are ignored — the dimension hint is accepted for API symmetry
  and future disambiguation but is not required to compute the magnitude.
"""

from __future__ import annotations

import re

__all__ = ["parse", "parse_range"]

# SI prefix multipliers. Case-sensitive on purpose: 'm' (milli) and 'M' (mega)
# must never collide, and 'k'/'K' as well as 'µ'/'u' are treated as synonyms.
_SI_PREFIXES: dict[str, float] = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,  # µ (MICRO SIGN)
    "μ": 1e-6,  # μ (GREEK SMALL LETTER MU) — sometimes used interchangeably
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
}

# A leading signed decimal at the very start of the (stripped) string.
# Examples matched: "5", "3.3", "-40", "0.25", "100".
_LEADING_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?")

# Comparator / qualifier characters that may precede a value (e.g. "≤5V", ">100MΩ").
# A leading "±" (PLUS-MINUS SIGN, U+00B1) is treated as a magnitude qualifier and
# stripped: "±1%" -> 1.0, "±100ppm/℃" -> 100.0 (the sign is dropped, the
# magnitude kept). Real tolerance/temperature-coefficient values are written this
# way and must parse to their positive magnitude.
_LEADING_COMPARATORS = "±≤≥<>=≈~ "  # ± ≤ ≥ < > = ≈ ~ space

# Characters that indicate a range or a conditional qualifier.
_RANGE_SEP = "~"
_CONDITION_SEP = "@"

# Base unit symbols that may legitimately follow an SI prefix. A prefix is only
# applied when it stands alone (e.g. "2.2M") or is immediately followed by one of
# these symbols (e.g. "100nF", "100ns", "1MHz", "10µH"). This prevents a leading
# letter of a multi-letter token from being mis-read as a prefix — e.g. the "p"
# of "ppm" in "100ppm/℃" must NOT be consumed as pico.
#   V Ω F H A W s % J — common electrical/SI base symbols (H covers Henry and the
#   leading H of "Hz"; s covers seconds, e.g. "100ns").
_UNIT_SYMBOLS = frozenset("VΩFHAWsJ%")


def _parse_scalar(value_str: str) -> float | None:
    """Parse a single magnitude token (no range/condition handling).

    Returns the base-SI float, or ``None`` if no leading number is present.
    Never raises.
    """
    s = value_str.strip().lstrip(_LEADING_COMPARATORS)
    if not s:
        return None

    match = _LEADING_NUMBER_RE.match(s)
    if match is None:
        return None

    try:
        base = float(match.group(0))
    except ValueError:  # pragma: no cover — regex guarantees a valid float
        return None

    remainder = s[match.end():].strip()
    if remainder:
        prefix_char = remainder[0]
        multiplier = _SI_PREFIXES.get(prefix_char)
        # Apply the prefix only when it stands alone (bare multiplier, e.g.
        # "2.2M") or is immediately followed by a recognised unit symbol (e.g.
        # "100nF", "1MHz"). A prefix letter that merely begins a longer word
        # (e.g. the "p" of "ppm") is NOT a prefix and is ignored.
        if multiplier is not None and (
            len(remainder) == 1 or remainder[1] in _UNIT_SYMBOLS
        ):
            return base * multiplier

    return base


def parse(value_str: str | None, dimension: str | None = None) -> float | None:
    """Parse *value_str* into a base-SI float, or ``None``.

    Args:
        value_str: The raw attribute string (e.g. ``"100nF"``). ``None`` and
            empty strings return ``None``.
        dimension: Optional dimension hint (e.g. ``"resistance"``). Accepted for
            API symmetry; the magnitude is derived from the SI prefix alone, so
            the hint does not change the result for the supported fixtures.

    Returns:
        The numeric value in base SI units, or ``None`` when the string contains
        no parseable single quantity. A range (``"1V~5V"``) returns ``None`` —
        use :func:`parse_range` for those.

    Never raises for any input.
    """
    if value_str is None:
        return None
    s = value_str.strip()
    if not s:
        return None

    # A range is not a single scalar — callers must use parse_range().
    if _RANGE_SEP in s:
        return None

    # A condition ("2V@1mA") yields the principal (left-hand) value.
    if _CONDITION_SEP in s:
        s = s.split(_CONDITION_SEP, 1)[0]

    return _parse_scalar(s)


def parse_range(
    value_str: str | None,
    dimension: str | None = None,
) -> tuple[float, float] | None:
    """Parse a ``"<lo>~<hi>"`` range into an ``(lo, hi)`` tuple of SI floats.

    Args:
        value_str: A range string such as ``"1V~5V"``. Non-range strings, or
            ranges whose endpoints are not parseable, return ``None``.
        dimension: Optional dimension hint. Accepted for API symmetry with
            :func:`parse`; the magnitude derives from the SI prefix alone.

    Returns:
        ``(lo, hi)`` in base SI units, or ``None``. Never raises.
    """
    if value_str is None:
        return None
    s = value_str.strip()
    if _RANGE_SEP not in s:
        return None

    lo_str, _, hi_str = s.partition(_RANGE_SEP)
    lo = _parse_scalar(lo_str)
    hi = _parse_scalar(hi_str)
    if lo is None or hi is None:
        return None
    return (lo, hi)
