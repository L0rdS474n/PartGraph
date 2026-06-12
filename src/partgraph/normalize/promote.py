"""Attribute enrichment and conservative SI promotion for the normalize stage.

This module is the single source of truth for two related, deterministic
operations applied to every :class:`~partgraph.normalize.model.AttrRecord`:

1. **Enrichment** — parse ``value_text`` into a base-SI ``value_num`` and assign
   a ``unit`` symbol, deriving extra (``min``/``max`` or per-token) records for
   ranges and multi-value strings. See :func:`enrich_attributes`.
2. **Promotion** — copy a *conservative* subset of enriched scalar attributes
   into the part's ``promoted`` map under stable predicate names. See
   :func:`promote`.

Both operations are pure functions of their inputs (no clock, no RNG, no mutable
module state) so the normalize stage stays byte-reproducible.

Routing (per attribute, applied to the ORIGINAL ``value_text``):

- ``"~"`` present  -> **range**: keep the original record (``value_num`` null)
  and append ``name + " (min)"`` / ``name + " (max)"`` derived records carrying
  the parsed bounds.
- otherwise ``";"`` splitting into >1 parsable tokens -> **multi-value**: keep
  the original (``value_num`` null) and append one derived record per token
  (``value_text`` = token, with its own ``value_num``), in source order.
- otherwise -> **scalar** (this also covers ``value@condition``, where
  :func:`~partgraph.normalize.units.parse` returns the principal value): set
  ``value_num`` / ``unit`` on the original record in place.
- non-parsable -> leave ``value_num`` / ``unit`` null, retain ``value_text``,
  emit no derived records.

Derived records are produced ONLY for tokens that actually parse. Ordering is
deterministic: the original first, then ``[min, max]`` for a range or the tokens
in source order for a multi-value string.
"""

from __future__ import annotations

import re

from partgraph.normalize.model import AttrRecord
from partgraph.normalize.units import parse, parse_range

__all__ = [
    "DIMENSION_UNIT",
    "PROMOTION_LEXICON",
    "enrich_attributes",
    "normalize_attr_name",
    "promote",
]

# ---------------------------------------------------------------------------
# Name normalization (MUST match tests/integration/test_promote_realdata.py)
# ---------------------------------------------------------------------------

_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")
_WS_RE = re.compile(r"\s+")


def normalize_attr_name(raw: str) -> str:
    """Lowercase, strip a parenthetical ``(...)`` suffix, collapse whitespace.

    Mirrors the helper pinned by the real-data integration test so a name maps
    to the same lexicon key here and there. ``"Overload Voltage (Max)"`` ->
    ``"overload voltage"``; ``"Power(Watts)"`` -> ``"power"``.
    """
    stripped = _PAREN_RE.sub(" ", raw).strip().lower()
    return _WS_RE.sub(" ", stripped)


# ---------------------------------------------------------------------------
# Dimension -> SI base symbol (the unit assigned during enrichment)
# ---------------------------------------------------------------------------

# Each physical dimension maps to exactly one SI base symbol. A name that maps
# to no dimension (temperature, dB, ppm, ...) receives a ``null`` unit.
DIMENSION_UNIT: dict[str, str] = {
    "resistance": "Ω",
    "capacitance": "F",
    "inductance": "H",
    "voltage": "V",
    "current": "A",
    "power": "W",
    "frequency": "Hz",
    "tolerance": "%",
}

# Normalized attribute name -> physical dimension, used to pick the unit symbol
# during enrichment. This is intentionally broader than the promotion lexicon:
# enrichment assigns a unit to a known dimension even when the name is too
# ambiguous to promote (e.g. "supply voltage" -> V unit, but never promoted).
_NAME_DIMENSION: dict[str, str] = {
    # resistance
    "resistance": "resistance",
    # capacitance
    "capacitance": "capacitance",
    # inductance
    "inductance": "inductance",
    # power
    "power": "power",
    "power dissipation": "power",
    # tolerance
    "tolerance": "tolerance",
    # frequency
    "frequency": "frequency",
    "clock frequency": "frequency",
    # current (incl. ambiguous names that still get an Amp unit)
    "current": "current",
    "output current": "current",
    "supply current": "current",
    "standby current": "current",
    # voltage (incl. ambiguous names that still get a Volt unit but never promote)
    "voltage": "voltage",
    "supply voltage": "voltage",
    "output voltage": "voltage",
    "input voltage": "voltage",
    "operating voltage": "voltage",
    "overload voltage": "voltage",
    "maximum input voltage": "voltage",
    "minimum input voltage": "voltage",
    "max voltage": "voltage",
    "min voltage": "voltage",
}


def _unit_for(normalized_name: str) -> str | None:
    """Return the SI base symbol for *normalized_name*, or ``None``."""
    dim = _NAME_DIMENSION.get(normalized_name)
    if dim is None:
        return None
    return DIMENSION_UNIT.get(dim)


# ---------------------------------------------------------------------------
# Promotion lexicon (conservative; normalized-name -> promoted predicate)
# ---------------------------------------------------------------------------

# Promote ONLY when the enriched ``value_num`` is not None AND the normalized
# name matches a key here. The first matching attribute (in record order) wins
# for each predicate. Ambiguous single-value voltage names ("supply voltage",
# "output voltage", "voltage", "input voltage", "operating voltage") are
# deliberately ABSENT: a lone ambiguous voltage must never be fabricated into a
# min/max bound.
PROMOTION_LEXICON: dict[str, str] = {
    "resistance": "resistance",
    "capacitance": "capacitance",
    "inductance": "inductance",
    "power": "power",
    "power dissipation": "power",
    "tolerance": "tolerance_pct",
    "frequency": "frequency_max",
    "clock frequency": "frequency_max",
    "output current": "current_max",
    "supply current": "current_max",
    "standby current": "current_max",
    "current": "current_max",
    "overload voltage": "voltage_max",
    "maximum input voltage": "voltage_max",
    "max voltage": "voltage_max",
    "minimum input voltage": "voltage_min",
    "min voltage": "voltage_min",
}

# Multi-value split separator.
_MULTI_SEP = ";"


def enrich_attributes(attributes: list[AttrRecord]) -> list[AttrRecord]:
    """Return a new attribute list with enrichment and derived records applied.

    The order is deterministic: for each input record, the (possibly enriched)
    original is emitted first, immediately followed by any derived records it
    produced, before moving on to the next input record.
    """
    out: list[AttrRecord] = []
    for attr in attributes:
        out.extend(_enrich_one(attr))
    return out


def _enrich_one(attr: AttrRecord) -> list[AttrRecord]:
    """Enrich a single record, returning the original plus any derived records."""
    text = attr.value_text
    if text is None:
        # Nothing to parse; pass through unchanged (no fabricated value_num).
        return [AttrRecord(name=attr.name, value_text=None, value_num=None, unit=None)]

    normalized = normalize_attr_name(attr.name)
    unit = _unit_for(normalized)

    # --- range: "~" present -------------------------------------------------
    if "~" in text:
        original = AttrRecord(name=attr.name, value_text=text, value_num=None, unit=None)
        rng = parse_range(text)
        if rng is None:
            return [original]
        lo, hi = rng
        return [
            original,
            AttrRecord(
                name=f"{attr.name} (min)", value_text=text, value_num=lo, unit=unit
            ),
            AttrRecord(
                name=f"{attr.name} (max)", value_text=text, value_num=hi, unit=unit
            ),
        ]

    # --- multi-value: ";" splitting into >1 parsable tokens -----------------
    if _MULTI_SEP in text:
        raw_tokens = [t.strip() for t in text.split(_MULTI_SEP)]
        parsed = [(tok, parse(tok)) for tok in raw_tokens if tok]
        parsable = [(tok, num) for tok, num in parsed if num is not None]
        if len(parsable) > 1:
            original = AttrRecord(
                name=attr.name, value_text=text, value_num=None, unit=None
            )
            derived = [
                AttrRecord(
                    name=attr.name, value_text=tok, value_num=num, unit=unit
                )
                for tok, num in parsable
            ]
            return [original, *derived]
        # Fall through: 0 or 1 parsable tokens -> treat as a scalar string.

    # --- scalar (incl. value@condition principal) ---------------------------
    value_num = parse(text)
    if value_num is None:
        # Non-parsable: retain text, no value_num/unit, no derived records.
        return [AttrRecord(name=attr.name, value_text=text, value_num=None, unit=None)]
    return [
        AttrRecord(name=attr.name, value_text=text, value_num=value_num, unit=unit)
    ]


def promote(attributes: list[AttrRecord]) -> dict[str, float]:
    """Return the conservative ``promoted`` map for an enriched attribute list.

    Only scalar records with a non-None ``value_num`` whose normalized name is a
    promotion-lexicon key contribute. For each predicate the first matching
    record (in list order) wins. Range / multi-value ORIGINALS carry a None
    ``value_num`` and therefore never promote through their bare name; their
    derived ``(min)``/``(max)`` records carry a parenthetical suffix that the
    name normalizer strips, but those names are not lexicon keys (e.g.
    "operating temperature") so they do not promote either — by design.
    """
    promoted: dict[str, float] = {}
    for attr in attributes:
        if attr.value_num is None:
            continue
        key = PROMOTION_LEXICON.get(normalize_attr_name(attr.name))
        if key is not None and key not in promoted:
            promoted[key] = attr.value_num
    return promoted
