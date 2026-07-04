"""
Tests: AC-RS-5..11, AC-RS-13, AC-RS-15, AC-RS-16 — partgraph.refresh.stock (leaf logic)

Specifies the behaviour of the NEW leaf module ``partgraph.refresh.stock``,
which underpins the `partgraph refresh` CLI command (issue #11, PR 2:
stock/price refresh). Mirrors the link-rot checker's
(``partgraph.refresh.links``) leaf discipline (ADR-0010): this leaf depends
only on the injected ``client`` (pydgraph) / ``clock`` seams — never a real
Dgraph connection or the real wall clock — and never opens a socket (there is
no HTTP surface at all in this leaf: the JLC stock/price data is already
loaded, in full, into an in-memory ``stock_index`` dict BEFORE this module is
called; see test_cli_refresh_stock.py for how that index is built once and
reused across every page).

Contract pinned by this file (leaf module ``partgraph.refresh.stock``):
  - ``build_stock_index(parts_iter) -> dict[str, tuple[int | None, float |
    None, bool]]`` — joins an iterable of JLC-source-shaped rows (exposing
    ``lcsc_id``/``stock``/``price_usd``/``is_basic``, e.g. a
    :class:`partgraph.normalize.model.StagedPart` or an equivalent
    ``SimpleNamespace``) into a ``lcsc_id -> (stock, price_usd, is_basic)``
    lookup dict. A row with ``lcsc_id is None`` is skipped; a duplicate
    ``lcsc_id`` is last-wins (source-file row order determines the winner).
  - ``refresh_stock_write(parts_iter, client, *, stock_index, clock) -> dict``
    — the main leaf entry point. ``parts_iter`` yields GRAPH Part rows (uid +
    lcsc_id, as selected by the CLI's ``_select_parts_for_refresh``); for each,
    a lookup into the (pre-built, externally-injected) ``stock_index`` decides
    whether the row is "matched" (a full stock/price/is_basic write-back plus
    the freshness stamp) or "absent" (a stamp-only write-back that leaves the
    volatile stock/price/is_basic fields completely untouched). All rows for
    one call are written back in ONE committed transaction. Returns EXACTLY
    ``{"checked": int, "matched": int, "absent": int}`` (AC-RS-16).
  - ``format_checked_at(moment: datetime) -> str`` — deterministic RFC-3339
    UTC ('Z'-suffixed) string for a given (injected) datetime, mirroring
    ``partgraph.refresh.links.format_verified_at``'s convention exactly (but
    NOT imported from it — the two leaves stay decoupled; see the structural
    isolation tests below).

Idempotency (AC-RS-13) split across files, mirroring how AC-RL-13 splits
across test_refresh_links.py / test_cli_refresh_links.py: this file pins the
LEAF's own defensive contribution — a uid appearing twice within a single
``refresh_stock_write`` call is processed (checked/written) exactly once. The
cross-run "a freshly-stamped part is skipped on the next run within
--stale-days" guarantee is a CLI-level selection-query concern (the
``stock_checked_at`` staleness filter built by ``_select_parts_for_refresh``)
and is pinned in test_cli_refresh_stock.py instead.

AC-RS-15's structural-isolation guarantee is likewise split across files: this
file pins the LEAF's own contribution — ``refresh_stock_write``'s own source
never references any embed/refresh-links/loader helper. The CLI-level half
(``_select_parts_for_refresh`` / ``_refresh_stock_all_pages`` source purity,
plus the cross-PR constant regression anchors) is pinned in
test_cli_refresh_stock.py.

Fidelity (mandate): rather than a hand-rolled fake dict feeding
``build_stock_index`` for every test (used for the leaf's pure-logic tests
below, since sqlite is irrelevant to those), exactly ONE dedicated test in
this file builds a REAL ``sqlite3.connect(":memory:")`` database with a tiny
``components`` table shaped exactly like jlcparts.py's Strategy A (denormalized)
schema, runs it through the REAL, already-shipped
:class:`partgraph.sources.jlcparts.JlcpartsAdapter`, and feeds the resulting
:class:`~partgraph.normalize.model.StagedPart` stream into
``build_stock_index`` — proving the leaf's reuse of the existing adapter
actually parses correctly end to end (no 1 GB file, no committed binary; see
``test_fidelity_...`` below).

Gate 3 (security + architecture review, test-contract stage) — SHOULD
hardenings added test-first, RED against the not-yet-written impl:
  - AC-RS-10 extension (security, upper-bound sanity): an absurd-but-finite
    stock/price (above new leaf constants ``_MAX_SANE_STOCK`` /
    ``_MAX_SANE_PRICE_USD``) must have that volatile OMITTED, exactly like
    the existing negative/non-finite cases — a corrupt or overflowed source
    field must not be written through as if it were a real quantity. Tests
    bind to the constants (imported via module-qualified access, mirroring
    the AC-RS-15 anchor pattern) rather than hardcoding the magic numbers,
    plus boundary tests pinning '>' (strictly greater than) semantics: a
    value exactly AT the ceiling is still valid and still written.
  - is_basic bool-coercion (security): ``build_stock_index`` fed a raw int
    (``1``) or a raw non-empty string (``"true"``) for is_basic must
    normalize to the canonical ``True``/``False`` singleton by IDENTITY
    (``is True``/``is False``), never merely a truthy raw value — pins the
    leaf's own advertised duck-typed row contract rather than relying on the
    one trusted ``JlcpartsAdapter`` caller (which already yields a proper
    bool) to be the only caller that ever exists.
  - Module-wide import isolation (architecture): extends the AC-RS-8-style
    whole-module ``inspect.getsource(stock_mod)`` scan (which already
    forbids ``partgraph.normalize``) to ALSO forbid ``"pydgraph"``,
    ``"httpx"``, ``"Loader"``, ``"embed_write"`` and ``"refresh_links_write"``
    ANYWHERE in the module source — not just inside ``refresh_stock_write``'s
    own body, which the narrower AC-RS-15 structural test already covers. A
    stray top-level ``import pydgraph``/``import httpx`` or a *helper*
    function (as opposed to ``refresh_stock_write`` itself) reaching into a
    sibling pipeline would slip past the narrower, per-function check alone.

NOTE: Collection will ERROR because partgraph.refresh.stock does not yet
exist. That is the expected RED state before implementation.
"""

from __future__ import annotations

import inspect
import json
import math
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# --- Module under test (will be red until implementation exists) ---
from partgraph.refresh.stock import (  # noqa: F401
    build_stock_index,
    format_checked_at,
    refresh_stock_write,
)

# ---------------------------------------------------------------------------
# Fixed test instant (deliberately NOT "now" in any real sense, so a test that
# accidentally reads the real wall clock fails an exact-match assertion instead
# of passing by coincidence). Deliberately distinct from test_refresh_links.py's
# fixed moment (2030-01-15) and test_cli_refresh_stock.py's own fixed "now"
# (2031-09-09) so a failure is never ambiguous about which file/seam it came
# from.
# ---------------------------------------------------------------------------

_FIXED_MOMENT = datetime(2031, 3, 3, 15, 45, 0, tzinfo=UTC)
_FIXED_MOMENT_STR = "2031-03-03T15:45:00Z"


def _fixed_clock() -> datetime:
    return _FIXED_MOMENT


# ---------------------------------------------------------------------------
# Fake pydgraph client/txn builders (mirrors test_refresh_links.py's
# _make_txn / _make_simple_client / _written_payload_items exactly).
# ---------------------------------------------------------------------------

def _make_txn() -> MagicMock:
    txn = MagicMock()
    txn.discard.return_value = None
    txn.commit.return_value = None
    txn.mutate.return_value = MagicMock()
    txn.__enter__ = MagicMock(return_value=txn)
    txn.__exit__ = MagicMock(return_value=False)
    return txn


def _make_simple_client(write_txn: MagicMock) -> MagicMock:
    """Return a mock client where every client.txn() call returns *write_txn*."""
    client = MagicMock()
    client.txn.return_value = write_txn
    return client


def _written_payload_items(write_txn: MagicMock) -> list[dict]:
    """Flatten every set_obj payload item written via *write_txn*.mutate()."""
    items: list[dict] = []
    for call_obj in write_txn.mutate.call_args_list:
        _, kwargs = call_obj
        set_obj = kwargs.get("set_obj")
        if set_obj is None:
            continue
        items.extend(set_obj if isinstance(set_obj, list) else [set_obj])
    return items


# ---------------------------------------------------------------------------
# Fake graph-Part row builder (uid + lcsc_id only — the shape
# _select_parts_for_refresh yields; see test_cli_refresh_stock.py AC-RS-3).
# ---------------------------------------------------------------------------

def _make_part_row(*, uid: str = "0xP001", lcsc_id: str | None = "C1") -> SimpleNamespace:
    return SimpleNamespace(uid=uid, lcsc_id=lcsc_id)


# ===========================================================================
# AC-RS-5: build_stock_index (dict join over the JLC source rows)
# ===========================================================================

def test_ac_rs_5_build_stock_index_maps_lcsc_id_to_stock_price_is_basic_tuple() -> None:
    """AC-RS-5: Given an iterable of JLC-source-shaped rows, each exposing
    lcsc_id/stock/price_usd/is_basic.
    When build_stock_index is called.
    Then it returns a dict mapping each lcsc_id to the exact
    (stock, price_usd, is_basic) tuple.
    """
    rows = [
        SimpleNamespace(lcsc_id="C1", stock=10, price_usd=1.0, is_basic=True),
        SimpleNamespace(lcsc_id="C2", stock=20, price_usd=2.0, is_basic=False),
    ]
    index = build_stock_index(iter(rows))
    assert index == {"C1": (10, 1.0, True), "C2": (20, 2.0, False)}, (
        f"AC-RS-5: expected a lcsc_id -> (stock, price_usd, is_basic) dict. "
        f"Got: {index!r}"
    )


def test_ac_rs_5_build_stock_index_skips_rows_with_lcsc_id_none() -> None:
    """AC-RS-5: Given one row whose lcsc_id is None (no LCSC identity — the
    JLC source can carry rows with no lcsc, e.g. a corrupt/incomplete record).
    When build_stock_index is called.
    Then that row is skipped entirely (never keyed under a literal `None`).
    """
    rows = [
        SimpleNamespace(lcsc_id=None, stock=10, price_usd=1.0, is_basic=True),
        SimpleNamespace(lcsc_id="C2", stock=20, price_usd=2.0, is_basic=False),
    ]
    index = build_stock_index(iter(rows))
    assert index == {"C2": (20, 2.0, False)}, (
        f"AC-RS-5: a None lcsc_id must be skipped. Got: {index!r}"
    )
    assert None not in index


def test_ac_rs_5_build_stock_index_last_wins_on_duplicate_lcsc_id() -> None:
    """AC-RS-5: Given two rows sharing the SAME lcsc_id with different
    stock/price/is_basic values.
    When build_stock_index is called.
    Then the LAST row's values win (later source-file rows supersede earlier
    ones for the same LCSC identity).
    """
    rows = [
        SimpleNamespace(lcsc_id="C1", stock=10, price_usd=1.0, is_basic=True),
        SimpleNamespace(lcsc_id="C1", stock=999, price_usd=9.99, is_basic=False),
    ]
    index = build_stock_index(iter(rows))
    assert index == {"C1": (999, 9.99, False)}, (
        f"AC-RS-5: the LAST duplicate row must win. Got: {index!r}"
    )


@pytest.mark.parametrize(
    ("raw_is_basic", "expected"),
    [
        pytest.param(1, True, id="int_one_coerces_to_true"),
        pytest.param("true", True, id="nonempty_string_coerces_to_true"),
        pytest.param(0, False, id="int_zero_coerces_to_false"),
    ],
)
def test_gate3_build_stock_index_coerces_is_basic_to_strict_bool_identity(
    raw_is_basic: object, expected: bool
) -> None:
    """Gate 3 hardening (security — bool-coercion, not merely truthiness):
    Given a JLC-source-shaped row whose is_basic is a raw int (1), a raw
    non-empty string ("true"), or a raw int (0) — duck-typed inputs a caller
    OTHER than the one trusted JlcpartsAdapter (which already yields a
    proper bool) might still hand build_stock_index.
    When build_stock_index is called.
    Then the resulting tuple's third element is STRICTLY `is True`/`is
    False` (identity against the bool singleton) — never the raw int/str
    value itself, which would be merely truthy/falsy (e.g. `1 is not True`
    and `"true" is not True` in Python) rather than the canonical bool. Pins
    the leaf's own advertised duck-typed contract (AC-RS-8) rather than
    relying on the one trusted adapter caller to always pre-coerce.
    """
    row = SimpleNamespace(
        lcsc_id="C-coerce", stock=10, price_usd=1.0, is_basic=raw_is_basic
    )
    index = build_stock_index(iter([row]))
    _, _, is_basic = index["C-coerce"]

    if expected:
        assert is_basic is True, (
            f"Gate 3: is_basic={raw_is_basic!r} must coerce to the "
            f"canonical True singleton (identity), not merely a truthy "
            f"value. Got: {is_basic!r} ({type(is_basic)})"
        )
    else:
        assert is_basic is False, (
            f"Gate 3: is_basic={raw_is_basic!r} must coerce to the "
            f"canonical False singleton (identity), not merely a falsy "
            f"value. Got: {is_basic!r} ({type(is_basic)})"
        )


# ===========================================================================
# Fidelity test (mandate): real sqlite3(":memory:") + the REAL JlcpartsAdapter
# ===========================================================================

def test_fidelity_build_stock_index_from_real_jlcparts_adapter_strategy_a() -> None:
    """Fidelity (mandate — no 1 GB file, no committed binary): Given a tiny,
    in-memory sqlite3 ``components`` table shaped exactly like jlcparts.py's
    Strategy A (denormalized) schema — the same column set
    test_jlcparts_adapter.py's own ``_build_denormalized_db`` helper uses
    (lcsc, mpn, manufacturer, category, subcategory, description, package,
    datasheet, stock, price, is_basic, extra) — carrying ONE realistic row
    (lcsc="C100", stock=500, a JSON price-tier array yielding 1.2345 USD,
    is_basic=1).
    When the REAL, already-shipped partgraph.sources.jlcparts.JlcpartsAdapter
    parses it and its StagedPart stream is fed into build_stock_index.
    Then the resulting index is EXACTLY {"C100": (500, 1.2345, True)} —
    proving build_stock_index's reuse of the adapter's StagedPart shape
    (lcsc_id/stock/price_usd/is_basic) parses correctly end to end, not just
    against a hand-constructed SimpleNamespace fake.
    """
    from partgraph.sources.jlcparts import JlcpartsAdapter

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
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
        """
    )
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
            "lcsc": "C100",
            "mpn": "TESTPART100",
            "manufacturer": "TestMfr",
            "category": "IC",
            "subcategory": "Logic",
            "description": "Test part",
            "package": "SOP-8",
            "datasheet": "https://example.com/ds.pdf",
            "stock": 500,
            "price": json.dumps([{"price": 1.2345}]),
            "is_basic": 1,
            "extra": "{}",
        },
    )
    conn.commit()

    adapter = JlcpartsAdapter(conn)
    index = build_stock_index(adapter.iter_parts())

    assert index == {"C100": (500, 1.2345, True)}, (
        f"Fidelity: a real JlcpartsAdapter (Strategy A) parse must feed "
        f"build_stock_index correctly. Got: {index!r}"
    )


# ===========================================================================
# AC-RS-6: matched write-back shape
# ===========================================================================

def test_ac_rs_6_matched_payload_shape_exact_keys_and_values() -> None:
    """AC-RS-6: Given a graph Part (uid="0xP1", lcsc_id="C100") and a
    stock_index mapping "C100" -> (500, 1.23, True).
    When refresh_stock_write processes it.
    Then the written payload item is EXACTLY
    {uid, stock, price_usd, is_basic, stock_checked_at}: uid == "0xP1"
    (never a blank node), stock == 500, price_usd == 1.23, is_basic is True —
    the exact-set-equality check below also proves no mpn/xid/description/
    edge/embedding key is ever present.
    """
    row = _make_part_row(uid="0xP1", lcsc_id="C100")
    stock_index = {"C100": (500, 1.23, True)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    assert len(items) == 1
    item = items[0]
    assert set(item.keys()) == {
        "uid", "stock", "price_usd", "is_basic", "stock_checked_at",
    }, (
        f"AC-RS-6: matched payload must have EXACTLY "
        f"{{uid, stock, price_usd, is_basic, stock_checked_at}} — no mpn/xid/"
        f"description/edge/embedding key. Got: {item!r}"
    )
    assert item["uid"] == "0xP1"
    assert not item["uid"].startswith("_:"), (
        f"AC-RS-6: uid must never be a blank node. Got: {item['uid']!r}"
    )
    assert item["stock"] == 500
    assert item["price_usd"] == 1.23
    assert item["is_basic"] is True
    assert item["stock_checked_at"] == _FIXED_MOMENT_STR
    assert summary == {"checked": 1, "matched": 1, "absent": 0}, (
        f"AC-RS-16: expected checked=1 matched=1 absent=0. Got: {summary!r}"
    )


# ===========================================================================
# AC-RS-7: absent write-back shape (stamp-only)
# ===========================================================================

def test_ac_rs_7_absent_payload_shape_stamp_only_volatile_untouched() -> None:
    """AC-RS-7: Given a graph Part whose lcsc_id ("C404") is NOT present in
    the stock_index (no matching JLC source row this run).
    When refresh_stock_write processes it.
    Then the written payload item is EXACTLY {uid, stock_checked_at} — a
    stamp-only write that leaves stock/price_usd/is_basic (the volatile
    fields) completely untouched — and the row is counted 'absent'.
    """
    row = _make_part_row(uid="0xP2", lcsc_id="C404")
    stock_index = {"C100": (500, 1.23, True)}  # "C404" deliberately absent.
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    assert len(items) == 1
    item = items[0]
    assert set(item.keys()) == {"uid", "stock_checked_at"}, (
        f"AC-RS-7: absent payload must be stamp-only EXACTLY "
        f"{{uid, stock_checked_at}} — volatile fields untouched. Got: {item!r}"
    )
    assert item["uid"] == "0xP2"
    assert item["stock_checked_at"] == _FIXED_MOMENT_STR
    assert summary == {"checked": 1, "matched": 0, "absent": 1}, (
        f"AC-RS-16: expected checked=1 matched=0 absent=1. Got: {summary!r}"
    )


# ===========================================================================
# AC-RS-8: injected clock -> deterministic RFC-3339 UTC stock_checked_at
# ===========================================================================

def test_ac_rs_8_format_checked_at_matches_fixed_moment_exactly() -> None:
    """AC-RS-8: Given a fixed, injected UTC datetime.
    When format_checked_at(moment) is called.
    Then the result is EXACTLY the expected RFC-3339 'Z'-suffixed string —
    proving no real wall-clock component leaks in.
    """
    assert format_checked_at(_FIXED_MOMENT) == _FIXED_MOMENT_STR, (
        f"AC-RS-8: format_checked_at must produce the exact RFC-3339 string "
        f"for the injected moment. Got: {format_checked_at(_FIXED_MOMENT)!r}"
    )


def test_ac_rs_8_end_to_end_stock_checked_at_uses_injected_clock_only() -> None:
    """AC-RS-8: Given refresh_stock_write is called with an injected fixed
    clock callable (returning _FIXED_MOMENT, never real time), for a mix of
    one matched and one absent row.
    Then EVERY written payload item's 'stock_checked_at' equals the exact
    deterministic string derived from that fixed moment — matched and absent
    rows alike, proving no real wall-clock value ever leaks in.
    """
    matched_row = _make_part_row(uid="0xC1", lcsc_id="C1")
    absent_row = _make_part_row(uid="0xC2", lcsc_id="C-missing")
    stock_index = {"C1": (10, 1.0, False)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([matched_row, absent_row]), client,
        stock_index=stock_index, clock=_fixed_clock,
    )

    items = _written_payload_items(write_txn)
    assert len(items) == 2
    for item in items:
        assert item["stock_checked_at"] == _FIXED_MOMENT_STR, (
            f"AC-RS-8: every payload item's stock_checked_at must equal the "
            f"injected clock's deterministic string. Got: {item!r}"
        )


def test_ac_rs_8_no_refresh_module_references_partgraph_normalize() -> None:
    """AC-RS-8 (leaf purity): Given the partgraph.refresh.stock module's own
    source code.
    Then it never references partgraph.normalize — the leaf depends only on
    the plain attribute shape (lcsc_id/stock/price_usd/is_basic) of whatever
    row-like object it is handed, never on the normalize package's models or
    helpers, keeping it a true leaf.
    """
    import partgraph.refresh.stock as stock_mod

    source = inspect.getsource(stock_mod)
    assert "partgraph.normalize" not in source, (
        f"AC-RS-8: partgraph.refresh.stock must never reference "
        f"partgraph.normalize. Found a reference in:\n{source}"
    )


def test_gate3_refresh_stock_module_scope_never_imports_pydgraph_httpx_or_forbidden_symbols() -> None:
    """Gate 3 hardening (architecture): Given the partgraph.refresh.stock
    module's own FULL source code (module scope — not just inside
    refresh_stock_write's body, which the narrower AC-RS-15 structural test
    already covers; a stray top-level `import pydgraph`/`import httpx`, or a
    *helper* function reaching into a sibling pipeline, would NOT be caught
    by that narrower per-function check alone).
    Then the module source never contains "pydgraph", "httpx", "Loader",
    "embed_write" or "refresh_links_write" ANYWHERE — the leaf stays thin
    and depends only on its injected client/stock_index/clock seams, never
    importing a concrete Dgraph/HTTP client library or reaching into a
    sibling pipeline itself.
    """
    import partgraph.refresh.stock as stock_mod

    source = inspect.getsource(stock_mod)
    forbidden = ("pydgraph", "httpx", "Loader", "embed_write", "refresh_links_write")
    for name in forbidden:
        assert name not in source, (
            f"Gate 3 (architecture): partgraph.refresh.stock must never "
            f"contain {name!r} anywhere at module scope. Source:\n{source}"
        )


# ===========================================================================
# AC-RS-9: None-valued fields individually omitted; is_basic/stamp always present
# ===========================================================================

@pytest.mark.parametrize(
    ("stock", "price", "is_basic", "expected_keys"),
    [
        pytest.param(
            None, 2.5, True,
            {"uid", "price_usd", "is_basic", "stock_checked_at"},
            id="stock_none",
        ),
        pytest.param(
            10, None, False,
            {"uid", "stock", "is_basic", "stock_checked_at"},
            id="price_none",
        ),
        pytest.param(
            None, None, False,
            {"uid", "is_basic", "stock_checked_at"},
            id="both_none",
        ),
    ],
)
def test_ac_rs_9_none_valued_fields_individually_omitted(
    stock: int | None, price: float | None, is_basic: bool, expected_keys: set[str]
) -> None:
    """AC-RS-9: Given a matched part whose stock and/or price_usd is None.
    When refresh_stock_write writes its payload.
    Then the None-valued field(s) are OMITTED from the payload (never a JSON
    null), while 'is_basic' and 'stock_checked_at' are ALWAYS present.
    """
    row = _make_part_row(uid="0xN1", lcsc_id="C-none")
    stock_index = {"C-none": (stock, price, is_basic)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    assert len(items) == 1
    item = items[0]
    assert set(item.keys()) == expected_keys, (
        f"AC-RS-9: expected exactly {expected_keys!r} (None fields omitted, "
        f"is_basic/stock_checked_at always present). Got: {item!r}"
    )
    assert item["is_basic"] is is_basic
    assert item["stock_checked_at"] == _FIXED_MOMENT_STR


# ===========================================================================
# AC-RS-10: value sanity (negative stock, non-finite/negative price omitted)
# ===========================================================================

def test_ac_rs_10_negative_stock_omitted_price_still_written() -> None:
    """AC-RS-10: Given a matched part whose stock is negative (invalid —
    stock can never be negative) but whose price_usd is a valid non-negative
    finite float.
    When refresh_stock_write writes its payload.
    Then 'stock' is OMITTED while 'price_usd' is still written, and
    'stock_checked_at' is still stamped.
    """
    row = _make_part_row(uid="0xV1", lcsc_id="C-neg-stock")
    stock_index = {"C-neg-stock": (-5, 1.0, True)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    item = items[0]
    assert set(item.keys()) == {"uid", "price_usd", "is_basic", "stock_checked_at"}, (
        f"AC-RS-10: negative stock must be omitted; price_usd/is_basic/"
        f"stock_checked_at still written. Got: {item!r}"
    )
    assert item["price_usd"] == 1.0
    assert item["stock_checked_at"] == _FIXED_MOMENT_STR


def test_ac_rs_10_nan_price_omitted_stock_still_written() -> None:
    """AC-RS-10: Given a matched part whose price_usd is NaN (non-finite,
    invalid) but whose stock is a valid non-negative integer.
    When refresh_stock_write writes its payload.
    Then 'price_usd' is OMITTED while 'stock' is still written.
    """
    row = _make_part_row(uid="0xV2", lcsc_id="C-nan")
    stock_index = {"C-nan": (10, float("nan"), False)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    item = items[0]
    assert set(item.keys()) == {"uid", "stock", "is_basic", "stock_checked_at"}, (
        f"AC-RS-10: NaN price_usd must be omitted; stock still written. "
        f"Got: {item!r}"
    )
    assert item["stock"] == 10


def test_ac_rs_10_positive_infinity_price_omitted() -> None:
    """AC-RS-10: Given a matched part whose price_usd is +inf (non-finite).
    When refresh_stock_write writes its payload.
    Then 'price_usd' is OMITTED.
    """
    row = _make_part_row(uid="0xV3", lcsc_id="C-pinf")
    stock_index = {"C-pinf": (10, float("inf"), False)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    assert "price_usd" not in items[0], (
        f"AC-RS-10: +inf price_usd must be omitted. Got: {items[0]!r}"
    )


def test_ac_rs_10_negative_infinity_price_omitted() -> None:
    """AC-RS-10: Given a matched part whose price_usd is -inf (non-finite).
    When refresh_stock_write writes its payload.
    Then 'price_usd' is OMITTED.
    """
    row = _make_part_row(uid="0xV4", lcsc_id="C-ninf")
    stock_index = {"C-ninf": (10, float("-inf"), False)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    assert "price_usd" not in items[0], (
        f"AC-RS-10: -inf price_usd must be omitted. Got: {items[0]!r}"
    )


def test_ac_rs_10_negative_price_omitted_stock_still_written() -> None:
    """AC-RS-10: Given a matched part whose price_usd is negative (invalid —
    price can never be negative) but whose stock is valid.
    When refresh_stock_write writes its payload.
    Then 'price_usd' is OMITTED while 'stock' is still written.
    """
    row = _make_part_row(uid="0xV5", lcsc_id="C-negprice")
    stock_index = {"C-negprice": (10, -2.5, False)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    item = items[0]
    assert "price_usd" not in item, (
        f"AC-RS-10: negative price_usd must be omitted. Got: {item!r}"
    )
    assert item["stock"] == 10


def test_ac_rs_10_both_stock_and_price_invalid_simultaneously() -> None:
    """AC-RS-10: Given a matched part whose stock is negative AND whose
    price_usd is NaN (both invalid simultaneously).
    When refresh_stock_write writes its payload.
    Then BOTH 'stock' and 'price_usd' are omitted — only 'uid', 'is_basic'
    and 'stock_checked_at' remain (identical shape to AC-RS-9's None/None
    case), proving invalid values are normalized the same way as absent
    values, never smuggled through as NaN/negative numbers.
    """
    row = _make_part_row(uid="0xV6", lcsc_id="C-bothbad")
    stock_index = {"C-bothbad": (-1, float("nan"), True)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    item = items[0]
    assert set(item.keys()) == {"uid", "is_basic", "stock_checked_at"}, (
        f"AC-RS-10: both invalid values must be omitted together. Got: {item!r}"
    )
    assert item["is_basic"] is True


def test_ac_rs_10_no_nan_or_inf_ever_serialized_in_any_payload_value() -> None:
    """AC-RS-10: Given several matched rows carrying every invalid-value shape
    (negative stock, NaN price, +inf price, -inf price, negative price) in a
    SINGLE refresh_stock_write call.
    When the payload is written.
    Then no float value in ANY produced payload item is NaN or infinite —
    invalid values are omitted outright, never smuggled through as a
    non-finite number (which is not valid JSON and would corrupt the
    mutation).
    """
    rows = [
        _make_part_row(uid="0xB1", lcsc_id="C-neg-stock"),
        _make_part_row(uid="0xB2", lcsc_id="C-nan"),
        _make_part_row(uid="0xB3", lcsc_id="C-pinf"),
        _make_part_row(uid="0xB4", lcsc_id="C-ninf"),
        _make_part_row(uid="0xB5", lcsc_id="C-negprice"),
    ]
    stock_index = {
        "C-neg-stock": (-5, 1.0, True),
        "C-nan": (10, float("nan"), False),
        "C-pinf": (10, float("inf"), False),
        "C-ninf": (10, float("-inf"), False),
        "C-negprice": (10, -2.5, False),
    }
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter(rows), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    assert len(items) == 5
    for item in items:
        for key, value in item.items():
            if isinstance(value, float):
                assert math.isfinite(value), (
                    f"AC-RS-10: no payload value may ever be NaN/inf. "
                    f"Got {key}={value!r} in item {item!r}."
                )


def test_gate3_max_sane_constants_exist_and_are_finite_positive() -> None:
    """Gate 3 hardening (security — AC-RS-10 extension): Given the leaf's
    upper-bound sanity constants.
    Then _MAX_SANE_STOCK and _MAX_SANE_PRICE_USD both exist, are finite,
    strictly positive numbers — named ceilings a test binds to (mirroring
    the AC-RS-15 anchor pattern), never a magic number duplicated here.
    """
    import partgraph.refresh.stock as stock_mod

    assert hasattr(stock_mod, "_MAX_SANE_STOCK"), (
        "Gate 3: partgraph.refresh.stock._MAX_SANE_STOCK must exist."
    )
    assert hasattr(stock_mod, "_MAX_SANE_PRICE_USD"), (
        "Gate 3: partgraph.refresh.stock._MAX_SANE_PRICE_USD must exist."
    )

    max_stock = stock_mod._MAX_SANE_STOCK
    assert isinstance(max_stock, int) and not isinstance(max_stock, bool), (
        f"Gate 3: _MAX_SANE_STOCK must be a plain int. Got: {max_stock!r}"
    )
    assert max_stock > 0, f"Gate 3: _MAX_SANE_STOCK must be > 0. Got: {max_stock!r}"

    max_price = stock_mod._MAX_SANE_PRICE_USD
    assert isinstance(max_price, (int, float)) and not isinstance(max_price, bool), (
        f"Gate 3: _MAX_SANE_PRICE_USD must be a plain int/float. "
        f"Got: {max_price!r}"
    )
    assert math.isfinite(max_price), (
        f"Gate 3: _MAX_SANE_PRICE_USD must be finite. Got: {max_price!r}"
    )
    assert max_price > 0, (
        f"Gate 3: _MAX_SANE_PRICE_USD must be > 0. Got: {max_price!r}"
    )


def test_gate3_absurd_but_finite_stock_above_max_sane_omitted() -> None:
    """Gate 3 hardening (security — AC-RS-10 extension): Given a matched part
    whose stock exceeds _MAX_SANE_STOCK (an absurd-but-finite value — e.g. a
    corrupt or overflowed source field), with a valid price.
    When refresh_stock_write writes its payload.
    Then 'stock' is OMITTED (exactly like the negative/non-finite cases)
    while price_usd is still written, and stock_checked_at is still stamped.
    """
    import partgraph.refresh.stock as stock_mod

    absurd_stock = stock_mod._MAX_SANE_STOCK + 1
    row = _make_part_row(uid="0xV7", lcsc_id="C-huge-stock")
    stock_index = {"C-huge-stock": (absurd_stock, 1.0, True)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    item = items[0]
    assert set(item.keys()) == {"uid", "price_usd", "is_basic", "stock_checked_at"}, (
        f"Gate 3: stock above _MAX_SANE_STOCK ({stock_mod._MAX_SANE_STOCK!r}) "
        f"must be omitted. Got: {item!r}"
    )
    assert item["price_usd"] == 1.0
    assert item["stock_checked_at"] == _FIXED_MOMENT_STR


def test_gate3_absurd_but_finite_price_above_max_sane_omitted() -> None:
    """Gate 3 hardening (security — AC-RS-10 extension): Given a matched part
    whose price_usd exceeds _MAX_SANE_PRICE_USD (an absurd-but-finite
    value), with a valid stock.
    When refresh_stock_write writes its payload.
    Then 'price_usd' is OMITTED while stock is still written.
    """
    import partgraph.refresh.stock as stock_mod

    absurd_price = stock_mod._MAX_SANE_PRICE_USD + 1.0
    row = _make_part_row(uid="0xV8", lcsc_id="C-huge-price")
    stock_index = {"C-huge-price": (10, absurd_price, False)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    item = items[0]
    assert set(item.keys()) == {"uid", "stock", "is_basic", "stock_checked_at"}, (
        f"Gate 3: price_usd above _MAX_SANE_PRICE_USD "
        f"({stock_mod._MAX_SANE_PRICE_USD!r}) must be omitted. Got: {item!r}"
    )
    assert item["stock"] == 10
    assert item["stock_checked_at"] == _FIXED_MOMENT_STR


def test_gate3_stock_exactly_at_max_sane_boundary_is_still_written() -> None:
    """Gate 3 hardening (boundary — pins '>' not '>='): Given stock ==
    _MAX_SANE_STOCK exactly (the boundary itself, not one above it).
    When refresh_stock_write writes its payload.
    Then 'stock' is STILL written (not omitted) — only a value STRICTLY
    GREATER than the ceiling is invalid.
    """
    import partgraph.refresh.stock as stock_mod

    boundary_stock = stock_mod._MAX_SANE_STOCK
    row = _make_part_row(uid="0xV9", lcsc_id="C-boundary-stock")
    stock_index = {"C-boundary-stock": (boundary_stock, 1.0, True)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    item = items[0]
    assert item.get("stock") == boundary_stock, (
        f"Gate 3: stock exactly AT _MAX_SANE_STOCK ({boundary_stock!r}) must "
        f"still be written (only values STRICTLY GREATER are invalid). "
        f"Got: {item!r}"
    )


def test_gate3_price_exactly_at_max_sane_boundary_is_still_written() -> None:
    """Gate 3 hardening (boundary — pins '>' not '>='): Given price_usd ==
    _MAX_SANE_PRICE_USD exactly.
    When refresh_stock_write writes its payload.
    Then 'price_usd' is STILL written (not omitted).
    """
    import partgraph.refresh.stock as stock_mod

    boundary_price = stock_mod._MAX_SANE_PRICE_USD
    row = _make_part_row(uid="0xVA", lcsc_id="C-boundary-price")
    stock_index = {"C-boundary-price": (10, boundary_price, False)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_stock_write(
        iter([row]), client, stock_index=stock_index, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    item = items[0]
    assert item.get("price_usd") == boundary_price, (
        f"Gate 3: price_usd exactly AT _MAX_SANE_PRICE_USD "
        f"({boundary_price!r}) must still be written. Got: {item!r}"
    )


# ===========================================================================
# AC-RS-11: one committed txn per page; write-back failures propagate
# ===========================================================================

def test_ac_rs_11_multiple_valid_rows_written_in_one_mutate_call() -> None:
    """AC-RS-11: Given three matched rows in a single refresh_stock_write call.
    When the call runs.
    Then exactly ONE txn.mutate() call (and one commit()) covers all three
    rows — never one transaction per row.
    """
    rows = [_make_part_row(uid=f"0x{i}", lcsc_id=f"C{i}") for i in (1, 2, 3)]
    stock_index = {f"C{i}": (i * 10, float(i), False) for i in (1, 2, 3)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_stock_write(
        iter(rows), client, stock_index=stock_index, clock=_fixed_clock
    )

    assert write_txn.mutate.call_count == 1, (
        f"AC-RS-11: all rows must be written in ONE mutate() call. "
        f"Got {write_txn.mutate.call_count} calls."
    )
    assert write_txn.commit.call_count == 1
    items = _written_payload_items(write_txn)
    assert {item["uid"] for item in items} == {"0x1", "0x2", "0x3"}
    assert summary == {"checked": 3, "matched": 3, "absent": 0}


def test_ac_rs_11_write_back_mutate_exception_propagates_not_swallowed() -> None:
    """AC-RS-11 (mirrors refresh_links_write's own write-back guarantee):
    Given the write-back txn's mutate() raises.
    When refresh_stock_write is called.
    Then the exception PROPAGATES (never caught/swallowed) so the CLI's own
    try/except can convert it into the path-free _REFRESH_STOCK_DB_ERROR.
    """
    row = _make_part_row(uid="0xW1", lcsc_id="C1")
    stock_index = {"C1": (10, 1.0, True)}
    write_txn = _make_txn()
    write_txn.mutate.side_effect = RuntimeError("connection refused")
    client = _make_simple_client(write_txn)

    with pytest.raises(RuntimeError, match="connection refused"):
        refresh_stock_write(
            iter([row]), client, stock_index=stock_index, clock=_fixed_clock
        )


def test_ac_rs_11_write_back_commit_exception_propagates_not_swallowed() -> None:
    """AC-RS-11: Given the write-back txn's commit() raises (mutate succeeds).
    When refresh_stock_write is called.
    Then the exception PROPAGATES unchanged.
    """
    row = _make_part_row(uid="0xW2", lcsc_id="C1")
    stock_index = {"C1": (10, 1.0, True)}
    write_txn = _make_txn()
    write_txn.commit.side_effect = RuntimeError("connection refused")
    client = _make_simple_client(write_txn)

    with pytest.raises(RuntimeError, match="connection refused"):
        refresh_stock_write(
            iter([row]), client, stock_index=stock_index, clock=_fixed_clock
        )


# ===========================================================================
# AC-RS-13 (leaf half): duplicate uid within one call processed once
# ===========================================================================

def test_ac_rs_13_duplicate_uid_within_same_call_processed_once() -> None:
    """AC-RS-13 (leaf half — split across files; the CLI-level cross-run
    staleness-filter dedup is pinned separately in test_cli_refresh_stock.py,
    mirroring how AC-RL-13 splits leaf-level de-dup from the CLI-level cursor
    guarantee): Given the SAME Part uid appears TWICE within a single
    refresh_stock_write call's input iterable (a defensive scenario — this
    should never happen given a correct uid keyset cursor at the CLI layer,
    but the leaf must not double-count even if it does).
    When refresh_stock_write processes the duplicated input.
    Then summary['checked'] == 1 (not 2), and exactly ONE payload item for
    that uid is written.
    """
    row_a = _make_part_row(uid="0xDUP", lcsc_id="C1")
    row_b = _make_part_row(uid="0xDUP", lcsc_id="C1")
    stock_index = {"C1": (10, 1.0, True)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_stock_write(
        iter([row_a, row_b]), client, stock_index=stock_index, clock=_fixed_clock
    )

    assert summary["checked"] == 1, (
        f"AC-RS-13: a duplicate uid within one call must be checked exactly "
        f"once, not double-counted. Got summary: {summary!r}"
    )
    items = _written_payload_items(write_txn)
    matching = [item for item in items if item["uid"] == "0xDUP"]
    assert len(matching) == 1, (
        f"AC-RS-13: exactly one payload item for the duplicated uid must be "
        f"written. Got: {items!r}"
    )


# ===========================================================================
# AC-RS-15 (leaf half): structural isolation from embed/refresh-links/loader
# ===========================================================================

def test_ac_rs_15_refresh_stock_write_never_references_forbidden_symbols() -> None:
    """AC-RS-15 (leaf half): Given refresh_stock_write's own source code.
    Then it never references Loader, a bare 'load(' call, _build_part_obj,
    _embed_all_pages, _select_parts_for_embed, embed_write,
    refresh_links_write, _refresh_all_pages or _select_datasheets_for_refresh
    — proving this new leaf is fully independent of the loader, embed and
    refresh-links pipelines (none of which this PR may modify).
    """
    import partgraph.refresh.stock as stock_mod

    source = inspect.getsource(stock_mod.refresh_stock_write)
    forbidden = (
        "Loader",
        "load(",
        "_build_part_obj",
        "_embed_all_pages",
        "_select_parts_for_embed",
        "embed_write",
        "refresh_links_write",
        "_refresh_all_pages",
        "_select_datasheets_for_refresh",
    )
    for name in forbidden:
        assert name not in source, (
            f"AC-RS-15: refresh_stock_write must not reference {name!r}. "
            f"Source:\n{source}"
        )


# ===========================================================================
# AC-RS-16: summary shape
# ===========================================================================

def test_ac_rs_16_summary_returns_exactly_checked_matched_absent() -> None:
    """AC-RS-16: Given a mixed batch: two matched rows and one absent row.
    When refresh_stock_write processes the batch.
    Then it returns a dict with EXACTLY the keys {checked, matched, absent}
    and correct counts: checked=3, matched=2, absent=1, and
    checked == matched + absent.
    """
    rows = [
        _make_part_row(uid="0xE1", lcsc_id="C1"),
        _make_part_row(uid="0xE2", lcsc_id="C2"),
        _make_part_row(uid="0xE3", lcsc_id="C-missing"),
    ]
    stock_index = {"C1": (1, 1.0, True), "C2": (2, 2.0, False)}
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_stock_write(
        iter(rows), client, stock_index=stock_index, clock=_fixed_clock
    )

    assert set(summary.keys()) == {"checked", "matched", "absent"}, (
        f"AC-RS-16: summary must have EXACTLY {{checked, matched, absent}} "
        f"keys. Got: {summary!r}"
    )
    assert summary == {"checked": 3, "matched": 2, "absent": 1}, (
        f"AC-RS-16: expected checked=3 matched=2 absent=1. Got: {summary!r}"
    )
    assert summary["checked"] == summary["matched"] + summary["absent"]
