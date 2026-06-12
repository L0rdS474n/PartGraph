"""Pure, total parsing of a free-text component search string.

:func:`parse_query` converts a query such as ``"10k 0402 1%"`` into a
:class:`ParsedQuery` carrying:

- ``quantities`` — recognised numeric parameters (e.g. resistance=10000.0),
  each with the SI-normalised magnitude and the predicate it maps to.
- ``package`` — a single package code (e.g. ``"0402"`` or ``"SOT-23"``).
- ``text_tokens`` — remaining free-text tokens (e.g. an MPN like ``"MAX232"``).

It is a *pure, total* function: the same input always yields the same output,
it never depends on locale or mutable state, and it never raises for any input
(including arbitrary or hostile unicode).

A token only becomes a :class:`Quantity` when it carries a *recognised* unit
symbol. ``units.parse`` extracts a magnitude even from ``"5Z"`` (5.0) or a bare
``"10000"``, but those have no recognised electrical dimension, so they are
*not* promoted to quantities — they degrade to text tokens. This avoids
silently assigning an ambiguous bare number to a predicate such as resistance.

Security (ADR-0007): the input is truncated to ``MAX_QUERY_LEN`` characters
before tokenising and the total number of emitted tokens is capped at
``MAX_TOKENS`` so that attacker-controlled input cannot drive an unbounded
tokenisation loop (CPU/memory DoS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from partgraph.normalize import units

__all__ = ["MAX_QUERY_LEN", "MAX_TOKENS", "ParsedQuery", "Quantity", "parse_query"]

# ---------------------------------------------------------------------------
# DoS bounds (ADR-0007)
# ---------------------------------------------------------------------------

#: Maximum input length considered. Longer inputs are truncated before
#: tokenising so a 10 000-char single token cannot trigger pathological
#: allocation or regex backtracking.
MAX_QUERY_LEN = 500

#: Maximum number of emitted tokens (quantities + package + text_tokens). A
#: repeated token stream is capped here so tokenisation stays bounded.
MAX_TOKENS = 10

# ---------------------------------------------------------------------------
# Unit-symbol -> predicate mapping (ADR-VOLT: bare "V" -> voltage_max)
# ---------------------------------------------------------------------------

# Mapping from a recognised trailing unit symbol to the promoted DQL predicate.
# Keys are matched case-sensitively where it matters (e.g. "Hz" before "H").
_SYMBOL_TO_PREDICATE: dict[str, str] = {
    "Ω": "resistance",
    "ohm": "resistance",
    "F": "capacitance",
    "H": "inductance",
    "A": "current_max",
    "W": "power",
    "Hz": "frequency_max",
    "%": "tolerance_pct",
    "V": "voltage_max",  # ADR-VOLT: a bare "V" suffix maps to voltage_max.
}

# Recognised unit symbols, longest first so multi-char symbols ("Hz", "ohm")
# are tried before single-char ones ("H", "Ω").
_UNIT_SYMBOLS_ORDERED: tuple[str, ...] = ("Hz", "ohm", "Ω", "F", "H", "A", "W", "%", "V")

# SI prefix characters that may sit between the magnitude and the unit symbol.
# Mirrors partgraph.normalize.units so magnitude and symbol agree.
_SI_PREFIX_CHARS = "pnuµμmkKMGT"

# A leading signed decimal magnitude.
_LEADING_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?")

# Range / condition separators that disqualify a token from being a scalar
# quantity (a range like "1V~5V" or a conditional like "2V@1mA").
_RANGE_SEP = "~"
_CONDITION_SEP = "@"

# A bare numeric package code: exactly four digits (0402/0603/0805/1206/...).
_NUMERIC_PACKAGE_RE = re.compile(r"^[0-9]{4}$")

# Known alphabetic package families. Matched case-insensitively against the
# uppercased token prefix; the catalogue uses forms like SOT-23, SOIC-16,
# QFN-32, TQFP-44, TSSOP-20, DIP-16, PDIP-16, SOD-123, DFN-8, BGA, LQFP-48.
_ALPHA_PACKAGE_PREFIXES: tuple[str, ...] = (
    "SOIC",
    "SOT",
    "SOD",
    "SON",
    "QFN",
    "DFN",
    "TQFP",
    "LQFP",
    "QFP",
    "TSSOP",
    "MSOP",
    "SSOP",
    "TSOP",
    "PDIP",
    "DIP",
    "SIP",
    "BGA",
    "LGA",
    "DPAK",
    "TO",
)

# Final validation applied to any candidate package before it is accepted
# (also enforced again at the DQL builder boundary). Uppercase alnum start,
# then up to 19 more uppercase-alnum or hyphen characters.
_PACKAGE_VALID_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,19}$")

# A token that is only punctuation / symbols (no letters or digits) is dropped.
_HAS_ALNUM_RE = re.compile(r"[A-Za-z0-9]")

# Minimum remainder length that can hold both an SI prefix and a unit symbol
# (e.g. the "nF" of "100nF"); below this only the bare-symbol case applies.
_PREFIXED_SYMBOL_MIN_LEN = 2


@dataclass(frozen=True)
class Quantity:
    """A single recognised numeric parameter extracted from the query.

    Attributes:
        predicate: The DQL predicate this quantity targets (e.g. ``"resistance"``).
        value: The SI-normalised magnitude (e.g. ``10000.0`` for ``"10k"``).
        raw: The original token as typed (e.g. ``"10k"``).
    """

    predicate: str
    value: float
    raw: str


@dataclass(frozen=True)
class ParsedQuery:
    """The structured result of parsing a free-text search string.

    Attributes:
        quantities: Recognised numeric parameters (may be empty).
        package: A single package code, or ``None`` when none was recognised.
        text_tokens: Remaining free-text tokens (e.g. an MPN).
        raw_query: The original, unmodified input string.
    """

    quantities: list[Quantity] = field(default_factory=list)
    package: str | None = None
    text_tokens: list[str] = field(default_factory=list)
    raw_query: str = ""


def _match_unit_symbol(token: str) -> str | None:
    """Return the recognised unit symbol that *token* ends with, or ``None``.

    The token must look like ``<number>[<si-prefix>]<symbol>`` where the symbol
    is one of the recognised unit symbols. A bare number (``"10000"``), or a
    number followed by an unrecognised letter (e.g. ``"5Z"``), returns ``None``.

    Resistor shorthand: a magnitude followed by *only* an SI-prefix character
    (``"10k"`` -> 10 kΩ, ``"2.2M"`` -> 2.2 MΩ) is treated as a resistance, so
    its implicit unit symbol is the ohm sign. This matches the pinned decision
    that ``"10k" -> resistance`` while ``"10000"`` (no suffix) stays text.
    """
    match = _LEADING_NUMBER_RE.match(token)
    if match is None:
        return None
    remainder = token[match.end():]
    if not remainder:
        return None

    # A lone SI-prefix suffix (no explicit unit symbol) is resistor shorthand.
    if len(remainder) == 1 and remainder in _SI_PREFIX_CHARS:
        return "Ω"

    # Drop a single leading SI prefix character if present, so that "10kΩ"
    # exposes the trailing "Ω" and "100nF" exposes the trailing "F".
    if len(remainder) >= _PREFIXED_SYMBOL_MIN_LEN and remainder[0] in _SI_PREFIX_CHARS:
        candidate = remainder[1:]
    else:
        candidate = remainder

    for symbol in _UNIT_SYMBOLS_ORDERED:
        if candidate == symbol:
            return symbol
    return None


def _try_quantity(token: str) -> Quantity | None:
    """Return a :class:`Quantity` for *token*, or ``None`` if it is not one.

    A quantity is only minted when the token carries a recognised unit symbol
    *and* ``units.parse`` yields a magnitude. Ranges and conditionals never
    produce a scalar quantity.
    """
    if _RANGE_SEP in token or _CONDITION_SEP in token:
        return None

    symbol = _match_unit_symbol(token)
    if symbol is None:
        return None

    predicate = _SYMBOL_TO_PREDICATE.get(symbol)
    if predicate is None:  # pragma: no cover — symbol set and map kept in sync
        return None

    value = units.parse(token)
    if value is None:
        return None

    return Quantity(predicate=predicate, value=float(value), raw=token)


def _try_package(token: str) -> str | None:
    """Return a normalised package code for *token*, or ``None``.

    Accepts a four-digit numeric package (``0402``) or a known alphabetic
    package family (``SOT-23``, ``SOIC-16``, ...). The result is uppercased and
    must satisfy the package validation regex; otherwise ``None`` is returned.
    """
    upper = token.upper()

    if _NUMERIC_PACKAGE_RE.match(upper):
        return upper if _PACKAGE_VALID_RE.match(upper) else None

    for prefix in _ALPHA_PACKAGE_PREFIXES:
        if upper.startswith(prefix):
            return upper if _PACKAGE_VALID_RE.match(upper) else None

    return None


def parse_query(s: str | None) -> ParsedQuery:
    """Parse a free-text component search string into a :class:`ParsedQuery`.

    Pure and total: never raises, never depends on locale or mutable state.

    Classification per whitespace-separated token, in order:
      1. Recognised quantity (number + known unit symbol) -> ``quantities``.
      2. Recognised package code (only the first wins) -> ``package``.
      3. Otherwise, if the token contains a letter or digit -> ``text_tokens``.
      4. Punctuation-only tokens are dropped.

    The input is truncated to ``MAX_QUERY_LEN`` characters and the total number
    of emitted tokens is capped at ``MAX_TOKENS`` (ADR-0007 DoS bounds).
    """
    raw_query = s if s is not None else ""

    # Truncate before tokenising to bound work on hostile/huge input.
    work = raw_query[:MAX_QUERY_LEN]

    quantities: list[Quantity] = []
    text_tokens: list[str] = []
    package: str | None = None

    for token in work.split():
        # Stop emitting once the DoS token cap is reached.
        if len(quantities) + len(text_tokens) + (1 if package else 0) >= MAX_TOKENS:
            break

        quantity = _try_quantity(token)
        if quantity is not None:
            quantities.append(quantity)
            continue

        if package is None:
            candidate_pkg = _try_package(token)
            if candidate_pkg is not None:
                package = candidate_pkg
                continue

        # Drop punctuation-only tokens; keep anything with a letter or digit.
        if _HAS_ALNUM_RE.search(token):
            text_tokens.append(token)

    return ParsedQuery(
        quantities=quantities,
        package=package,
        text_tokens=text_tokens,
        raw_query=raw_query,
    )
