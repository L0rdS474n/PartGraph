"""Stock/price refresh (leaf module for ``partgraph refresh``).

This is a **leaf** module (issue #11, PR 2). It writes the current LCSC
stock / price / basic-part status back onto graph Part nodes by ``uid`` and
stamps each touched Part with a freshness timestamp. Like its sibling
link-refresh leaf it depends only on injected seams — the graph ``client`` and
a wall-clock ``clock`` callable — and never opens a socket, never contacts a
real graph database, and never reads the real wall clock, so the unit suite
stays hermetic.

There is deliberately **no** network surface in this leaf at all: the JLC
stock/price snapshot is parsed once, up front, into an in-memory
``stock_index`` dict by the CLI (via the already-shipped source adapter) and
handed in ready-built; this module only joins that dict against the graph rows
it is given and stages the narrow write-back.

Design posture
--------------
- **Freshness is stamped at refresh time**, from the injected ``clock`` seam,
  formatted as a deterministic RFC-3339 UTC ``…Z`` string
  (:func:`format_checked_at`) so the stamp is byte-reproducible and no real
  wall clock leaks into a test. The formatter's convention matches the
  link-refresh leaf's, but the logic is copied here rather than imported so the
  two leaves stay fully decoupled.
- **Value sanity before write-back.** A stock or price that is ``None``,
  negative, non-finite (``NaN``/``inf``) or absurdly large (above
  :data:`_MAX_SANE_STOCK` / :data:`_MAX_SANE_PRICE_USD`) is OMITTED from the
  payload entirely — never written through as a JSON ``null`` or a corrupt
  quantity. ``is_basic`` is always written, coerced to a strict ``bool``.
- **One committed transaction per call.** Every eligible row for one call is
  written back in a single ``mutate``/``commit``; a mutation failure propagates
  untouched so the CLI can convert it into a single path-free error. A ``uid``
  seen twice within one call is processed exactly once.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "build_stock_index",
    "format_checked_at",
    "refresh_stock_write",
]

#: Upper sanity ceiling for a Part's stock count. A value STRICTLY above this is
#: treated as corrupt/overflowed source data and omitted from the write-back:
#: JLC's whole catalogue is on the order of 10^5 distinct parts and per-part
#: on-hand stock is bounded by real inventory, so 10^9 is comfortably
#: unreachable by a legitimate quantity while still catching a garbage/overflowed
#: field. The bound is inclusive (a value exactly AT it is still valid).
_MAX_SANE_STOCK = 1_000_000_000

#: Upper sanity ceiling (USD) for a Part's unit price. A value STRICTLY above
#: this is treated as corrupt source data and omitted: no real single electronic
#: component costs a million dollars, so a larger figure signals a parse/units
#: error rather than a price. The bound is inclusive.
_MAX_SANE_PRICE_USD = 1_000_000.0


def format_checked_at(moment: datetime) -> str:
    """Return the deterministic RFC-3339 UTC (``…Z``) string for *moment*.

    A naive datetime is treated as UTC; a tz-aware one is converted to UTC. The
    result is byte-stable for a given instant (no wall-clock or locale leak).
    The convention matches the sibling link-refresh leaf's formatter, but the
    logic is copied here so the two leaves stay decoupled.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_stock_index(
    parts_iter: Iterable[Any],
) -> dict[str, tuple[int | None, float | None, bool]]:
    """Join JLC-source rows into a ``lcsc_id -> (stock, price_usd, is_basic)`` map.

    *parts_iter* yields row-like objects exposing ``lcsc_id`` / ``stock`` /
    ``price_usd`` / ``is_basic`` attributes (e.g. a source ``StagedPart`` or an
    equivalent namespace). A row whose ``lcsc_id`` is ``None`` is skipped (never
    keyed under a literal ``None``); a duplicate ``lcsc_id`` is last-wins, so a
    later source row supersedes an earlier one for the same LCSC identity.

    ``is_basic`` is coerced to a strict ``bool`` by IDENTITY (``bool(...)`` — a
    raw ``1``/``"true"`` becomes the canonical ``True`` singleton, ``0`` the
    canonical ``False``), pinning the module's advertised duck-typed row contract
    rather than trusting every caller to pre-coerce.
    """
    index: dict[str, tuple[int | None, float | None, bool]] = {}
    for row in parts_iter:
        lcsc_id = getattr(row, "lcsc_id", None)
        if lcsc_id is None:
            continue
        index[lcsc_id] = (
            getattr(row, "stock", None),
            getattr(row, "price_usd", None),
            bool(getattr(row, "is_basic", False)),
        )
    return index


def _is_sane_stock(stock: Any) -> bool:
    """Return ``True`` iff *stock* is a plain, non-negative, in-range integer.

    A ``bool`` is explicitly rejected (it is an ``int`` subclass but is never a
    real stock quantity), as is ``None``, a negative value, or a value strictly
    above :data:`_MAX_SANE_STOCK`.
    """
    return (
        isinstance(stock, int)
        and not isinstance(stock, bool)
        and 0 <= stock <= _MAX_SANE_STOCK
    )


def _is_sane_price(price: Any) -> bool:
    """Return ``True`` iff *price* is a finite, non-negative, in-range number.

    Rejects ``None``, a ``bool``, a non-finite float (``NaN``/``inf`` via
    :func:`math.isfinite`), a negative value, or a value strictly above
    :data:`_MAX_SANE_PRICE_USD` — so no such value is ever serialized into the
    mutation payload.
    """
    return (
        isinstance(price, (int, float))
        and not isinstance(price, bool)
        and math.isfinite(price)
        and 0 <= price <= _MAX_SANE_PRICE_USD
    )


def refresh_stock_write(
    parts_iter: Iterable[Any],
    client: Any,
    *,
    stock_index: dict[str, tuple[int | None, float | None, bool]],
    clock: Callable[[], datetime],
) -> dict:
    """Write current stock/price back onto graph Parts and stamp their freshness.

    *parts_iter* yields graph Part rows (each exposing ``uid`` and ``lcsc_id``,
    as selected by the CLI). For each unique ``uid``:

    - a MATCHED row (its ``lcsc_id`` is present in *stock_index*) stages a narrow
      ``{uid, stock?, price_usd?, is_basic, stock_checked_at}`` write — ``stock``
      and ``price_usd`` are each included only when sane (see
      :func:`_is_sane_stock` / :func:`_is_sane_price`), ``is_basic`` is always
      written and ``stock_checked_at`` is always stamped;
    - an ABSENT row (no matching source entry) stages a stamp-only
      ``{uid, stock_checked_at}`` write, leaving the volatile fields untouched.

    Every staged row is written back in ONE committed transaction; a
    ``mutate``/``commit`` failure propagates untouched (never swallowed) so the
    CLI can convert it into a single path-free error. A ``uid`` appearing twice
    within one call is processed exactly once. Returns exactly
    ``{"checked": int, "matched": int, "absent": int}`` with
    ``checked == matched + absent``.
    """
    stamp = format_checked_at(clock())
    payload: list[dict[str, Any]] = []
    matched = 0
    seen: set[str] = set()

    for row in parts_iter:
        uid = getattr(row, "uid", None)
        if not isinstance(uid, str) or not uid or uid in seen:
            continue
        seen.add(uid)

        item: dict[str, Any] = {"uid": uid, "stock_checked_at": stamp}
        lcsc_id = getattr(row, "lcsc_id", None)
        if lcsc_id is not None and lcsc_id in stock_index:
            stock, price_usd, is_basic = stock_index[lcsc_id]
            if _is_sane_stock(stock):
                item["stock"] = stock
            if _is_sane_price(price_usd):
                item["price_usd"] = price_usd
            item["is_basic"] = bool(is_basic)
            matched += 1
        payload.append(item)

    checked = len(payload)
    if payload:
        txn = client.txn()
        try:
            txn.mutate(set_obj=payload)
            txn.commit()
        finally:
            txn.discard()

    return {"checked": checked, "matched": matched, "absent": checked - matched}
