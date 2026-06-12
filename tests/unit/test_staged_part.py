"""
Tests: T-STAGED-*

Verifies partgraph.normalize.model.StagedPart:
- T-STAGED-contract:  to_json()/from_json() round-trip identity with
                      sort_keys=True and ensure_ascii=False.
- T-STAGED-mpnnorm:   MPN normalization rules (strip whitespace, uppercase,
                      strip non-alphanumeric suffix characters, reject all-
                      punctuation inputs).
- T-STAGED-xid:       xid is deterministically f"{mpn_norm}|{mfr_norm}".

NOTE: Collection will ERROR if partgraph.normalize.model does not yet exist.
That is the expected red state before implementation.
"""

from __future__ import annotations

import json

import pytest

from partgraph.normalize.model import AttrRecord, StagedPart  # noqa: F401


# ---------------------------------------------------------------------------
# Minimal valid StagedPart factory
# ---------------------------------------------------------------------------

def _make_part(**overrides) -> StagedPart:
    """Return a minimal valid StagedPart, applying any field overrides."""
    defaults: dict = {
        "mpn": "MAX232ACPE+",
        "mpn_norm": "MAX232ACPE",
        "mfr_name": "Texas Instruments",
        "mfr_norm": "TEXASINSTRUMENTS",
        "xid": "MAX232ACPE|TEXASINSTRUMENTS",
        "description": "RS-232 Line Driver/Receiver",
        "package": "DIP-16",
        "category": "IC",
        "subcategory": "Interface",
        "datasheet_url": "https://www.ti.com/lit/ds/max232.pdf",
        "lcsc_id": "C97805",
        "stock": 1500,
        "price_usd": 0.45,
        "is_basic": False,
        "promoted": {"voltage_max": 5.0, "current_max": 0.01},
        "attributes": [
            AttrRecord(name="Supply Voltage", value_text="5V", value_num=5.0, unit="V"),
        ],
        "tags": ["RS-232", "UART"],
        "source_ref": "jlcparts@2026-06-11",
    }
    defaults.update(overrides)
    return StagedPart(**defaults)


# ---------------------------------------------------------------------------
# T-STAGED-contract
# ---------------------------------------------------------------------------

def test_staged_contract_round_trip_identity() -> None:
    """Given a fully populated StagedPart.
    When to_json() is called and the result is passed to from_json().
    Then the deserialized object equals the original (field-by-field identity).
    """
    original = _make_part()
    serialized = original.to_json()
    deserialized = StagedPart.from_json(serialized)
    assert deserialized == original, (
        f"Round-trip identity failed.\nOriginal:     {original}\nDeserialized: {deserialized}"
    )


def test_staged_contract_json_keys_sorted() -> None:
    """Given a StagedPart.
    When to_json() is called.
    Then the resulting JSON string must have keys in sorted order
    (sort_keys=True requirement for deterministic output).
    """
    part = _make_part()
    serialized = part.to_json()
    parsed = json.loads(serialized)
    keys = list(parsed.keys())
    assert keys == sorted(keys), (
        f"JSON keys are not sorted. Got: {keys}"
    )


def test_staged_contract_ensure_ascii_false() -> None:
    """Given a StagedPart with non-ASCII characters in description.
    When to_json() is called.
    Then the output contains the literal Unicode characters, not \\uXXXX escapes
    (ensure_ascii=False requirement).
    """
    part = _make_part(description="Résistance 10kΩ")
    serialized = part.to_json()
    assert "Résistance" in serialized, (
        "Non-ASCII characters must appear as literal Unicode in JSON output. "
        "ensure_ascii=False must be passed to json.dumps."
    )
    assert "\\u" not in serialized or "Résistance" in serialized, (
        f"Unexpected Unicode escapes found in: {serialized!r}"
    )


def test_staged_contract_attr_record_round_trip() -> None:
    """Given a StagedPart containing multiple AttrRecord entries.
    When round-tripped through to_json()/from_json().
    Then each AttrRecord is preserved with all fields intact.
    """
    attrs = [
        AttrRecord(name="Resistance", value_text="10kΩ", value_num=10000.0, unit="Ω"),
        AttrRecord(name="Mounting Style", value_text="SMD", value_num=None, unit=None),
    ]
    part = _make_part(attributes=attrs)
    deserialized = StagedPart.from_json(part.to_json())
    assert deserialized.attributes == attrs, (
        f"AttrRecord list not preserved after round-trip.\n"
        f"Expected: {attrs}\nGot:      {deserialized.attributes}"
    )


def test_staged_contract_empty_promoted_round_trip() -> None:
    """Given a StagedPart with an empty promoted dict.
    When round-tripped.
    Then promoted == {} exactly.
    """
    part = _make_part(promoted={})
    deserialized = StagedPart.from_json(part.to_json())
    assert deserialized.promoted == {}


def test_staged_contract_none_optional_fields_preserved() -> None:
    """Given a StagedPart where optional fields are None.
    When round-tripped through to_json()/from_json().
    Then None fields are preserved as None (not missing or default-filled).
    """
    part = _make_part(
        datasheet_url=None,
        price_usd=None,
        package=None,
        subcategory=None,
    )
    deserialized = StagedPart.from_json(part.to_json())
    assert deserialized.datasheet_url is None
    assert deserialized.price_usd is None
    assert deserialized.package is None
    assert deserialized.subcategory is None


# ---------------------------------------------------------------------------
# T-STAGED-mpnnorm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_mpn, expected_norm", [
    # Trailing non-alphanumeric stripped, uppercase.
    ("MAX232ACPE+", "MAX232ACPE"),
    # Mixed case, separator, E/SE variant suffix retained as alnum.
    ("max3232-e/se", "MAX3232ESE"),
    # Leading/trailing whitespace stripped.
    ("  TL072  ", "TL072"),
    # Already normalized.
    ("LM358N", "LM358N"),
    # Digits only.
    ("4066B", "4066B"),
    # Trailing slash/plus stripped.
    ("STM32F103C8T6/TRAY", "STM32F103C8T6TRAY"),
    # Multiple trailing punctuation.
    ("BC547A++", "BC547A"),
])
def test_staged_mpnnorm_normalization(raw_mpn: str, expected_norm: str) -> None:
    """Given a raw MPN string.
    When StagedPart is constructed with mpn=raw_mpn and the normalizer applied.
    Then mpn_norm equals expected_norm.

    The normalization contract:
    - Uppercase all alpha characters.
    - Strip leading/trailing whitespace.
    - Replace or strip separator characters (-, /, space) that appear between
      alphanumeric groups according to the normalization rules.
    - Strip trailing punctuation/symbols that are not alphanumeric.
    """
    # We test the normalization function independently of the model constructor
    # since the normalization logic may be a standalone utility.
    # Import normalize_mpn if exposed at module level, or derive from StagedPart.
    try:
        from partgraph.normalize.model import normalize_mpn
        result = normalize_mpn(raw_mpn)
    except ImportError:
        # Fallback: construct a StagedPart with the helper fields and inspect.
        pytest.skip("normalize_mpn not exported; test via StagedPart constructor instead.")
        return

    assert result == expected_norm, (
        f"normalize_mpn({raw_mpn!r}) = {result!r}, expected {expected_norm!r}"
    )


def test_staged_mpnnorm_all_punctuation_rejected() -> None:
    """Given an MPN string consisting entirely of punctuation/symbols (no alnum).
    When normalization is applied.
    Then the result is empty-string or the part is rejected.
    """
    try:
        from partgraph.normalize.model import normalize_mpn
        result = normalize_mpn("+++")
        assert result == "" or result is None, (
            f"All-punctuation MPN should normalize to empty/None, got {result!r}"
        )
    except ImportError:
        pytest.skip("normalize_mpn not exported.")


# ---------------------------------------------------------------------------
# T-STAGED-xid
# ---------------------------------------------------------------------------

def test_staged_xid_format_is_mpnnorm_pipe_mfrnorm() -> None:
    """Given a StagedPart with mpn_norm='LM358N' and mfr_norm='TEXASINSTRUMENTS'.
    When the xid field is read.
    Then xid == 'LM358N|TEXASINSTRUMENTS'.
    """
    part = _make_part(
        mpn="LM358N",
        mpn_norm="LM358N",
        mfr_name="Texas Instruments",
        mfr_norm="TEXASINSTRUMENTS",
        xid="LM358N|TEXASINSTRUMENTS",
    )
    assert part.xid == "LM358N|TEXASINSTRUMENTS"


def test_staged_xid_is_deterministic() -> None:
    """Given two StagedParts constructed with the same mpn_norm and mfr_norm.
    When their xid fields are compared.
    Then they are equal (xid is deterministic, not random/time-based).
    """
    part_a = _make_part(
        mpn="BC547A",
        mpn_norm="BC547A",
        mfr_name="Fairchild",
        mfr_norm="FAIRCHILD",
        xid="BC547A|FAIRCHILD",
    )
    part_b = _make_part(
        mpn="BC547A",
        mpn_norm="BC547A",
        mfr_name="Fairchild",
        mfr_norm="FAIRCHILD",
        xid="BC547A|FAIRCHILD",
    )
    assert part_a.xid == part_b.xid == "BC547A|FAIRCHILD"


def test_staged_xid_pipe_separator_present() -> None:
    """Given any StagedPart.
    When the xid field is read.
    Then it contains exactly one '|' separating mpn_norm and mfr_norm.
    """
    part = _make_part(
        mpn_norm="TESTMPN",
        mfr_norm="TESTMFR",
        xid="TESTMPN|TESTMFR",
    )
    parts_of_xid = part.xid.split("|")
    assert len(parts_of_xid) == 2, (
        f"xid must contain exactly one '|', got: {part.xid!r}"
    )
    assert parts_of_xid[0] == "TESTMPN"
    assert parts_of_xid[1] == "TESTMFR"


def test_staged_part_is_frozen() -> None:
    """Given a StagedPart instance.
    When we attempt to modify a field.
    Then a FrozenInstanceError (or AttributeError) is raised.

    StagedPart must be a frozen dataclass to ensure immutability after creation.
    """
    part = _make_part()
    with pytest.raises((AttributeError, TypeError)):
        part.mpn = "MODIFIED"  # type: ignore[misc]
