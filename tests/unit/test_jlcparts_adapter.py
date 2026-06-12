"""
Tests: T-ADAPT-*

Verifies partgraph.sources.jlcparts.JlcpartsAdapter behaviour against an
in-memory SQLite3 database so NO file I/O or real component data is required.

Two schema shapes are exercised:
  Strategy A -- denormalized: category and manufacturer stored directly on the
               components table as plain TEXT columns.
  Strategy B -- FK-joined:    components table holds integer FKs referencing
               separate manufacturers and categories tables.

The REAL CDFER schema (verified against data/raw/jlcpcb-components.sqlite3,
616 593 rows, 2026-06-11) is Strategy B:

  components columns:
    lcsc           INTEGER  (e.g. 1002)  → lcsc_id rendered as "C1002"
    mfr            TEXT     (THE MPN STRING — confusingly named; primary MPN source)
    manufacturer_id INTEGER  FK → manufacturers(id)
    category_id    INTEGER  FK → categories(id)
    basic          INTEGER  (0/1) → is_basic bool
    description    TEXT
    package        TEXT
    datasheet      TEXT
    stock          INTEGER
    price          TEXT     (JSON tier array)
    extra          TEXT     (JSON; top-level "mpn" = fallback; "attributes" = flat str→str dict)
    flag, joints, last_update, last_on_stock, preferred

  manufacturers(id, name)
  categories(id, category, subcategory)   -- NOT "name"; category=L1, subcategory=L2

  MPN resolution order: components.mfr first; fallback to extra["mpn"] when mfr
  is empty or NULL.

  is_basic ← basic (0/1 int → bool)

  lcsc_id rendered as canonical "C{lcsc}" string (e.g. lcsc=1002 → "C1002")

  category  ← categories.category   (via category_id FK)
  subcategory ← categories.subcategory (via category_id FK)
  manufacturer ← manufacturers.name (via manufacturer_id FK)

Tests:
- T-ADAPT-introspect:    introspection selects the correct strategy for each
                         schema shape, and raises a clear error when neither
                         matches.
- T-ADAPT-skip-empty-mpn: rows with empty/NULL mfr AND empty/absent extra.mpn
                           are skipped; counter records how many were skipped.
- T-ADAPT-corrupt-extra:  rows whose extra JSON column is not a valid dict
                          produce StagedPart with attributes=[] and increment
                          a counter.
- T-ADAPT-attrs:          structured {"format","primary","values":{k:[num,unit]}}
                          extraction AND plain-string attribute fallback both
                          produce AttrRecord objects correctly.
- T-ADAPT-fk:             orphan FK (manufacturer or category FK points to a
                          nonexistent row) resolves to None field + counter.
- T-ADAPT-open-ro:        open_jlcparts_db() returns a read-only connection.
- T-ADAPT-no-fetchall:    iter_parts() must use iterator protocol (fetchmany),
                          never fetchall.
- T-ADAPT-identifier-safety: discovered column names validated against an
                              identifier allowlist; malicious columns are
                              ignored or raise a clear error without generating
                              malformed SQL.

NOTE: Collection will ERROR if partgraph.sources.jlcparts does not yet exist.
That is the expected red state before implementation.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from typing import Any

import pytest

from partgraph.sources.jlcparts import JlcpartsAdapter  # noqa: F401
from partgraph.sources.jlcparts import open_jlcparts_db  # noqa: F401
from partgraph.normalize.model import StagedPart, AttrRecord  # noqa: F401

# ---------------------------------------------------------------------------
# Identifier safety pattern -- only [A-Za-z_][A-Za-z0-9_]* column names allowed.
# ---------------------------------------------------------------------------
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# DB Builders
# ---------------------------------------------------------------------------

def _build_denormalized_db(rows: list[dict[str, Any]]) -> sqlite3.Connection:
    """Return an in-memory SQLite3 connection using the denormalized schema.

    Denormalized schema has category and manufacturer as TEXT columns directly
    on the components table (Strategy A).  This shape is NOT the real CDFER
    schema but must still be supported for backward compatibility with older
    distributions.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE components (
            lcsc        TEXT PRIMARY KEY,
            mpn         TEXT,
            manufacturer TEXT,
            category    TEXT,
            subcategory TEXT,
            description TEXT,
            package     TEXT,
            datasheet   TEXT,
            stock       INTEGER DEFAULT 0,
            price       TEXT DEFAULT '[]',
            is_basic    INTEGER DEFAULT 0,
            extra       TEXT DEFAULT '{}'
        )
    """)
    for row in rows:
        conn.execute(
            """
            INSERT INTO components
              (lcsc, mpn, manufacturer, category, subcategory, description,
               package, datasheet, stock, price, is_basic, extra)
            VALUES
              (:lcsc, :mpn, :manufacturer, :category, :subcategory, :description,
               :package, :datasheet, :stock, :price, :is_basic, :extra)
            """,
            {
                "lcsc": row.get("lcsc", "C1"),
                "mpn": row.get("mpn", "TESTMPN"),
                "manufacturer": row.get("manufacturer", "TestMfr"),
                "category": row.get("category", "IC"),
                "subcategory": row.get("subcategory", "Logic"),
                "description": row.get("description", "Test part"),
                "package": row.get("package", "SOP-8"),
                "datasheet": row.get("datasheet", "https://example.com/ds.pdf"),
                "stock": row.get("stock", 100),
                "price": row.get("price", "[]"),
                "is_basic": int(row.get("is_basic", False)),
                "extra": row.get("extra", "{}"),
            },
        )
    conn.commit()
    return conn


def _build_fk_db(
    components: list[dict[str, Any]],
    manufacturers: list[dict[str, Any]] | None = None,
    categories: list[dict[str, Any]] | None = None,
) -> sqlite3.Connection:
    """Return an in-memory SQLite3 connection matching the REAL CDFER schema.

    Strategy B (FK-joined) -- REAL CDFER shape:
      components.lcsc          INTEGER  (rendered as "C{lcsc}")
      components.mfr           TEXT     (MPN string; primary MPN source)
      components.manufacturer_id INTEGER FK -> manufacturers(id)
      components.category_id   INTEGER FK -> categories(id)
      components.basic         INTEGER  (0/1 -> is_basic bool)
      manufacturers(id, name)
      categories(id, category, subcategory)

    extra top-level "mpn" is the MPN fallback when mfr is empty.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE manufacturers (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE categories (
            id          INTEGER PRIMARY KEY,
            category    TEXT NOT NULL,
            subcategory TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE components (
            lcsc            INTEGER PRIMARY KEY,
            mfr             TEXT,
            manufacturer_id INTEGER REFERENCES manufacturers(id),
            category_id     INTEGER REFERENCES categories(id),
            description     TEXT,
            package         TEXT,
            datasheet       TEXT,
            stock           INTEGER DEFAULT 0,
            price           TEXT DEFAULT '[]',
            basic           INTEGER DEFAULT 0,
            extra           TEXT DEFAULT '{}'
        )
    """)
    for mfr in manufacturers or [{"id": 1, "name": "TestMfr"}]:
        conn.execute(
            "INSERT INTO manufacturers (id, name) VALUES (:id, :name)", mfr
        )
    for cat in categories or [
        {"id": 1, "category": "IC", "subcategory": "Logic"}
    ]:
        conn.execute(
            "INSERT INTO categories (id, category, subcategory) "
            "VALUES (:id, :category, :subcategory)",
            cat,
        )
    for row in components:
        conn.execute(
            """
            INSERT INTO components
              (lcsc, mfr, manufacturer_id, category_id, description,
               package, datasheet, stock, price, basic, extra)
            VALUES
              (:lcsc, :mfr, :manufacturer_id, :category_id, :description,
               :package, :datasheet, :stock, :price, :basic, :extra)
            """,
            {
                "lcsc": row.get("lcsc", 1),
                "mfr": row.get("mfr", "TESTMPN"),
                "manufacturer_id": row.get("manufacturer_id", 1),
                "category_id": row.get("category_id", 1),
                "description": row.get("description", "Test part"),
                "package": row.get("package", "SOP-8"),
                "datasheet": row.get("datasheet", "https://example.com/ds.pdf"),
                "stock": row.get("stock", 100),
                "price": row.get("price", "[]"),
                "basic": int(row.get("basic", False)),
                "extra": row.get("extra", "{}"),
            },
        )
    conn.commit()
    return conn


def _build_unknown_schema_db() -> sqlite3.Connection:
    """Return an in-memory DB whose components table has neither recognized shape."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE components (
            lcsc INTEGER PRIMARY KEY,
            mfr  TEXT
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# T-ADAPT-introspect
# ---------------------------------------------------------------------------

def test_adapt_introspect_denormalized_selects_strategy_a() -> None:
    """Given a components table with TEXT manufacturer/category columns.
    When JlcpartsAdapter is constructed with that connection.
    Then it selects Strategy A (denormalized) without raising.
    """
    conn = _build_denormalized_db([{"lcsc": "C1", "mpn": "TESTPART"}])
    adapter = JlcpartsAdapter(conn)
    # Strategy A is active: introspection succeeds and yields at least one part.
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    assert parts[0].lcsc_id == "C1"


def test_adapt_introspect_fk_selects_strategy_b() -> None:
    """Given a components table with the real CDFER FK schema (mfr, basic, lcsc
    INTEGER, categories with category/subcategory columns).
    When JlcpartsAdapter is constructed with that connection.
    Then it selects Strategy B (FK-join) without raising.
    """
    conn = _build_fk_db(
        components=[{
            "lcsc": 2,
            "mfr": "FKPART",
            "manufacturer_id": 1,
            "category_id": 1,
        }],
        manufacturers=[{"id": 1, "name": "FKMfr"}],
        categories=[{"id": 1, "category": "Filters", "subcategory": "Ferrite Beads"}],
    )
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    assert parts[0].mfr_name == "FKMfr"


def test_adapt_introspect_fk_lcsc_integer_rendered_as_c_string() -> None:
    """Given a real-schema components row where lcsc is INTEGER 1002.
    When JlcpartsAdapter iterates it.
    Then lcsc_id on the resulting StagedPart is the string "C1002".

    Contract: lcsc_id = "C" + str(lcsc_int).
    """
    conn = _build_fk_db(
        components=[{
            "lcsc": 1002,
            "mfr": "GZ1608D601TF",
            "manufacturer_id": 1,
            "category_id": 1,
        }],
        manufacturers=[{"id": 1, "name": "Sunlord"}],
        categories=[{"id": 1, "category": "Filters", "subcategory": "Ferrite Beads"}],
    )
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    assert parts[0].lcsc_id == "C1002", (
        f"lcsc INTEGER 1002 must render as 'C1002', got {parts[0].lcsc_id!r}"
    )


def test_adapt_introspect_fk_basic_int_maps_to_is_basic_bool() -> None:
    """Given real-schema rows where basic=1 and basic=0.
    When JlcpartsAdapter iterates them.
    Then is_basic is True for basic=1 and False for basic=0.

    Contract: is_basic <- bool(basic).
    """
    conn = _build_fk_db(
        components=[
            {"lcsc": 10, "mfr": "BASIC_PART", "manufacturer_id": 1, "category_id": 1,
             "basic": 1},
            {"lcsc": 11, "mfr": "NONBASIC_PART", "manufacturer_id": 1, "category_id": 1,
             "basic": 0},
        ],
        manufacturers=[{"id": 1, "name": "Mfr"}],
        categories=[{"id": 1, "category": "IC", "subcategory": "Logic"}],
    )
    adapter = JlcpartsAdapter(conn)
    parts = {p.lcsc_id: p for p in adapter.iter_parts()}
    assert parts["C10"].is_basic is True, (
        "basic=1 must map to is_basic=True"
    )
    assert parts["C11"].is_basic is False, (
        "basic=0 must map to is_basic=False"
    )


def test_adapt_introspect_fk_category_and_subcategory_from_categories_table() -> None:
    """Given a real-schema categories table with category and subcategory columns.
    When JlcpartsAdapter resolves a category_id FK.
    Then StagedPart.category = categories.category (L1 name)
    AND StagedPart.subcategory = categories.subcategory (L2 name).

    Contract: NOT categories.name (that column does not exist in the real schema).
    """
    conn = _build_fk_db(
        components=[{
            "lcsc": 1003,
            "mfr": "GZ1608D151TF",
            "manufacturer_id": 1,
            "category_id": 5,
        }],
        manufacturers=[{"id": 1, "name": "Sunlord"}],
        categories=[{
            "id": 5,
            "category": "Filters/EMI Optimization",
            "subcategory": "Ferrite Beads",
        }],
    )
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    assert parts[0].category == "Filters/EMI Optimization", (
        f"category must come from categories.category, got {parts[0].category!r}"
    )
    assert parts[0].subcategory == "Ferrite Beads", (
        f"subcategory must come from categories.subcategory, got {parts[0].subcategory!r}"
    )


def test_adapt_introspect_fk_mpn_from_mfr_column() -> None:
    """Given a real-schema row where mfr='GZ1608D601TF'.
    When JlcpartsAdapter iterates it.
    Then StagedPart.mpn == 'GZ1608D601TF'.

    Contract: MPN comes from components.mfr (primary source).
    """
    conn = _build_fk_db(
        components=[{
            "lcsc": 1002,
            "mfr": "GZ1608D601TF",
            "manufacturer_id": 1,
            "category_id": 1,
        }],
        manufacturers=[{"id": 1, "name": "Sunlord"}],
        categories=[{"id": 1, "category": "Filters", "subcategory": "Ferrite Beads"}],
    )
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    assert parts[0].mpn == "GZ1608D601TF", (
        f"MPN must come from components.mfr, got {parts[0].mpn!r}"
    )


def test_adapt_introspect_fk_mpn_fallback_to_extra_mpn_when_mfr_empty() -> None:
    """Given a real-schema row where mfr is empty-string AND extra has top-level mpn.
    When JlcpartsAdapter iterates it.
    Then StagedPart.mpn comes from extra['mpn'] (fallback source).

    Contract: MPN order = mfr first; extra.mpn when mfr is empty/NULL.
    """
    extra_with_mpn = json.dumps({"mpn": "FALLBACKMPN123"})
    # Build with raw sqlite3 so we can insert empty-string mfr.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT NOT NULL)
    """)
    conn.execute("""
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY, category TEXT NOT NULL, subcategory TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE components (
            lcsc INTEGER PRIMARY KEY,
            mfr TEXT,
            manufacturer_id INTEGER,
            category_id INTEGER,
            description TEXT DEFAULT 'desc',
            package TEXT DEFAULT 'SOP-8',
            datasheet TEXT DEFAULT 'https://x.com',
            stock INTEGER DEFAULT 0,
            price TEXT DEFAULT '[]',
            basic INTEGER DEFAULT 0,
            extra TEXT DEFAULT '{}'
        )
    """)
    conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'TestMfr')")
    conn.execute("INSERT INTO categories (id, category, subcategory) VALUES (1, 'IC', 'Logic')")
    # Empty-string mfr, extra has mpn.
    conn.execute(
        "INSERT INTO components (lcsc, mfr, manufacturer_id, category_id, extra) "
        "VALUES (?, ?, 1, 1, ?)",
        (9001, "", extra_with_mpn),
    )
    conn.commit()

    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1, "Row with empty mfr but valid extra.mpn must be yielded."
    assert parts[0].mpn == "FALLBACKMPN123", (
        f"Fallback to extra.mpn failed; got mpn={parts[0].mpn!r}"
    )
    assert parts[0].lcsc_id == "C9001"


def test_adapt_introspect_unknown_schema_raises_with_found_columns() -> None:
    """Given a components table with an unrecognized column set.
    When JlcpartsAdapter is constructed with that connection.
    Then it raises a clear error that lists the columns it found.
    """
    conn = _build_unknown_schema_db()
    with pytest.raises(Exception) as exc_info:
        JlcpartsAdapter(conn)
    msg = str(exc_info.value).lower()
    # The error must be clear -- must at least mention columns or schema.
    assert any(kw in msg for kw in ("column", "schema", "manufacturer", "lcsc")), (
        f"Error message for unknown schema shape should mention relevant columns, got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# T-ADAPT-skip-empty-mpn
# ---------------------------------------------------------------------------

def test_adapt_skip_empty_mpn_increments_counter() -> None:
    """Given a real-schema (Strategy B) components table where:
      - one row has a valid mfr string
      - one row has empty-string mfr AND empty extra (no extra.mpn)
      - one row has NULL mfr AND empty extra
      - one row has whitespace-only mfr AND empty extra
    When iter_parts() is called.
    Then the first row is yielded; the rest are skipped (both mfr and extra.mpn
    are empty/NULL), and skipped_empty_mpn counter == 3.

    Contract: a row is skipped only when BOTH mfr and extra['mpn'] are absent/empty.
    """
    # Build with raw sqlite3 so we can insert NULL mfr.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT NOT NULL)
    """)
    conn.execute("""
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY, category TEXT NOT NULL, subcategory TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE components (
            lcsc            INTEGER PRIMARY KEY,
            mfr             TEXT,
            manufacturer_id INTEGER DEFAULT 1,
            category_id     INTEGER DEFAULT 1,
            description     TEXT DEFAULT 'desc',
            package         TEXT DEFAULT 'SOP-8',
            datasheet       TEXT DEFAULT 'https://x.com',
            stock           INTEGER DEFAULT 0,
            price           TEXT DEFAULT '[]',
            basic           INTEGER DEFAULT 0,
            extra           TEXT DEFAULT '{}'
        )
    """)
    conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'TestMfr')")
    conn.execute("INSERT INTO categories (id, category, subcategory) VALUES (1, 'IC', 'Logic')")
    rows = [
        (10, "GOOD_PART", "{}"),            # valid mfr
        (11, "", "{}"),                      # empty-string mfr, no extra.mpn
        (12, None, "{}"),                    # NULL mfr, no extra.mpn
        (13, "   ", "{}"),                   # whitespace-only mfr, no extra.mpn
    ]
    for lcsc, mfr, extra in rows:
        conn.execute(
            "INSERT INTO components (lcsc, mfr, extra) VALUES (?, ?, ?)",
            (lcsc, mfr, extra),
        )
    conn.commit()

    adapter = JlcpartsAdapter(conn)
    yielded = list(adapter.iter_parts())
    yielded_lcscs = [p.lcsc_id for p in yielded]

    assert "C10" in yielded_lcscs, "Row with valid mfr must be yielded."
    assert "C11" not in yielded_lcscs, "Empty-string mfr + no extra.mpn must be skipped."
    assert "C12" not in yielded_lcscs, "NULL mfr + no extra.mpn must be skipped."
    assert "C13" not in yielded_lcscs, "Whitespace-only mfr + no extra.mpn must be skipped."
    assert adapter.counters["skipped_empty_mpn"] == 3, (
        f"Expected 3 skipped_empty_mpn, got {adapter.counters.get('skipped_empty_mpn')}"
    )


# ---------------------------------------------------------------------------
# T-ADAPT-corrupt-extra
# ---------------------------------------------------------------------------

def test_adapt_corrupt_extra_json_produces_empty_attrs() -> None:
    """Given real-schema rows where one has valid extra, one has corrupt JSON,
    and one has a JSON array (not a dict).
    When iter_parts() is called.
    Then corrupt rows yield StagedPart with attributes=[] and the adapter
    counter 'corrupt_extra' is incremented.
    """
    rows = [
        {
            "lcsc": 20,
            "mfr": "OK_PART",
            "extra": json.dumps({"attributes": {"Resistance": "10k"}}),
        },
        {
            "lcsc": 21,
            "mfr": "BAD_JSON",
            "extra": "not-json{{{",
        },
        {
            "lcsc": 22,
            "mfr": "NON_DICT",
            "extra": json.dumps([1, 2, 3]),
        },
    ]
    conn = _build_fk_db(rows)
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    by_lcsc = {p.lcsc_id: p for p in parts}

    assert "C21" in by_lcsc, "Bad-JSON row must still yield a StagedPart."
    assert by_lcsc["C21"].attributes == [], (
        "Corrupt extra JSON must produce attributes=[]."
    )
    assert "C22" in by_lcsc, "Non-dict extra row must still yield a StagedPart."
    assert by_lcsc["C22"].attributes == [], (
        "Non-dict extra must produce attributes=[]."
    )
    assert adapter.counters.get("corrupt_extra", 0) >= 2, (
        f"Expected at least 2 corrupt_extra counted, got {adapter.counters.get('corrupt_extra')}"
    )


# ---------------------------------------------------------------------------
# T-ADAPT-attrs
# ---------------------------------------------------------------------------

def test_adapt_attrs_structured_format_extraction() -> None:
    """Given a real-schema row whose extra column has the structured JLC attribute
    format:
      {"attributes": {"Resistance": {"format": "~{$unit}", "primary": "10k",
                                     "values": {"Resistance": [num,unit]}}}}
    When iter_parts() is called.
    Then the resulting StagedPart contains an AttrRecord with name="Resistance",
    value_num=10000, unit="Ohm".
    """
    structured_extra = json.dumps({
        "attributes": {
            "Resistance": {
                "format": "~{$unit}",
                "primary": "10kOhm",
                "values": {
                    "Resistance": [10000.0, "Ohm"],
                },
            },
            "Power (Watts)": {
                "format": "{$unit}",
                "primary": "0.125W",
                "values": {
                    "Power (Watts)": [0.125, "W"],
                },
            },
        }
    })
    conn = _build_fk_db([{
        "lcsc": 30,
        "mfr": "RES10K",
        "extra": structured_extra,
    }])
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    attrs = {a.name: a for a in parts[0].attributes}
    assert "Resistance" in attrs, f"Resistance attr missing from {list(attrs)}"
    assert attrs["Resistance"].value_num == pytest.approx(10000.0)
    assert attrs["Resistance"].unit == "Ohm"
    assert "Power (Watts)" in attrs
    assert attrs["Power (Watts)"].value_num == pytest.approx(0.125)


def test_adapt_attrs_plain_string_fallback() -> None:
    """Given a real-schema row whose extra attributes contain plain-string values
    (the dominant real-file shape: extra.attributes is a flat str->str dict).
    When iter_parts() is called.
    Then the resulting StagedPart contains AttrRecord entries with value_text set
    to the string and value_num=None.
    """
    plain_extra = json.dumps({
        "attributes": {
            "Operating Temperature": "-40C~+85C",
            "Mounting Style": "SMD",
        }
    })
    conn = _build_fk_db([{
        "lcsc": 31,
        "mfr": "TEMPPART",
        "extra": plain_extra,
    }])
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    attrs = {a.name: a for a in parts[0].attributes}
    assert "Mounting Style" in attrs
    assert attrs["Mounting Style"].value_text == "SMD"
    assert attrs["Mounting Style"].value_num is None
    assert "Operating Temperature" in attrs
    assert attrs["Operating Temperature"].value_text == "-40C~+85C"


def test_adapt_attrs_denormalized_structured_format_extraction() -> None:
    """Given a denormalized-schema (Strategy A) row with structured JLC attrs.
    When iter_parts() is called.
    Then the structured attribute is extracted correctly (same logic as Strategy B).
    """
    structured_extra = json.dumps({
        "attributes": {
            "Resistance": {
                "format": "~{$unit}",
                "primary": "10kOhm",
                "values": {
                    "Resistance": [10000.0, "Ohm"],
                },
            },
        }
    })
    conn = _build_denormalized_db([
        {"lcsc": "C30", "mpn": "RES10K", "extra": structured_extra},
    ])
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    attrs = {a.name: a for a in parts[0].attributes}
    assert "Resistance" in attrs
    assert attrs["Resistance"].value_num == pytest.approx(10000.0)
    assert attrs["Resistance"].unit == "Ohm"


def test_adapt_attrs_denormalized_plain_string_fallback() -> None:
    """Given a denormalized-schema (Strategy A) row with plain-string attributes.
    When iter_parts() is called.
    Then AttrRecords are produced with value_text and value_num=None.
    """
    plain_extra = json.dumps({
        "attributes": {
            "Operating Temperature": "-40C~+85C",
            "Mounting Style": "SMD",
        }
    })
    conn = _build_denormalized_db([
        {"lcsc": "C31", "mpn": "TEMPPART", "extra": plain_extra},
    ])
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    attrs = {a.name: a for a in parts[0].attributes}
    assert "Mounting Style" in attrs
    assert attrs["Mounting Style"].value_text == "SMD"
    assert attrs["Mounting Style"].value_num is None


# ---------------------------------------------------------------------------
# T-ADAPT-fk
# ---------------------------------------------------------------------------

def test_adapt_fk_orphan_manufacturer_resolves_to_none() -> None:
    """Given a real-schema DB where a component references a manufacturer_id
    that does not exist in the manufacturers table (orphan FK).
    When iter_parts() is called.
    Then the resulting StagedPart has mfr_name=None and the adapter counter
    'fk_orphan' is incremented.
    """
    conn = _build_fk_db(
        components=[{
            "lcsc": 40,
            "mfr": "ORPHANPART",
            "manufacturer_id": 999,
            "category_id": 1,
        }],
        manufacturers=[{"id": 1, "name": "ExistingMfr"}],
        categories=[{"id": 1, "category": "IC", "subcategory": "Logic"}],
    )
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    assert parts[0].mfr_name is None, (
        "Orphan manufacturer FK must resolve to mfr_name=None."
    )
    assert adapter.counters.get("fk_orphan", 0) >= 1, (
        f"Expected fk_orphan counter >= 1, got {adapter.counters.get('fk_orphan')}"
    )


def test_adapt_fk_orphan_category_resolves_to_none() -> None:
    """Given a real-schema DB where a component references a category_id that
    does not exist in the categories table (orphan FK).
    When iter_parts() is called.
    Then the resulting StagedPart has category=None and the adapter counter
    'fk_orphan' is incremented.
    """
    conn = _build_fk_db(
        components=[{
            "lcsc": 41,
            "mfr": "ORPHANCAT",
            "manufacturer_id": 1,
            "category_id": 888,
        }],
        manufacturers=[{"id": 1, "name": "GoodMfr"}],
        categories=[{"id": 1, "category": "IC", "subcategory": "Logic"}],
    )
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    assert parts[0].category is None, (
        "Orphan category FK must resolve to category=None."
    )
    assert adapter.counters.get("fk_orphan", 0) >= 1


def test_adapt_fk_valid_row_resolves_names_correctly() -> None:
    """Given a real-schema DB where all FKs are valid.
    When iter_parts() is called.
    Then manufacturer name, category (L1), and subcategory (L2) are resolved
    correctly from the joined tables (no None, fk_orphan counter stays 0).

    Contract: category <- categories.category, subcategory <- categories.subcategory.
    """
    conn = _build_fk_db(
        components=[{
            "lcsc": 50,
            "mfr": "VALIDPART",
            "manufacturer_id": 2,
            "category_id": 3,
            "description": "Valid FK part",
        }],
        manufacturers=[
            {"id": 1, "name": "OtherMfr"},
            {"id": 2, "name": "CorrectMfr"},
        ],
        categories=[
            {"id": 1, "category": "IC", "subcategory": "Logic"},
            {
                "id": 3,
                "category": "Capacitors",
                "subcategory": "Multilayer Ceramic Capacitors MLCC",
            },
        ],
    )
    adapter = JlcpartsAdapter(conn)
    parts = list(adapter.iter_parts())
    assert len(parts) == 1
    assert parts[0].mfr_name == "CorrectMfr"
    assert parts[0].category == "Capacitors", (
        f"category must be L1 name from categories.category, got {parts[0].category!r}"
    )
    assert parts[0].subcategory == "Multilayer Ceramic Capacitors MLCC", (
        f"subcategory must be L2 from categories.subcategory, got {parts[0].subcategory!r}"
    )
    assert adapter.counters.get("fk_orphan", 0) == 0


# ---------------------------------------------------------------------------
# T-ADAPT-open-ro (C1) -- open_jlcparts_db returns a read-only connection
# ---------------------------------------------------------------------------

def test_adapt_open_jlcparts_db_returns_read_only_connection(tmp_path: pathlib.Path) -> None:
    """Given a real temporary SQLite file created with sqlite3.
    When open_jlcparts_db(path) is called.
    Then the returned connection is READ-ONLY: cursor.execute('CREATE TABLE x(y)')
    raises sqlite3.OperationalError.

    The helper must use file: URI mode=ro (or equivalent) so that data files
    can never be accidentally mutated by the adapter code path.
    """
    # Create a valid SQLite file at tmp_path.
    db_path = tmp_path / "test_readonly.sqlite3"
    setup_conn = sqlite3.connect(str(db_path))
    setup_conn.execute("CREATE TABLE components (lcsc INTEGER PRIMARY KEY, mfr TEXT)")
    setup_conn.commit()
    setup_conn.close()

    # Now open it via the production helper.
    ro_conn = open_jlcparts_db(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("CREATE TABLE x(y)")
    finally:
        ro_conn.close()


# ---------------------------------------------------------------------------
# T-ADAPT-no-fetchall (C2) -- iter_parts() must use fetchmany, never fetchall
# ---------------------------------------------------------------------------

def test_adapt_iter_parts_never_calls_fetchall() -> None:
    """Given a real in-memory DB (Strategy A) with multiple rows.
    When iter_parts() is called and consumed fully.
    Then fetchall() is never called on any cursor produced during iteration.

    This ensures the adapter uses an iterator protocol (fetchmany / fetchone)
    so it remains memory-safe on large datasets (600 k+ rows in the real file).
    """
    rows = [{"lcsc": f"C{i}", "mpn": f"MPN{i}"} for i in range(10)]
    conn = _build_denormalized_db(rows)

    # Wrap the connection's cursor() method to spy on fetchall calls.
    original_cursor = conn.cursor

    fetchall_was_called = False

    class _SpyCursor:
        """Proxy that records fetchall() calls."""

        def __init__(self, real_cursor):
            self._c = real_cursor

        def fetchall(self, *args, **kwargs):
            nonlocal fetchall_was_called
            fetchall_was_called = True
            return self._c.fetchall(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._c, name)

        def __iter__(self):
            return iter(self._c)

    def _spy_cursor(*args, **kwargs):
        return _SpyCursor(original_cursor(*args, **kwargs))

    class _ConnProxy:
        """Delegate to the real connection but hand out spying cursors.

        CPython's sqlite3.Connection.cursor is a read-only C method and cannot
        be monkeypatched by attribute assignment, so the connection is wrapped
        instead. row_factory get/set is forwarded so the adapter's column-name
        access keeps working.
        """

        def __init__(self, real_conn):
            self._conn = real_conn

        def cursor(self, *args, **kwargs):
            return _spy_cursor(*args, **kwargs)

        @property
        def row_factory(self):
            return self._conn.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._conn.row_factory = value

        def __getattr__(self, name):
            return getattr(self._conn, name)

    adapter = JlcpartsAdapter(_ConnProxy(conn))
    # Consume all rows.
    all_parts = list(adapter.iter_parts())

    assert not fetchall_was_called, (
        "iter_parts() called fetchall() on a cursor. This is forbidden because "
        "fetchall() loads all rows into memory at once, which is unsafe for the "
        "full jlcparts DB (~600k components). Use fetchmany() or iterate the cursor."
    )
    # Basic sanity: we still got all the rows despite not using fetchall.
    assert len(all_parts) == 10, (
        f"Expected 10 parts via fetchmany iteration, got {len(all_parts)}"
    )


# ---------------------------------------------------------------------------
# T-ADAPT-identifier-safety (C3) -- column name validation before SQL use
# ---------------------------------------------------------------------------

def test_adapt_identifier_safety_malicious_column_name_not_interpolated() -> None:
    """Given a DB whose components table has an extra column with a malicious
    name (containing SQL injection characters, built at runtime via CREATE TABLE
    with a quoted identifier).
    When JlcpartsAdapter is constructed and iter_parts() is iterated.
    Then the adapter either:
      (a) ignores the malicious column silently (it is not in the safe allowlist), OR
      (b) raises a clear validation error.
    And it NEVER interpolates the malicious name into SQL in a way that produces
    an OperationalError from malformed generated SQL.

    Security: column names must be validated against ^[A-Za-z_][A-Za-z0-9_]*$
    before any dynamic SQL interpolation.
    """
    # Build the malicious column name at runtime using chr() to avoid source-level
    # scanner false positives.
    # Target: 'bad"; --col'
    quote = chr(34)       # "
    semicolon = chr(59)   # ;
    dash = chr(45)        # -
    malicious_name = f"bad{quote}{semicolon} {dash}{dash}col"

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Use denormalized schema (Strategy A) so the test doesn't depend on having
    # manufacturers/categories tables for detection.  The malicious column is an
    # extra column alongside the safe schema.
    escaped_identifier = malicious_name.replace(quote, quote + quote)
    conn.execute(f"""
        CREATE TABLE components (
            lcsc        TEXT PRIMARY KEY,
            mpn         TEXT,
            manufacturer TEXT DEFAULT 'Mfr',
            category    TEXT DEFAULT 'IC',
            subcategory TEXT DEFAULT 'Sub',
            description TEXT DEFAULT 'desc',
            package     TEXT DEFAULT 'SOP-8',
            datasheet   TEXT DEFAULT 'https://x.com',
            stock       INTEGER DEFAULT 0,
            price       TEXT DEFAULT '[]',
            is_basic    INTEGER DEFAULT 0,
            extra       TEXT DEFAULT '{{}}',
            {quote}{escaped_identifier}{quote} TEXT DEFAULT 'evil'
        )
    """)
    conn.execute(
        "INSERT INTO components (lcsc, mpn) VALUES (?, ?)",
        ("C99", "SAFEPART"),
    )
    conn.commit()

    # Verify that the malicious column was actually created in the DB.
    col_names = [
        row[1] for row in conn.execute("PRAGMA table_info(components)")
    ]
    assert malicious_name in col_names, (
        f"Test setup error: malicious column {malicious_name!r} was not created. "
        f"Found columns: {col_names}"
    )

    # The adapter must not crash with an OperationalError from malformed SQL.
    try:
        adapter = JlcpartsAdapter(conn)
        parts = list(adapter.iter_parts())
        # If no exception: the adapter successfully ignored the malicious column.
        assert len(parts) >= 1, (
            "Adapter must still yield rows even when a malicious column is present."
        )
        # Verify that the malicious name did not make it into any StagedPart attribute.
        for part in parts:
            for attr in part.attributes:
                assert malicious_name not in attr.name, (
                    f"Malicious column name {malicious_name!r} must not appear "
                    f"as an attribute name in the yielded StagedPart."
                )
    except sqlite3.OperationalError as exc:
        pytest.fail(
            f"Adapter produced a malformed SQL OperationalError, indicating the "
            f"malicious column name was interpolated directly into SQL without "
            f"validation. Error: {exc}\n"
            f"Column names found in table: {col_names}"
        )
    except (ValueError, RuntimeError, TypeError) as exc:
        # A non-sqlite3 exception from identifier validation is acceptable.
        msg = str(exc).lower()
        assert any(kw in msg for kw in ("column", "identifier", "invalid", "unsafe", "schema")), (
            f"Exception raised for malicious column must describe the validation "
            f"failure clearly. Got: {exc!r}"
        )
