"""
Tests: PR-C (feat/db-idle-autostop) — CLI wiring of `partgraph.util.activity`
into the nine DB-touching commands already established by PR-B2's own
allowlist (`tests/unit/test_cli_autostart.py`'s B-6 section: `stats`,
`search`, `show`, `embed`, `refresh-links`, `refresh`, `db apply-schema`,
`db check-index`, `ingest jlcparts` load stage) — C-1 (every one of them
stamps activity on completion and holds a lease while it runs) and C-2
(the four PAGING commands among them — `embed`, `refresh`, `refresh-links`,
`ingest jlcparts` load — refresh the stamp PER PAGE, not merely once at the
end, "so a multi-hour run never looks idle").

NOT YET IMPLEMENTED. `partgraph.util.activity` does not exist yet — this
whole file is expected to ERROR at COLLECTION with `ModuleNotFoundError`,
mirroring `tests/unit/test_cli_db_down.py`'s own documented pre-PR-A
history.

SCOPE, DISCLOSED (mirrors `test_cli_autostart.py`'s own B-1/B-2/B-3-vs-B-6
precedent: "ONE deep... the remaining commands are covered by [a lighter
mechanism] instead"): C-1's simple "touch on completion" ordering is proven
for all five NON-paging commands below. The FULL lease-acquire/lease-release
wrap (including the error path) is proven DEEPLY for one representative,
non-paging command (`stats`) rather than independently for all nine — the
underlying lease-context-manager mechanics (`held_lease`) are ALREADY
exhaustively proven at the leaf level in `tests/unit/test_activity.py`;
what this file adds is that `partgraph.cli` actually WIRES that leaf
primitive around each command's own real DB work, which one deep proof
establishes for the wiring PATTERN. C-2's per-page heartbeat is proven with
a REAL, end-to-end, TWO-page CLI-level run for all three commands with an
existing multi-page unit-test harness in this repo (`embed`, `refresh-links`,
`refresh`/stock — each fixture builder below is a trimmed, LOCAL copy of the
already-existing pattern in `test_cli_embed.py` / `test_cli_refresh_links.py`
/ `test_cli_refresh_stock.py`, per CONTRIBUTING.md's "test fixtures stay
local to their file"), and for the fourth (`ingest jlcparts` load stage) by
capturing the SAME `progress=` callback `Loader(...)` already receives (the
load stage's own pre-existing per-batch hook — see `cli.py`'s `_stage_load`)
and proving it ALSO drives the heartbeat, reusing that existing seam rather
than inventing a second, parallel one.
"""

from __future__ import annotations

import json
import pathlib
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

import partgraph.cli as cli_mod
from partgraph.cli import app
from partgraph.embed import EMBED_DIM

# This import is expected to raise ModuleNotFoundError until
# src/partgraph/util/activity.py exists — the correct test-first red state.
from partgraph.util.activity import touch_activity  # noqa: E402, F401

from typer.testing import CliRunner  # noqa: E402

RUNNER = CliRunner()


def _invoke(args: list[str]):
    return RUNNER.invoke(app, args)


def _autostart_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here is about the activity stamp/lease, not autostart —
    the autouse fixture in conftest.py already forces PARTGRAPH_AUTOSTART=0,
    kept explicit here for readability."""
    monkeypatch.setenv("PARTGRAPH_AUTOSTART", "0")


def _make_ordered_touch(order: list[str], name: str, *, return_value=None):
    def _fn(*args, **kwargs):
        order.append(name)
        return return_value

    return MagicMock(side_effect=_fn)


def _make_ordered_touch_activity(order: list[str]):
    def _fn(*args, **kwargs):
        order.append("touch_activity")

    return MagicMock(side_effect=_fn)


def _make_ordered_held_lease(order: list[str]):
    @contextmanager
    def _cm(*args, **kwargs):
        order.append("held_lease_enter")
        try:
            yield
        finally:
            order.append("held_lease_exit")

    return _cm


# ---------------------------------------------------------------------------
# C-1 — the five non-paging DB-touching commands: touch_activity fires AFTER
# the command's own DB work completes.
# ---------------------------------------------------------------------------


def test_c1_stats_touches_activity_after_its_db_work(monkeypatch: pytest.MonkeyPatch) -> None:
    _autostart_off(monkeypatch)
    order: list[str] = []

    def _fake_query(dql, *a, **kw):
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    with (
        patch.object(
            cli_mod, "_build_dgraph_client",
            _make_ordered_touch(order, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        ),
        patch("partgraph.cli.touch_activity", _make_ordered_touch_activity(order)),
    ):
        result = _invoke(["stats"])

    assert result.exit_code == 0, result.output
    assert order == ["_build_dgraph_client", "touch_activity"], (
        f"touch_activity must fire AFTER the DB work completes, not before: {order!r}"
    )


def test_c1_search_touches_activity_after_its_db_work(monkeypatch: pytest.MonkeyPatch) -> None:
    _autostart_off(monkeypatch)
    order: list[str] = []

    def _fake_query(dql, *a, **kw):
        resp = MagicMock()
        resp.json = json.dumps({"exact": [], "trig": [], "fts": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    with (
        patch.object(
            cli_mod, "_build_dgraph_client",
            _make_ordered_touch(order, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        ),
        patch("partgraph.cli.touch_activity", _make_ordered_touch_activity(order)),
    ):
        result = _invoke(["search", "MAX232"])

    assert result.exit_code == 0, result.output
    assert order == ["_build_dgraph_client", "touch_activity"]


def test_c1_show_touches_activity_after_its_db_work(monkeypatch: pytest.MonkeyPatch) -> None:
    _autostart_off(monkeypatch)
    order: list[str] = []

    def _fake_query(dql, *a, **kw):
        resp = MagicMock()
        resp.json = json.dumps({"part": [], "related": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    with (
        patch.object(
            cli_mod, "_build_dgraph_client",
            _make_ordered_touch(order, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        ),
        patch("partgraph.cli.touch_activity", _make_ordered_touch_activity(order)),
    ):
        result = _invoke(["show", "MAX232"])

    assert result.exit_code == 0, result.output
    assert order == ["_build_dgraph_client", "touch_activity"]


def test_c1_db_apply_schema_touches_activity_after_its_db_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _autostart_off(monkeypatch)
    order: list[str] = []

    with (
        patch.object(cli_mod.schema_module, "load_schema", return_value="type Part {}"),
        patch.object(
            cli_mod.schema_module, "apply_schema",
            _make_ordered_touch(order, "schema_module.apply_schema"),
        ),
        patch("partgraph.cli.touch_activity", _make_ordered_touch_activity(order)),
    ):
        result = _invoke(["db", "apply-schema"])

    assert result.exit_code == 0, result.output
    assert order == ["schema_module.apply_schema", "touch_activity"]


def test_c1_db_check_index_touches_activity_after_its_db_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _autostart_off(monkeypatch)
    order: list[str] = []
    healthy_result = MagicMock(reachable=True, schema_ok=True, self_similarity_ok=True, message="ok")

    with (
        patch(
            "partgraph.cli.check_index_integrity",
            _make_ordered_touch(order, "check_index_integrity", return_value=healthy_result),
        ),
        patch("partgraph.cli.touch_activity", _make_ordered_touch_activity(order)),
    ):
        result = _invoke(["db", "check-index"])

    assert result.exit_code == 0, result.output
    assert order == ["check_index_integrity", "touch_activity"]


# ---------------------------------------------------------------------------
# C-1 / C-3 (deep) — held_lease genuinely wraps the DB work, INCLUDING the
# error path (no stamp on a command that did NOT complete).
# ---------------------------------------------------------------------------


def test_c3_stats_holds_a_lease_around_its_db_work_and_touches_activity_inside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-1/C-3 [deep, representative]: Given `stats`.
    When it runs successfully.
    Then the sequence is EXACTLY: lease acquired, the DB work runs, the
    activity stamp is touched, THEN the lease is released — proving
    `held_lease` genuinely wraps the real command body (not merely present
    somewhere unreached), and that the stamp update happens INSIDE the held
    lease window (never after it is already released, which would leave a
    window where a concurrent `db idle-stop` could stop a database whose
    activity was about to be recorded but was not yet)."""
    _autostart_off(monkeypatch)
    order: list[str] = []

    def _fake_query(dql, *a, **kw):
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    with (
        patch("partgraph.cli.held_lease", _make_ordered_held_lease(order)),
        patch.object(
            cli_mod, "_build_dgraph_client",
            _make_ordered_touch(order, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        ),
        patch("partgraph.cli.touch_activity", _make_ordered_touch_activity(order)),
    ):
        result = _invoke(["stats"])

    assert result.exit_code == 0, result.output
    assert order == [
        "held_lease_enter", "_build_dgraph_client", "touch_activity", "held_lease_exit",
    ], order


def test_c3_stats_releases_the_lease_even_when_its_db_work_raises_and_never_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-3 [error path]: Given `stats`'s own DB work raises.
    When `partgraph stats` runs.
    Then the lease is still released (`held_lease_exit` reached, via
    `finally`) AND `touch_activity` is NEVER called — a command that did NOT
    complete real work must not record activity for work it never finished.
    """
    _autostart_off(monkeypatch)
    order: list[str] = []

    def _raise(*args, **kwargs):
        order.append("_build_dgraph_client_raised")
        raise RuntimeError("simulated DB failure")

    with (
        patch("partgraph.cli.held_lease", _make_ordered_held_lease(order)),
        patch.object(cli_mod, "_build_dgraph_client", side_effect=_raise),
        patch("partgraph.cli.touch_activity", _make_ordered_touch_activity(order)),
    ):
        _invoke(["stats"])  # exit code irrelevant here — only the sequence matters

    assert "held_lease_enter" in order
    assert "held_lease_exit" in order, (
        f"the lease must be released even though the DB work raised: {order!r}"
    )
    assert "touch_activity" not in order, (
        f"a command whose DB work raised must NOT record activity: {order!r}"
    )


# ---------------------------------------------------------------------------
# C-2 — paging commands heartbeat PER PAGE. Real, end-to-end, two-page runs
# (local, trimmed copies of each command's own existing multi-page harness).
# ---------------------------------------------------------------------------


# --- embed (mirrors test_cli_embed.py's own AC-EC-11 harness, trimmed) -----


def _embed_row(uid: str) -> dict:
    return {"uid": uid, "xid": f"ELIGIBLE-{uid}|VEND", "description": "widget"}


def _make_embed_cursor_read_txn(pages: list[dict]) -> MagicMock:
    """[Gate 3b defect 3 fix] Mirrors `test_cli_embed.py`'s own
    `_make_cursor_aware_read_txn` docstring exactly: a selection query is
    recognised by containing BOTH `"type(Part)"` and `"first:"` (the shape
    `_select_parts_for_embed` emits) and consumes the next page from
    *pages*; any OTHER query — namely `embed_write`'s own xid-resolution
    lookup via `_resolve_uids_by_xid` — must NOT also consume a page, or a
    real two-page run dies on `StopIteration` once that unrelated query
    steals page two's payload. Answered with an empty match set instead, a
    deterministic, side-effect-free stand-in that degrades uid resolution to
    each part's own `uid` (already set by selection)."""
    remaining = iter(pages)
    empty_resolve = MagicMock()
    empty_resolve.json = json.dumps({"q": []}).encode()

    def _side_effect(query_text, *a, **kw):
        if "type(Part)" in query_text and "first:" in query_text:
            resp = MagicMock()
            resp.json = json.dumps(next(remaining)).encode()
            return resp
        return empty_resolve

    txn = MagicMock()
    txn.query.side_effect = _side_effect
    txn.discard.return_value = None
    return txn


def _make_embed_write_txn() -> MagicMock:
    txn = MagicMock()
    txn.mutate.return_value = MagicMock()
    txn.commit.return_value = None
    txn.discard.return_value = None
    return txn


def _make_embed_dispatch_client(read_txn: MagicMock, write_txn: MagicMock) -> MagicMock:
    client = MagicMock()

    def _factory(*a, **kw):
        return read_txn if kw.get("read_only") else write_txn

    client.txn.side_effect = _factory
    return client


def test_c2_embed_heartbeats_the_activity_stamp_once_per_real_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-2: Given a real, TWO-page embed run (page size patched to 2; page 1
    has 2 eligible parts — a FULL page, forcing continuation; page 2 has 1
    eligible part — a SHORT page, terminating without a further fetch), so
    exactly two pages of REAL work happen.
    When `partgraph embed` runs.
    Then `touch_activity` is called at least twice — once per real page —
    not merely once at the very end of the whole run."""
    _autostart_off(monkeypatch)
    page1 = {"q": [_embed_row("0x0001"), _embed_row("0x0002")]}
    page2 = {"q": [_embed_row("0x0003")]}
    read_txn = _make_embed_cursor_read_txn([page1, page2])
    write_txn = _make_embed_write_txn()
    client = _make_embed_dispatch_client(read_txn, write_txn)

    calls: list[None] = []

    def _fake_get_encoder():
        # [Gate 3b defect 3 fix] Must return EMBED_DIM-wide vectors
        # (384) — `partgraph.embed` validates each vector's width and
        # raises otherwise; a 1-dimensional stub silently killed the run
        # before it ever reached a second page.
        return lambda texts: [[0.0] * EMBED_DIM for _ in texts]

    with (
        patch.object(cli_mod, "_build_dgraph_client", return_value=(client, MagicMock())),
        patch.object(cli_mod, "get_encoder", _fake_get_encoder, create=True),
        patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 2, create=True),
        patch("partgraph.cli.touch_activity", side_effect=lambda *a, **k: calls.append(None)),
    ):
        result = _invoke(["embed"])

    assert result.exit_code == 0, result.output
    assert len(calls) >= 2, (
        f"expected touch_activity at least once per real page (2 pages), "
        f"got {len(calls)} call(s)"
    )


# --- refresh-links (mirrors test_cli_refresh_links.py's own harness) ------


def _ds_row(uid: str, *, url: str = "https://lcsc.com/x.pdf", fail_count: int = 0) -> dict:
    return {"uid": uid, "url": url, "http_status": 0, "fail_count": fail_count}


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeHttpClient:
    def head(self, url: str, **kwargs) -> _FakeResponse:
        return _FakeResponse(200)

    def get(self, url: str, **kwargs) -> _FakeResponse:
        return _FakeResponse(200)


def _make_links_cursor_read_txn(pages: list[dict]) -> MagicMock:
    remaining = iter(pages)

    def _side_effect(query_text, *a, **kw):
        resp = MagicMock()
        if "type(Datasheet)" in query_text and "first:" in query_text:
            resp.json = json.dumps(next(remaining)).encode()
        else:
            resp.json = json.dumps({"q": []}).encode()
        return resp

    txn = MagicMock()
    txn.query.side_effect = _side_effect
    txn.discard.return_value = None
    return txn


def test_c2_refresh_links_heartbeats_the_activity_stamp_once_per_real_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-2: Given a real, two-page refresh-links run (page size patched to
    2; page 1 full with 2 rows, page 2 short with 1 row)."""
    _autostart_off(monkeypatch)
    page1 = {"q": [_ds_row("0xF001"), _ds_row("0xF002")]}
    page2 = {"q": [_ds_row("0xF003")]}
    read_txn = _make_links_cursor_read_txn([page1, page2])
    write_txn = MagicMock()
    write_txn.mutate.return_value = MagicMock()
    write_txn.commit.return_value = None
    write_txn.discard.return_value = None
    client = MagicMock()
    client.txn.side_effect = lambda *a, **kw: read_txn if kw.get("read_only") else write_txn

    calls: list[None] = []
    fixed_now = datetime(2030, 6, 1, 12, 0, 0, tzinfo=UTC)

    with (
        patch.object(cli_mod, "_build_dgraph_client", return_value=(client, MagicMock())),
        patch.object(cli_mod, "_build_http_client", return_value=_FakeHttpClient(), create=True),
        patch.object(cli_mod, "_utcnow", lambda: fixed_now, create=True),
        patch.object(cli_mod, "_REFRESH_SELECT_PAGE_SIZE", 2, create=True),
        patch("partgraph.cli.touch_activity", side_effect=lambda *a, **k: calls.append(None)),
    ):
        result = _invoke(["refresh-links"])

    assert result.exit_code == 0, result.output
    assert len(calls) >= 2, (
        f"expected touch_activity at least once per real page (2 pages), got {len(calls)}"
    )


# --- refresh / stock (mirrors test_cli_refresh_stock.py's own harness) -----


def _stock_part_row(uid: str, lcsc_id: str = "C1") -> dict:
    return {"uid": uid, "lcsc_id": lcsc_id}


def _make_stock_cursor_read_txn(pages: list[dict]) -> MagicMock:
    remaining = iter(pages)

    def _side_effect(query_text, *a, **kw):
        resp = MagicMock()
        if "type(Part)" in query_text and "first:" in query_text and "stock_checked_at" in query_text:
            resp.json = json.dumps(next(remaining)).encode()
        else:
            resp.json = json.dumps({"q": []}).encode()
        return resp

    txn = MagicMock()
    txn.query.side_effect = _side_effect
    txn.discard.return_value = None
    return txn


def test_c2_refresh_stock_heartbeats_the_activity_stamp_once_per_real_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """C-2: Given a real, two-page `refresh` (stock/price) run (page size
    patched to 2; page 1 full with 2 rows, page 2 short with 1 row) and a
    stubbed source-loading seam so the command reaches its DB-touching
    point."""
    _autostart_off(monkeypatch)
    dummy = tmp_path / "dummy-jlcpcb-components.sqlite3"
    dummy.write_bytes(b"")
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", dummy)

    page1 = {"q": [_stock_part_row("0x0001"), _stock_part_row("0x0002")]}
    page2 = {"q": [_stock_part_row("0x0003")]}
    read_txn = _make_stock_cursor_read_txn([page1, page2])
    write_txn = MagicMock()
    write_txn.mutate.return_value = MagicMock()
    write_txn.commit.return_value = None
    write_txn.discard.return_value = None
    client = MagicMock()
    client.txn.side_effect = lambda *a, **kw: read_txn if kw.get("read_only") else write_txn

    calls: list[None] = []
    fixed_now = datetime(2031, 9, 9, 12, 0, 0, tzinfo=UTC)

    with (
        patch.object(cli_mod, "_build_dgraph_client", return_value=(client, MagicMock())),
        patch.object(cli_mod, "_load_stock_index", return_value={"C1": (1, 1.0, False)}, create=True),
        patch.object(cli_mod, "_utcnow", lambda: fixed_now, create=True),
        patch.object(cli_mod, "_REFRESH_STOCK_SELECT_PAGE_SIZE", 2, create=True),
        patch("partgraph.cli.touch_activity", side_effect=lambda *a, **k: calls.append(None)),
    ):
        result = _invoke(["refresh"])

    assert result.exit_code == 0, result.output
    assert len(calls) >= 2, (
        f"expected touch_activity at least once per real page (2 pages), got {len(calls)}"
    )


# --- ingest jlcparts load stage: reuses the EXISTING per-batch Loader
# progress callback, rather than inventing a second, parallel hook. ---------


def test_c2_ingest_jlcparts_load_heartbeats_via_the_existing_loader_progress_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """C-2 [reuses the existing seam]: Given `_stage_load` already threads a
    per-batch `progress=` callback into `Loader(client, progress=_on_load)`
    (pre-existing, for the progress bar) — see `src/partgraph/cli.py`'s
    `_stage_load`.
    When `partgraph ingest jlcparts` reaches the load stage and the
    CONSTRUCTED `Loader`'s own `progress` callback is captured and then
    invoked directly, twice, as `Loader.load` itself would for two real
    batches.
    Then EACH invocation also drives `touch_activity` — proving the
    heartbeat reuses this ALREADY-ESTABLISHED per-batch hook rather than
    adding a second, parallel one that could silently drift out of sync
    with it.
    """
    _autostart_off(monkeypatch)
    dummy = tmp_path / "dummy-jlcpcb-components.sqlite3"
    dummy.write_bytes(b"")
    staged = tmp_path / "staged.jsonl"
    staged.write_bytes(b"")
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", dummy)
    monkeypatch.setattr(cli_mod, "STAGED_PATH", staged)
    monkeypatch.setattr(cli_mod, "LOAD_CHECKPOINT_PATH", tmp_path / "state" / "load_checkpoint.json")

    captured_kwargs: dict = {}
    calls: list[None] = []

    class _FakeLoader:
        def __init__(self, client, **kwargs):
            captured_kwargs.update(kwargs)

        def load(self, parts, **kwargs):
            progress = captured_kwargs.get("progress")
            assert callable(progress), "Loader must be constructed with a callable progress="
            progress(1, 2)
            progress(2, 2)

    with (
        patch.object(cli_mod, "_build_dgraph_client", return_value=(MagicMock(), MagicMock())),
        patch("partgraph.sources.jlcparts.open_jlcparts_db", return_value=MagicMock()),
        patch("partgraph.sources.jlcparts.JlcpartsAdapter"),
        patch("partgraph.normalize.run.normalize", return_value=None),
        patch("partgraph.load.loader.Loader", _FakeLoader),
        patch.object(cli_mod, "_read_staged_parts", return_value=[object(), object()], create=True),
        patch("partgraph.cli.touch_activity", side_effect=lambda *a, **k: calls.append(None)),
    ):
        result = _invoke(["ingest", "jlcparts"])

    assert result.exit_code == 0, result.output
    assert len(calls) >= 2, (
        f"expected the reused progress callback to drive touch_activity twice "
        f"(once per fake batch), got {len(calls)}"
    )
