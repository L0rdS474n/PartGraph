"""
Tests: AC-D1-* (Defect 1 — attribute enrichment + SI promotion)

Verifies that partgraph.normalize.run.normalize() enriches AttrRecord fields
(value_num, unit) and populates the StagedPart.promoted map according to the
pinned lexicon.

ALL tests in this module are EXPECTED RED against the current (un-enriched)
production code.  They turn green only after Defect 1 is fixed.

Coverage map
-----------
AC-D1-1  scalar resistance "30kΩ"  → value_num=30000.0, unit="Ω", promoted["resistance"]=30000.0
AC-D1-2  scalar power "100mW"      → value_num=0.1,     unit="W", promoted["power"]=0.1
AC-D1-3  tolerance "±1%"           → value_num=1.0,     unit="%", promoted["tolerance_pct"]=1.0
AC-D1-4  multi-value split "1.8V;2.5V;3.3V" → original kept (value_num null) + 3 derived records
AC-D1-5  range min/max derived "-55℃~+155℃" → original kept + (name+" (min)", name+" (max)")
AC-D1-6  value@condition "56dB@(120Hz)" → principal value_num=56.0, unit=null (dB not in lexicon)
AC-D1-7  non-parsable retained, no derived records ("Thick Film Resistors", "-")
AC-D1-8  promote voltage_max from "Overload Voltage (Max)"=75V
AC-D1-9  promote current_max from "Output Current"
AC-D1-10 ambiguous "Output Voltage"=10V NOT promoted to voltage_min/max
AC-D1-11 unknown attribute name "Mounting Style" not promoted
AC-D1-12 determinism: two normalize() calls over enrichable attrs are byte-identical
AC-D1-13 multi-value of an ambiguous name: derived records are NOT promoted
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

from partgraph.normalize.model import AttrRecord, StagedPart
from partgraph.normalize.run import normalize


# ---------------------------------------------------------------------------
# Minimal helpers
# ---------------------------------------------------------------------------

def _make_base(
    lcsc_id: str,
    *,
    mpn: str = "TESTMPN",
    mfr_norm: str = "MFR",
    attributes: list[AttrRecord] | None = None,
) -> StagedPart:
    """Return a minimal StagedPart with the given attribute list."""
    mpn_norm = mpn.upper().replace("-", "").replace(" ", "")
    return StagedPart(
        mpn=mpn,
        mpn_norm=mpn_norm,
        mfr_name="TestMfr",
        mfr_norm=mfr_norm,
        xid=f"{mpn_norm}|{mfr_norm}",
        description="Test part",
        package="0402",
        category="Passive",
        subcategory="Resistors",
        datasheet_url=None,
        lcsc_id=lcsc_id,
        stock=10,
        price_usd=0.01,
        is_basic=False,
        promoted={},
        attributes=attributes or [],
        tags=[],
        source_ref="",
    )


class _FakeAdapter:
    """Minimal adapter that yields a pre-seeded list of StagedParts."""

    def __init__(self, parts: list[StagedPart]) -> None:
        self._parts = parts

    def iter_parts(self) -> Iterator[StagedPart]:
        yield from self._parts


def _run_normalize(parts: list[StagedPart], tmp_path: pathlib.Path) -> list[StagedPart]:
    """Run normalize() and return the parsed StagedPart records."""
    out = tmp_path / "staged.jsonl"
    normalize(
        adapter=_FakeAdapter(parts),
        source_ref="test@2026-06-12",
        output_path=out,
    )
    result: list[StagedPart] = []
    for raw_line in out.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped:
            result.append(StagedPart.from_json(stripped))
    return result


def _find_part(parts: list[StagedPart], lcsc_id: str) -> StagedPart:
    for p in parts:
        if p.lcsc_id == lcsc_id:
            return p
    raise KeyError(f"lcsc_id={lcsc_id!r} not found in output")


# ---------------------------------------------------------------------------
# AC-D1-1: scalar resistance → value_num + unit + promoted["resistance"]
# ---------------------------------------------------------------------------

def test_ac_d1_1_scalar_resistance_enriched(tmp_path: pathlib.Path) -> None:
    """Given a part with attribute name "Resistance", value_text="30kΩ".
    When normalize() is called.
    Then the AttrRecord has value_num≈30000.0, unit="Ω",
    and StagedPart.promoted["resistance"]≈30000.0.
    """
    parts = [_make_base("C1001", attributes=[
        AttrRecord(name="Resistance", value_text="30kΩ"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1001")

    # Find the base AttrRecord for "Resistance" / "30kΩ"
    attrs = [a for a in part.attributes if a.name == "Resistance" and a.value_text == "30kΩ"]
    assert attrs, f"No Resistance/30kΩ AttrRecord in output; attrs={part.attributes!r}"
    attr = attrs[0]
    assert attr.value_num == pytest.approx(30_000.0, rel=1e-6), (
        f"Expected value_num≈30000.0, got {attr.value_num!r}"
    )
    assert attr.unit == "Ω", f"Expected unit='Ω', got {attr.unit!r}"
    assert "resistance" in part.promoted, (
        f"'resistance' not promoted; promoted={part.promoted!r}"
    )
    assert part.promoted["resistance"] == pytest.approx(30_000.0, rel=1e-6), (
        f"promoted['resistance']≈30000.0 expected, got {part.promoted['resistance']!r}"
    )


# ---------------------------------------------------------------------------
# AC-D1-2: scalar power "100mW" → 0.1 W + promoted["power"]
# ---------------------------------------------------------------------------

def test_ac_d1_2_scalar_power_enriched(tmp_path: pathlib.Path) -> None:
    """Given attribute name "Power Dissipation", value_text="100mW".
    When normalize() is called.
    Then value_num≈0.1, unit="W", promoted["power"]≈0.1.
    """
    parts = [_make_base("C1002", attributes=[
        AttrRecord(name="Power Dissipation", value_text="100mW"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1002")

    attrs = [a for a in part.attributes
             if a.name == "Power Dissipation" and a.value_text == "100mW"]
    assert attrs, f"No Power Dissipation/100mW attr; attrs={part.attributes!r}"
    attr = attrs[0]
    assert attr.value_num == pytest.approx(0.1, rel=1e-6), (
        f"Expected value_num≈0.1, got {attr.value_num!r}"
    )
    assert attr.unit == "W", f"Expected unit='W', got {attr.unit!r}"
    assert part.promoted.get("power") == pytest.approx(0.1, rel=1e-6), (
        f"promoted['power']≈0.1 expected, got {part.promoted!r}"
    )


# ---------------------------------------------------------------------------
# AC-D1-3: tolerance "±1%" → value_num=1.0, unit="%", promoted["tolerance_pct"]
# ---------------------------------------------------------------------------

def test_ac_d1_3_tolerance_pm_enriched(tmp_path: pathlib.Path) -> None:
    """Given attribute name "Tolerance", value_text="±1%".
    When normalize() is called.
    Then value_num≈1.0, unit="%", promoted["tolerance_pct"]≈1.0.
    Parser MUST strip the leading ± before extracting the numeric part.
    """
    parts = [_make_base("C1003", attributes=[
        AttrRecord(name="Tolerance", value_text="±1%"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1003")

    attrs = [a for a in part.attributes
             if a.name == "Tolerance" and a.value_text == "±1%"]
    assert attrs, f"No Tolerance/±1% attr; attrs={part.attributes!r}"
    attr = attrs[0]
    assert attr.value_num == pytest.approx(1.0, rel=1e-6), (
        f"Expected value_num≈1.0 after ± strip, got {attr.value_num!r}"
    )
    assert attr.unit == "%", f"Expected unit='%', got {attr.unit!r}"
    assert part.promoted.get("tolerance_pct") == pytest.approx(1.0, rel=1e-6), (
        f"promoted['tolerance_pct'] expected 1.0, got {part.promoted!r}"
    )


# ---------------------------------------------------------------------------
# AC-D1-4: multi-value split "1.8V;2.5V;3.3V"
# ---------------------------------------------------------------------------

def test_ac_d1_4_multi_value_split(tmp_path: pathlib.Path) -> None:
    """Given attribute name "Supply Voltage", value_text="1.8V;2.5V;3.3V".
    When normalize() is called.
    Then the ORIGINAL AttrRecord is kept with value_num=null, full text intact,
    PLUS three derived AttrRecords (value_text="1.8V", "2.5V", "3.3V") appended
    in source order after the original.
    Total attribute count for that name must be ≥ 4 (original + 3 derived).
    Derived records must have value_num set (≥1.0 for "1.8V" → 1.8).
    """
    parts = [_make_base("C1004", attributes=[
        AttrRecord(name="Supply Voltage", value_text="1.8V;2.5V;3.3V"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1004")

    supply_attrs = [a for a in part.attributes if a.name == "Supply Voltage"]
    assert len(supply_attrs) >= 4, (
        f"Expected ≥4 Supply Voltage attrs (original+3 derived), got {len(supply_attrs)}: "
        f"{supply_attrs!r}"
    )

    # Original must be first and have value_num=null
    original = supply_attrs[0]
    assert original.value_text == "1.8V;2.5V;3.3V", (
        f"First record must keep the original text, got {original.value_text!r}"
    )
    assert original.value_num is None, (
        f"Original multi-value record must have value_num=null, got {original.value_num!r}"
    )

    # The three derived records follow in order
    derived = supply_attrs[1:4]
    expected_texts = ["1.8V", "2.5V", "3.3V"]
    derived_texts = [a.value_text for a in derived]
    assert derived_texts == expected_texts, (
        f"Derived records must appear in source order {expected_texts}, got {derived_texts!r}"
    )

    # Each derived record must have value_num set
    for a in derived:
        assert a.value_num is not None, (
            f"Derived record {a.value_text!r} must have value_num set, got None"
        )


# ---------------------------------------------------------------------------
# AC-D1-5: range min/max derived "-55℃~+155℃"
# ---------------------------------------------------------------------------

def test_ac_d1_5_range_min_max_derived(tmp_path: pathlib.Path) -> None:
    """Given attribute name "Operating Temperature", value_text="-55℃~+155℃".
    When normalize() is called.
    Then:
    - Original AttrRecord kept first (value_num=null).
    - "Operating Temperature (min)" derived with value_num≈-55.0.
    - "Operating Temperature (max)" derived with value_num≈155.0.
    - unit=null for temperature (not in SI promotion lexicon).
    """
    parts = [_make_base("C1005", attributes=[
        AttrRecord(name="Operating Temperature", value_text="-55" + chr(8451) + "~+155" + chr(8451)),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1005")

    original_attrs = [
        a for a in part.attributes if a.name == "Operating Temperature"
    ]
    assert original_attrs, "Original Operating Temperature AttrRecord must be present"
    original = original_attrs[0]
    assert original.value_num is None, (
        f"Original range record must have value_num=null, got {original.value_num!r}"
    )

    min_attrs = [a for a in part.attributes if a.name == "Operating Temperature (min)"]
    max_attrs = [a for a in part.attributes if a.name == "Operating Temperature (max)"]
    assert min_attrs, "Operating Temperature (min) derived record must exist"
    assert max_attrs, "Operating Temperature (max) derived record must exist"

    assert min_attrs[0].value_num == pytest.approx(-55.0, rel=1e-6), (
        f"(min) value_num expected≈-55.0, got {min_attrs[0].value_num!r}"
    )
    assert max_attrs[0].value_num == pytest.approx(155.0, rel=1e-6), (
        f"(max) value_num expected≈155.0, got {max_attrs[0].value_num!r}"
    )
    # Temperature is not in the SI lexicon → unit must be null
    assert min_attrs[0].unit is None, (
        f"Temperature unit must be null (not in lexicon), got {min_attrs[0].unit!r}"
    )
    assert max_attrs[0].unit is None, (
        f"Temperature unit must be null (not in lexicon), got {max_attrs[0].unit!r}"
    )


# ---------------------------------------------------------------------------
# AC-D1-6: value@condition "56dB@(120Hz)" → principal value_num, unit=null
# ---------------------------------------------------------------------------

def test_ac_d1_6_value_at_condition_principal(tmp_path: pathlib.Path) -> None:
    """Given attribute name "PSRR", value_text="56dB@(120Hz)".
    When normalize() is called.
    Then value_num≈56.0 (the left-hand / principal value) and unit=null
    (dB is not in the SI promotion lexicon so no unit is assigned).
    No derived records are produced for value@condition format.
    """
    parts = [_make_base("C1006", attributes=[
        AttrRecord(name="PSRR", value_text="56dB@(120Hz)"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1006")

    psrr_attrs = [a for a in part.attributes if a.name == "PSRR"]
    assert psrr_attrs, "PSRR AttrRecord must be present"
    attr = psrr_attrs[0]
    assert attr.value_text == "56dB@(120Hz)", (
        f"value_text must be preserved unchanged, got {attr.value_text!r}"
    )
    assert attr.value_num == pytest.approx(56.0, rel=1e-6), (
        f"value@condition must yield principal value 56.0, got {attr.value_num!r}"
    )
    # dB is not in the lexicon → unit null
    assert attr.unit is None, (
        f"dB is not in the SI lexicon; unit must be null, got {attr.unit!r}"
    )
    # Must be exactly one record (no derived records from @condition)
    assert len(psrr_attrs) == 1, (
        f"value@condition must not produce derived records; got {len(psrr_attrs)} attrs"
    )


# ---------------------------------------------------------------------------
# AC-D1-7: non-parsable: "Thick Film Resistors" and "-" → retained, no derived
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,text", [
    ("Composition", "Thick Film Resistors"),
    ("Temperature Coefficient", "-"),
])
def test_ac_d1_7_non_parsable_retained_no_derived(
    name: str, text: str, tmp_path: pathlib.Path
) -> None:
    """Given an attribute whose value_text has no parseable numeric quantity.
    When normalize() is called.
    Then value_num=null, value_text is retained unchanged, and no derived
    records are appended.
    """
    parts = [_make_base("C1007", mpn=f"NP{name[:4].upper()}", attributes=[
        AttrRecord(name=name, value_text=text),
    ])]
    result = _run_normalize(parts, tmp_path)
    # There is exactly one part; find it by its unique xid
    assert len(result) == 1
    part = result[0]

    matching = [a for a in part.attributes if a.name == name]
    assert len(matching) == 1, (
        f"Non-parsable attr '{name}' must produce exactly 1 record (no derived), "
        f"got {matching!r}"
    )
    attr = matching[0]
    assert attr.value_text == text, (
        f"value_text must be retained unchanged; got {attr.value_text!r}"
    )
    assert attr.value_num is None, (
        f"Non-parsable value_text must have value_num=null; got {attr.value_num!r}"
    )


# ---------------------------------------------------------------------------
# AC-D1-8: promote voltage_max from "Overload Voltage (Max)"=75V
# ---------------------------------------------------------------------------

def test_ac_d1_8_promote_voltage_max_overload(tmp_path: pathlib.Path) -> None:
    """Given attribute name "Overload Voltage (Max)", value_text="75V".
    When normalize() is called.
    Then value_num≈75.0, unit="V", and promoted["voltage_max"]≈75.0.
    """
    parts = [_make_base("C1008", attributes=[
        AttrRecord(name="Overload Voltage (Max)", value_text="75V"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1008")

    attrs = [a for a in part.attributes if a.name == "Overload Voltage (Max)"]
    assert attrs, f"Overload Voltage (Max) attr missing; attrs={part.attributes!r}"
    attr = attrs[0]
    assert attr.value_num == pytest.approx(75.0, rel=1e-6), (
        f"Expected value_num≈75.0, got {attr.value_num!r}"
    )
    assert attr.unit == "V", f"Expected unit='V', got {attr.unit!r}"
    assert part.promoted.get("voltage_max") == pytest.approx(75.0, rel=1e-6), (
        f"promoted['voltage_max'] expected 75.0, got {part.promoted!r}"
    )


# ---------------------------------------------------------------------------
# AC-D1-9: promote current_max from "Output Current"
# ---------------------------------------------------------------------------

def test_ac_d1_9_promote_current_max(tmp_path: pathlib.Path) -> None:
    """Given attribute name "Output Current", value_text="500mA".
    When normalize() is called.
    Then value_num≈0.5, unit="A", promoted["current_max"]≈0.5.
    """
    parts = [_make_base("C1009", attributes=[
        AttrRecord(name="Output Current", value_text="500mA"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1009")

    attrs = [a for a in part.attributes if a.name == "Output Current"]
    assert attrs, f"Output Current attr missing; attrs={part.attributes!r}"
    attr = attrs[0]
    assert attr.value_num == pytest.approx(0.5, rel=1e-6), (
        f"Expected value_num≈0.5, got {attr.value_num!r}"
    )
    assert attr.unit == "A", f"Expected unit='A', got {attr.unit!r}"
    assert part.promoted.get("current_max") == pytest.approx(0.5, rel=1e-6), (
        f"promoted['current_max'] expected 0.5, got {part.promoted!r}"
    )


# ---------------------------------------------------------------------------
# AC-D1-10: ambiguous "Output Voltage" NOT promoted to voltage_min/max
# ---------------------------------------------------------------------------

def test_ac_d1_10_output_voltage_not_promoted(tmp_path: pathlib.Path) -> None:
    """Given attribute name "Output Voltage", value_text="10V".
    When normalize() is called.
    Then value_num≈10.0, unit="V" (enrichment happens), BUT
    promoted MUST NOT contain "voltage_max" or "voltage_min"
    (ambiguous voltage names are explicitly excluded from promotion).
    """
    parts = [_make_base("C1010", attributes=[
        AttrRecord(name="Output Voltage", value_text="10V"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1010")

    # Enrichment (value_num) still expected — the exclusion is promotion only
    attrs = [a for a in part.attributes if a.name == "Output Voltage"]
    assert attrs, f"Output Voltage attr missing; attrs={part.attributes!r}"
    attr = attrs[0]
    assert attr.value_num == pytest.approx(10.0, rel=1e-6), (
        f"Enrichment must still set value_num≈10.0, got {attr.value_num!r}"
    )

    # Neither voltage_max nor voltage_min should be populated
    assert "voltage_max" not in part.promoted, (
        f"'Output Voltage' MUST NOT promote to voltage_max (ambiguous name excluded). "
        f"promoted={part.promoted!r}"
    )
    assert "voltage_min" not in part.promoted, (
        f"'Output Voltage' MUST NOT promote to voltage_min. promoted={part.promoted!r}"
    )


# ---------------------------------------------------------------------------
# AC-D1-11: unknown attribute name "Mounting Style" not promoted
# ---------------------------------------------------------------------------

def test_ac_d1_11_unknown_attr_name_not_promoted(tmp_path: pathlib.Path) -> None:
    """Given attribute name "Mounting Style", value_text="SMD".
    When normalize() is called.
    Then the part has an empty promoted dict (nothing to promote for this name).
    """
    parts = [_make_base("C1011", attributes=[
        AttrRecord(name="Mounting Style", value_text="SMD"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1011")

    # No numeric value so value_num=null, and nothing promoted
    assert part.promoted == {}, (
        f"Unknown attr 'Mounting Style' must not cause any promotion; "
        f"promoted={part.promoted!r}"
    )


# ---------------------------------------------------------------------------
# AC-D1-12: determinism — two normalize runs over enrichable attrs are byte-identical
# ---------------------------------------------------------------------------

def test_ac_d1_12_determinism_enrichable_attrs(tmp_path: pathlib.Path) -> None:
    """Given a fixed set of parts with enrichable attributes.
    When normalize() is called twice writing to separate paths.
    Then the two JSONL files are byte-identical.
    """
    parts = [
        _make_base("C2001", attributes=[
            AttrRecord(name="Resistance", value_text="30kΩ"),
            AttrRecord(name="Power Dissipation", value_text="100mW"),
            AttrRecord(name="Tolerance", value_text="±1%"),
        ]),
        _make_base("C2002", mpn="CHIP2", attributes=[
            AttrRecord(name="Output Current", value_text="500mA"),
        ]),
    ]

    out1 = tmp_path / "run1.jsonl"
    out2 = tmp_path / "run2.jsonl"
    normalize(adapter=_FakeAdapter(parts), source_ref="test@2026-06-12", output_path=out1)
    normalize(adapter=_FakeAdapter(parts), source_ref="test@2026-06-12", output_path=out2)

    assert out1.read_bytes() == out2.read_bytes(), (
        "Two normalize() runs over identical enrichable input must be byte-identical."
    )


# ---------------------------------------------------------------------------
# AC-D1-13: multi-value of ambiguous name: derived records NOT promoted
# ---------------------------------------------------------------------------

def test_ac_d1_13_multi_value_ambiguous_not_promoted(tmp_path: pathlib.Path) -> None:
    """Given attribute name "Supply Voltage" (explicitly NOT in promotion lexicon),
    value_text="1.8V;3.3V".
    When normalize() is called.
    Then derived records are created for each token, but the derived value_nums
    are NOT written to promoted (supply voltage is ambiguous, excluded from
    voltage_max / voltage_min promotion).
    """
    parts = [_make_base("C1013", attributes=[
        AttrRecord(name="Supply Voltage", value_text="1.8V;3.3V"),
    ])]
    result = _run_normalize(parts, tmp_path)
    part = _find_part(result, "C1013")

    # Derived records should exist (enrichment still happens)
    supply_attrs = [a for a in part.attributes if a.name == "Supply Voltage"]
    assert len(supply_attrs) >= 3, (
        f"Expected ≥3 Supply Voltage records (original+2 derived), got {len(supply_attrs)}"
    )
    # But promoted must NOT contain voltage_max or voltage_min
    assert "voltage_max" not in part.promoted, (
        f"'Supply Voltage' derived records must NOT promote to voltage_max. "
        f"promoted={part.promoted!r}"
    )
    assert "voltage_min" not in part.promoted, (
        f"'Supply Voltage' derived records must NOT promote to voltage_min. "
        f"promoted={part.promoted!r}"
    )
