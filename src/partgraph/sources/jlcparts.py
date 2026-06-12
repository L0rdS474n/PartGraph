"""Read-only adapter over the JLCPCB/LCSC ``components`` SQLite database.

The on-disk schema varies between distributions, so :class:`JlcpartsAdapter`
introspects the ``components`` table at construction time and selects one of two
strategies:

- **Strategy A (denormalized):** ``manufacturer`` and ``category`` are TEXT
  columns directly on ``components``; ``mpn`` is a TEXT column and ``lcsc`` is
  already a canonical ``"C1234"`` string. Retained for backward compatibility
  with older distributions.
- **Strategy B (FK-joined) — the REAL CDFER shape:** ``manufacturer_id`` /
  ``category_id`` integer FKs reference separate ``manufacturers`` /
  ``categories`` tables; ``lcsc`` is an INTEGER rendered as ``"C{lcsc}"``; the
  MPN lives in the confusingly named ``mfr`` column (with the top-level ``mpn``
  key of the ``extra`` JSON as a fallback when ``mfr`` is empty); ``basic``
  (0/1) maps to ``is_basic``; categories expose ``category`` (L1) and
  ``subcategory`` (L2) columns — there is no ``categories.name``.

Security: every identifier discovered via introspection is validated against a
strict ``^[A-Za-z_][A-Za-z0-9_]*$`` allowlist, and queries only ever reference a
hard-coded, regex-clean set of known column names. A hostile column such as
``bad"; --col`` is therefore never interpolated into SQL and is silently
ignored. Rows are streamed with ``fetchmany`` (never ``fetchall``) so the
adapter is memory-safe on the full ~600 k-row dataset. The database is opened
read-only (``mode=ro`` URI), the ``extra`` blob is parsed only with
``json.loads`` (never ``eval``), and all lookups are parameterized.

Resilience counters are exposed via :attr:`JlcpartsAdapter.counters`:
``skipped_empty_mpn``, ``corrupt_extra`` and ``fk_orphan``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from partgraph.normalize.model import (
    AttrRecord,
    StagedPart,
    make_xid,
    normalize_mfr,
    normalize_mpn,
)

__all__ = ["JlcpartsAdapter", "open_jlcparts_db"]

# Strict SQL identifier allowlist. Anything failing this is never interpolated.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Hard-coded, trusted column names shared by both strategies. Each is regex-clean
# by construction; the adapter only ever SELECTs from the intersection of these
# (plus the strategy-specific columns below) with the columns actually present.
_COMMON_COLUMNS = (
    "lcsc",
    "description",
    "package",
    "datasheet",
    "stock",
    "price",
    "extra",
)

# Strategy A (denormalized): MPN/is_basic live directly on the row; manufacturer
# and category are TEXT columns; subcategory may be present too.
_STRATEGY_A_COLUMNS = ("mpn", "is_basic", "manufacturer", "category", "subcategory")
_STRATEGY_A_REQUIRED = ("mpn",)
_STRATEGY_A_EXTRA = ("manufacturer", "category")

# Strategy B (FK-joined, real CDFER): MPN comes from ``mfr`` (with extra.mpn
# fallback); ``basic`` is the is_basic flag; manufacturer/category resolve via
# integer FKs into the manufacturers/categories tables.
_STRATEGY_B_COLUMNS = ("mfr", "basic", "manufacturer_id", "category_id")
_STRATEGY_B_REQUIRED = ("mfr",)
_STRATEGY_B_EXTRA = ("manufacturer_id", "category_id")

# How many component rows to pull per fetchmany() round-trip.
_FETCH_BATCH = 1000

# A structured attribute value entry is a ``[number, unit]`` pair.
_VALUE_UNIT_PAIR_LEN = 2


def open_jlcparts_db(path: str | Path) -> sqlite3.Connection:
    """Open the jlcparts SQLite database at *path* in read-only mode.

    Uses a ``file:...?mode=ro`` URI so the adapter can never mutate the source
    data file: any write attempt raises :class:`sqlite3.OperationalError`.

    Args:
        path: Filesystem path to the SQLite database.

    Returns:
        A read-only :class:`sqlite3.Connection` with a ``sqlite3.Row`` row
        factory for column-name access.
    """
    uri = f"file:{Path(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_columns(conn: sqlite3.Connection) -> set[str]:
    """Return the set of ``components`` column names that pass the allowlist.

    Iterates ``PRAGMA table_info`` row-by-row (no fetchall) and drops any
    column whose name does not match :data:`_SAFE_IDENTIFIER_RE` — those names
    are never used in generated SQL.
    """
    cols: set[str] = set()
    # PRAGMA table_info returns rows: (cid, name, type, notnull, dflt, pk).
    for row in conn.execute("PRAGMA table_info(components)"):
        name = row[1]
        if isinstance(name, str) and _SAFE_IDENTIFIER_RE.match(name):
            cols.add(name)
    return cols


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if a table named *table* exists (parameterized lookup)."""
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    return cur.fetchone() is not None


def _coerce_float(value: Any) -> float | None:
    """Return *value* as a float if it is a number or numeric string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _price_to_usd(raw: Any) -> float | None:
    """Best-effort extraction of a unit price (USD) from the price column.

    The jlcparts ``price`` column is a JSON array of tier dicts. This returns
    the first available numeric price, or ``None`` when absent/unparseable.
    Never raises.
    """
    direct = _coerce_float(raw)
    if direct is not None:
        return direct
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    for tier in data:
        if not isinstance(tier, dict):
            continue
        for key in ("price", "Price", "usd"):
            price = _coerce_float(tier.get(key))
            if price is not None:
                return price
    return None


def _attr_from_structured(name: str, payload: dict[str, Any]) -> AttrRecord:
    """Build an :class:`AttrRecord` from the structured JLC attribute form.

    Structured form: ``{"primary": "10kΩ", "values": {"<k>": [num, unit]}}``.
    The first ``values`` entry supplies ``value_num`` / ``unit``; ``primary``
    (when present) supplies ``value_text``.
    """
    value_text = payload.get("primary")
    value_text = str(value_text) if value_text is not None else None
    value_num: float | None = None
    unit: str | None = None

    values = payload.get("values")
    if isinstance(values, dict):
        # Prefer the entry keyed by the attribute name; otherwise take the first.
        entry = values.get(name)
        if entry is None and values:
            entry = next(iter(values.values()))
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
            num_candidate = entry[0]
            if isinstance(num_candidate, (int, float)):
                value_num = float(num_candidate)
            elif isinstance(num_candidate, str):
                try:
                    value_num = float(num_candidate)
                except ValueError:
                    value_num = None
            if len(entry) >= _VALUE_UNIT_PAIR_LEN and entry[1] is not None:
                unit = str(entry[1])

    return AttrRecord(name=name, value_text=value_text, value_num=value_num, unit=unit)


class JlcpartsAdapter:
    """Adapter that yields :class:`StagedPart`-shaped rows from a jlcparts DB.

    Construction introspects the ``components`` table and selects a strategy.
    If neither denormalized (Strategy A) nor FK-joined (Strategy B) shape
    matches, a :class:`ValueError` listing the discovered columns is raised.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Column-name access regardless of how the connection was created.
        self._conn.row_factory = sqlite3.Row
        self.counters: dict[str, int] = {
            "skipped_empty_mpn": 0,
            "corrupt_extra": 0,
            "fk_orphan": 0,
        }

        present = _safe_columns(conn)
        if "lcsc" not in present:
            raise ValueError(
                "Unrecognized jlcparts schema: the 'components' table is missing "
                f"the required 'lcsc' column. Found columns: {sorted(present)}."
            )

        # Strategy selection. Strategy A (denormalized) is matched first because
        # its TEXT manufacturer/category columns and 'mpn'/'is_basic' columns are
        # unambiguous. Strategy B requires the FK columns AND the joined tables so
        # an FK-shaped components table without its lookup tables is rejected
        # rather than silently producing orphan rows.
        if set(_STRATEGY_A_REQUIRED).issubset(present) and set(
            _STRATEGY_A_EXTRA
        ).issubset(present):
            self._strategy = "A"
        elif (
            set(_STRATEGY_B_REQUIRED).issubset(present)
            and set(_STRATEGY_B_EXTRA).issubset(present)
            and _table_exists(conn, "manufacturers")
            and _table_exists(conn, "categories")
        ):
            self._strategy = "B"
        else:
            raise ValueError(
                "Unrecognized jlcparts 'components' schema shape: expected either "
                "denormalized 'mpn' + 'manufacturer'/'category' TEXT columns "
                "(Strategy A) or 'mfr' + 'manufacturer_id'/'category_id' FK columns "
                "with 'manufacturers'/'categories' tables (Strategy B). "
                f"Found columns: {sorted(present)}."
            )

        self._present = present
        strategy_columns = (
            _STRATEGY_A_COLUMNS if self._strategy == "A" else _STRATEGY_B_COLUMNS
        )
        self._select_columns = [
            c for c in (*_COMMON_COLUMNS, *strategy_columns) if c in present
        ]
        # Defense in depth: never build SQL from a non-conforming identifier.
        for col in self._select_columns:
            if not _SAFE_IDENTIFIER_RE.match(col):  # pragma: no cover — literals only
                raise ValueError(f"Unsafe column identifier rejected: {col!r}")

    def _build_query(self) -> str:
        """Return the hard-coded SELECT for the active strategy (safe columns)."""
        common = ", ".join(f"c.{col}" for col in self._select_columns)
        if self._strategy == "A":
            return f"SELECT {common} FROM components AS c"
        # Strategy B: LEFT JOIN so orphan FKs resolve to NULL names. categories
        # exposes 'category' (L1) and 'subcategory' (L2) — never a 'name' column.
        joins = (
            "m.name AS mfr_name, "
            "cat.category AS cat_name, "
            "cat.subcategory AS cat_subcategory"
        )
        return (
            f"SELECT {common}, {joins} "
            "FROM components AS c "
            "LEFT JOIN manufacturers AS m ON c.manufacturer_id = m.id "
            "LEFT JOIN categories AS cat ON c.category_id = cat.id"
        )

    def _row_value(self, row: sqlite3.Row, key: str) -> Any:
        """Return ``row[key]`` if the column was selected, else ``None``."""
        if key in row.keys():  # noqa: SIM118 — sqlite3.Row has no __contains__
            return row[key]
        return None

    def _decode_extra(self, extra_raw: Any) -> dict[str, Any] | None:
        """Decode the ``extra`` JSON column into a dict.

        Returns the decoded dict, or ``None`` when the column is empty, not
        valid JSON, or not a JSON object. The ``corrupt_extra`` counter is
        incremented when a non-empty value fails to decode to a dict. Never
        raises (json.loads only — never eval).
        """
        if extra_raw is None or extra_raw == "":
            return None
        try:
            decoded = json.loads(extra_raw)
        except (ValueError, TypeError):
            self.counters["corrupt_extra"] += 1
            return None
        if not isinstance(decoded, dict):
            self.counters["corrupt_extra"] += 1
            return None
        return decoded

    def _attributes_from_extra(self, extra: dict[str, Any] | None) -> list[AttrRecord]:
        """Build AttrRecords from an already-decoded ``extra`` dict.

        A ``None`` extra (empty/corrupt/non-dict, already counted by
        :meth:`_decode_extra`) yields ``[]``. Never raises.
        """
        if extra is None:
            return []

        attributes_obj = extra.get("attributes")
        if not isinstance(attributes_obj, dict):
            return []

        records: list[AttrRecord] = []
        for name, payload in attributes_obj.items():
            if not isinstance(name, str):
                continue
            if isinstance(payload, dict):
                records.append(_attr_from_structured(name, payload))
            elif isinstance(payload, str):
                records.append(
                    AttrRecord(name=name, value_text=payload, value_num=None, unit=None)
                )
            # Other shapes (e.g. a bare list) are skipped rather than guessed.
        return records

    def _resolve_mpn(self, row: sqlite3.Row, extra: dict[str, Any] | None) -> str | None:
        """Resolve the MPN string for a row, or ``None`` to skip it.

        Strategy A: the ``mpn`` column. Strategy B: the ``mfr`` column with a
        fallback to the top-level ``"mpn"`` key of the decoded ``extra`` JSON
        when ``mfr`` is empty/whitespace/NULL. A row is skipped (returning
        ``None``) only when no usable MPN can be found from any source.
        """
        if self._strategy == "A":
            primary = self._row_value(row, "mpn")
        else:
            primary = self._row_value(row, "mfr")

        if primary is not None and str(primary).strip():
            return str(primary)

        # Strategy B fallback: extra top-level "mpn".
        if self._strategy == "B" and extra is not None:
            fallback = extra.get("mpn")
            if fallback is not None and str(fallback).strip():
                return str(fallback)

        return None

    def _resolve_lcsc_id(self, row: sqlite3.Row) -> str | None:
        """Render the canonical ``lcsc_id`` string for a row.

        Strategy A stores ``lcsc`` already as the canonical ``"C1234"`` string,
        so it is passed through verbatim. Strategy B stores ``lcsc`` as an
        INTEGER and renders it as ``"C{lcsc}"`` (e.g. 1002 -> "C1002").
        """
        lcsc = self._row_value(row, "lcsc")
        if lcsc is None:
            return None
        if self._strategy == "B":
            return f"C{lcsc}"
        return str(lcsc)

    def _resolve_is_basic(self, row: sqlite3.Row) -> bool:
        """Resolve the ``is_basic`` flag (``is_basic`` col for A, ``basic`` for B)."""
        key = "is_basic" if self._strategy == "A" else "basic"
        return bool(self._row_value(row, key))

    def _resolve_mfr(self, row: sqlite3.Row) -> str | None:
        """Return the manufacturer name, counting orphan FKs (Strategy B)."""
        if self._strategy == "A":
            value = self._row_value(row, "manufacturer")
            return str(value) if value is not None else None
        # Strategy B.
        fk = self._row_value(row, "manufacturer_id")
        name = self._row_value(row, "mfr_name")
        if name is None:
            if fk is not None:
                self.counters["fk_orphan"] += 1
            return None
        return str(name)

    def _resolve_category(self, row: sqlite3.Row) -> str | None:
        """Return the L1 category name, counting orphan FKs (Strategy B).

        Strategy A reads the ``category`` TEXT column. Strategy B reads
        ``categories.category`` (the L1 name) via the ``category_id`` FK — never
        a non-existent ``categories.name`` column.
        """
        if self._strategy == "A":
            value = self._row_value(row, "category")
            return str(value) if value is not None else None
        # Strategy B.
        fk = self._row_value(row, "category_id")
        name = self._row_value(row, "cat_name")
        if name is None:
            if fk is not None:
                self.counters["fk_orphan"] += 1
            return None
        return str(name)

    def _resolve_subcategory(self, row: sqlite3.Row) -> str | None:
        """Return the L2 subcategory name.

        Strategy A reads the optional ``subcategory`` TEXT column. Strategy B
        reads ``categories.subcategory`` via the ``category_id`` FK. An orphan
        category FK is already counted by :meth:`_resolve_category`; this method
        simply returns ``None`` for it without double-counting.
        """
        if self._strategy == "A":
            value = self._row_value(row, "subcategory")
            return str(value) if value is not None else None
        # Strategy B.
        value = self._row_value(row, "cat_subcategory")
        return str(value) if value is not None else None

    def iter_parts(self) -> Iterator[StagedPart]:
        """Yield one :class:`StagedPart` per usable ``components`` row.

        Rows with no usable MPN (empty/whitespace/NULL primary column AND, for
        Strategy B, no ``extra.mpn`` fallback) are skipped and counted under
        ``skipped_empty_mpn``. Iteration uses ``fetchmany`` so memory stays
        bounded on the full dataset.
        """
        query = self._build_query()
        cursor = self._conn.cursor()
        cursor.execute(query)
        while True:
            rows = cursor.fetchmany(_FETCH_BATCH)
            if not rows:
                break
            for row in rows:
                part = self._row_to_part(row)
                if part is not None:
                    yield part

    def _row_to_part(self, row: sqlite3.Row) -> StagedPart | None:
        """Convert a single DB row into a StagedPart, or ``None`` to skip it."""
        # Decode extra once: it is needed for both the MPN fallback (Strategy B)
        # and attribute extraction, and decoding it twice would double-count
        # corrupt_extra.
        extra = self._decode_extra(self._row_value(row, "extra"))

        mpn = self._resolve_mpn(row, extra)
        if mpn is None:
            self.counters["skipped_empty_mpn"] += 1
            return None

        mfr_name = self._resolve_mfr(row)
        category = self._resolve_category(row)
        subcategory = self._resolve_subcategory(row)
        mpn_norm = normalize_mpn(mpn)
        mfr_norm = normalize_mfr(mfr_name or "")

        description = self._row_value(row, "description")
        package = self._row_value(row, "package")
        datasheet = self._row_value(row, "datasheet")
        stock = self._row_value(row, "stock")
        lcsc_id = self._resolve_lcsc_id(row)
        is_basic = self._resolve_is_basic(row)
        price_usd = _price_to_usd(self._row_value(row, "price"))
        attributes = self._attributes_from_extra(extra)

        return StagedPart(
            mpn=mpn,
            mpn_norm=mpn_norm,
            mfr_name=mfr_name,
            mfr_norm=mfr_norm,
            xid=make_xid(mpn_norm, mfr_norm),
            description=str(description) if description is not None else None,
            package=str(package) if package is not None else None,
            category=category,
            subcategory=subcategory,
            datasheet_url=str(datasheet) if datasheet is not None else None,
            lcsc_id=lcsc_id,
            stock=int(stock) if isinstance(stock, (int, float)) else None,
            price_usd=price_usd,
            is_basic=is_basic,
            promoted={},
            attributes=attributes,
            tags=[],
            source_ref="",
        )
