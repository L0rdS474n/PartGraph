"""
Tests: AC-D1 (Defect 1 — attribute enrichment + SI promotion) — real-data gate

@pytest.mark.integration — requires data/raw/jlcpcb-components.sqlite3.
Tests are SKIPPED when the file is absent.

Purpose: anti-proxy gate that proves the enrichment pipeline achieves
meaningful numeric extraction and promotion rates on real JLCPCB data.

Sampling:
  - Deterministic prefix: the FIRST 4000 parts from JlcpartsAdapter.iter_parts()
    (no random seed needed; the adapter already iterates in a deterministic order).
  - normalize() writes all 4000 parts to a tmp JSONL; records are read back.
  - NOTE: the prefix is 4000 (not 2000) because the documented golden-case 30kΩ
    resistor first appears at iteration index 3383 in this dataset; a 2000-part
    window does not contain it. The pinned anti-proxy thresholds are unchanged.

Assertions:
  (a) Among AttrRecords whose value_text matches the unit-token pre-filter
      (reuses the regex from test_unit_realdata.py, stripping leading ± first),
      fraction with value_num not None >= 0.80.
  (b) Among parts that HAVE >=1 promotable attribute (lexicon-key name AND
      enriched value_num not None), fraction with non-empty promoted >= 0.95.
  (c) Golden cases present in the sample:
      - A "30kΩ" resistance → promoted["resistance"] ≈ 30000.0
      - A "100mW" power     → promoted["power"] ≈ 0.1
      - An "Overload Voltage (Max)"="75V" → promoted["voltage_max"] ≈ 75.0
  (d) Both fractions and sample size are PRINTED to stderr.

EXPECTED STATE: this test FAILS/ERRORS against the current (unenriched) code
because fractions will be 0.00.  It turns green only after Defect 1 is fixed.
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections.abc import Iterator

import pytest

# ---------------------------------------------------------------------------
# Paths (mirrors test_adapter_realfile.py convention)
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_REAL_DB = _REPO_ROOT / "data" / "raw" / "jlcpcb-components.sqlite3"

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration

_SKIP_REAL = pytest.mark.skipif(
    not _REAL_DB.exists(),
    reason=(
        f"Real JLCPCB data file absent: {_REAL_DB}. "
        "Run `partgraph ingest jlcparts --fetch` to download it (~1.6 GB). "
        "Skipping real-data enrichment integration tests."
    ),
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Deterministic-prefix sample size. Set to 4000 so the documented golden 30kΩ
# resistor (first appears at iteration index 3383 in this dataset) is included;
# a 2000-part window does not contain it. Thresholds are unchanged.
_SAMPLE_PARTS = 4000

# Unit-token pre-filter (same as test_unit_realdata.py).
# A value_text is included in the enrichment denominator only when it
# matches this regex (after stripping a leading ± character).
_UNIT_TOKEN_RE = re.compile(r"\d+\s*[pnuµmkKMGT]?[VΩFHzAW%]")

# Promotion lexicon keys (lowercase, parenthetical stripped, whitespace collapsed).
# Names in this set are expected to produce a promoted entry when value_num != None.
# "EXPLICITLY NOT promoted" list from the spec is excluded.
_PROMOTION_LEXICON: dict[str, str] = {
    "resistance": "resistance",
    "capacitance": "capacitance",
    "inductance": "inductance",
    "power": "power",
    "power dissipation": "power",
    "power (watts)": "power",
    "tolerance": "tolerance_pct",
    "frequency": "frequency_max",
    "clock frequency": "frequency_max",
    "output current": "current_max",
    "supply current": "current_max",
    "standby current": "current_max",
    "current": "current_max",
    "overload voltage (max)": "voltage_max",
    "maximum input voltage": "voltage_max",
    "max voltage": "voltage_max",
    "minimum input voltage": "voltage_min",
    "min voltage": "voltage_min",
}

_THRESHOLDS = {
    "enrichment_fraction": 0.80,
    "promotion_fraction": 0.95,
}


def _normalize_attr_name(raw: str) -> str:
    """Lowercase, strip parenthetical suffixes, collapse whitespace."""
    # strip parenthetical (...) before comparing to lexicon
    stripped = re.sub(r"\s*\([^)]*\)\s*", " ", raw).strip().lower()
    return re.sub(r"\s+", " ", stripped)


def _is_promotable_name(raw_name: str) -> bool:
    """Return True if the attribute name matches a promotion lexicon key."""
    return _normalize_attr_name(raw_name) in _PROMOTION_LEXICON


def _has_unit_token(value_text: str | None) -> bool:
    """Return True if value_text matches the unit pre-filter (after ± strip)."""
    if not value_text:
        return False
    stripped = value_text.lstrip("±")
    return bool(_UNIT_TOKEN_RE.search(stripped))


class _LimitedAdapter:
    """Wraps an adapter and yields only the first N parts (deterministic prefix)."""

    def __init__(self, inner: object, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    def iter_parts(self) -> Iterator:
        for count, part in enumerate(self._inner.iter_parts()):
            if count >= self._limit:
                break
            yield part


# ---------------------------------------------------------------------------
# Main integration test
# ---------------------------------------------------------------------------

@_SKIP_REAL
def test_promote_realdata_enrichment_and_promotion_rates(
    tmp_path: pathlib.Path,
) -> None:
    """Given the real JLCPCB SQLite, first 2000 parts through normalize().
    When the enriched JSONL is read back.
    Then:
    (a) >= 80% of unit-bearing AttrRecords have value_num set.
    (b) >= 95% of parts with a promotable attr (name in lexicon + value_num) have
        non-empty promoted dict.
    (c) Golden cases are present: 30kΩ resistance, 100mW power,
        Overload Voltage (Max)=75V promoted to voltage_max.

    EXPECTED RED against the current (unenriched) code.
    """
    from partgraph.normalize.model import StagedPart
    from partgraph.normalize.run import normalize
    from partgraph.sources.jlcparts import JlcpartsAdapter, open_jlcparts_db

    conn = open_jlcparts_db(_REAL_DB)
    try:
        raw_adapter = JlcpartsAdapter(conn)
        limited = _LimitedAdapter(raw_adapter, _SAMPLE_PARTS)

        out = tmp_path / "realdata_staged.jsonl"
        written = normalize(
            adapter=limited,
            source_ref="jlcparts@test",
            output_path=out,
        )
    finally:
        conn.close()

    # Read back all staged records
    parts: list[StagedPart] = []
    for raw_line in out.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped:
            parts.append(StagedPart.from_json(stripped))

    sample_size = len(parts)

    # ------------------------------------------------------------------
    # (a) Enrichment fraction: unit-bearing AttrRecords with value_num set
    # ------------------------------------------------------------------
    unit_bearing_total = 0
    unit_bearing_enriched = 0
    for part in parts:
        for attr in part.attributes:
            if _has_unit_token(attr.value_text):
                unit_bearing_total += 1
                if attr.value_num is not None:
                    unit_bearing_enriched += 1

    enrichment_fraction = (
        unit_bearing_enriched / unit_bearing_total if unit_bearing_total > 0 else 0.0
    )

    # ------------------------------------------------------------------
    # (b) Promotion fraction: parts with >= 1 promotable enriched attr
    #     that have non-empty promoted dict
    # ------------------------------------------------------------------
    parts_with_promotable_attr = 0
    parts_with_non_empty_promoted = 0
    for part in parts:
        has_promotable = any(
            _is_promotable_name(a.name) and a.value_num is not None
            for a in part.attributes
        )
        if has_promotable:
            parts_with_promotable_attr += 1
            if part.promoted:
                parts_with_non_empty_promoted += 1

    promotion_fraction = (
        parts_with_non_empty_promoted / parts_with_promotable_attr
        if parts_with_promotable_attr > 0
        else 0.0
    )

    # ------------------------------------------------------------------
    # (d) Print diagnostics to stderr
    # ------------------------------------------------------------------
    print(
        f"\n[AC-D1-realdata] Sample parts: {sample_size}, "
        f"Unit-bearing attrs: {unit_bearing_total}, "
        f"Enriched: {unit_bearing_enriched}, "
        f"Enrichment fraction: {enrichment_fraction:.1%} "
        f"(threshold: {_THRESHOLDS['enrichment_fraction']:.0%})",
        file=sys.stderr,
    )
    print(
        f"[AC-D1-realdata] Parts with promotable attr: {parts_with_promotable_attr}, "
        f"With non-empty promoted: {parts_with_non_empty_promoted}, "
        f"Promotion fraction: {promotion_fraction:.1%} "
        f"(threshold: {_THRESHOLDS['promotion_fraction']:.0%})",
        file=sys.stderr,
    )

    # ------------------------------------------------------------------
    # (c) Golden cases
    # ------------------------------------------------------------------
    # Golden case 1: some part with resistance ≈ 30000.0 Ω
    resistance_30k_parts = [
        p for p in parts
        if abs(p.promoted.get("resistance", 0.0) - 30_000.0) < 1.0
    ]

    # Golden case 2: some part with power ≈ 0.1 W (100mW)
    power_100mw_parts = [
        p for p in parts
        if abs(p.promoted.get("power", -1.0) - 0.1) < 0.001
    ]

    # Golden case 3: some part with overload voltage_max ≈ 75.0 V
    # We look for an attr named "Overload Voltage (Max)" with value "75V"
    # that caused promoted["voltage_max"] ≈ 75.0
    # NOTE: compare against "overload voltage" (parenthetical stripped). The
    # original literal "overload voltage (max)" was unsatisfiable because
    # _normalize_attr_name() removes the "(Max)" suffix, so the predicate could
    # never match. The intended part — "Overload Voltage (Max)" = "75V" promoting
    # to voltage_max ≈ 75.0 — is what we assert here.
    overvolt_parts = [
        p for p in parts
        if abs(p.promoted.get("voltage_max", -1.0) - 75.0) < 0.5
        and any(
            _normalize_attr_name(a.name) == "overload voltage"
            and a.value_text is not None and "75" in a.value_text
            for a in p.attributes
        )
    ]

    print(
        f"[AC-D1-realdata] Golden-30kΩ: {len(resistance_30k_parts)}, "
        f"Golden-100mW: {len(power_100mw_parts)}, "
        f"Golden-OVmax75V: {len(overvolt_parts)}",
        file=sys.stderr,
    )

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------
    assert unit_bearing_total > 0, (
        "No unit-bearing AttrRecords found in sample; the adapter may not be "
        "populating attributes at all."
    )

    assert enrichment_fraction >= _THRESHOLDS["enrichment_fraction"], (
        f"Enrichment fraction {enrichment_fraction:.1%} is below threshold "
        f"{_THRESHOLDS['enrichment_fraction']:.0%}. "
        f"unit-bearing attrs: {unit_bearing_total}, enriched: {unit_bearing_enriched}. "
        "Defect 1 is not fixed: normalize() must parse value_text and set value_num."
    )

    assert parts_with_promotable_attr > 0, (
        "No parts with promotable attributes found in the 2000-part sample."
    )

    assert promotion_fraction >= _THRESHOLDS["promotion_fraction"], (
        f"Promotion fraction {promotion_fraction:.1%} is below threshold "
        f"{_THRESHOLDS['promotion_fraction']:.0%}. "
        f"Parts with promotable attr: {parts_with_promotable_attr}, "
        f"with promoted: {parts_with_non_empty_promoted}. "
        "Defect 1 fix must populate StagedPart.promoted from enriched value_num."
    )

    assert resistance_30k_parts, (
        "Golden case MISSING: expected at least one part with promoted['resistance'] ≈ 30000.0 "
        "(a 30kΩ resistor). Either the sample lacks this part or enrichment is not working."
    )

    assert power_100mw_parts, (
        "Golden case MISSING: expected at least one part with promoted['power'] ≈ 0.1 "
        "(a 100mW power rating). Either the sample lacks this part or enrichment is not working."
    )

    assert overvolt_parts, (
        "Golden case MISSING: expected at least one part with "
        "attr 'Overload Voltage (Max)' = '75V' and promoted['voltage_max'] ≈ 75.0. "
        "Either the sample lacks this part or voltage_max promotion is not working."
    )
