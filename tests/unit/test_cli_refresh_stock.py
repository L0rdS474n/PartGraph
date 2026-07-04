"""
Tests: AC-RS-1, AC-RS-2, AC-RS-3, AC-RS-4, AC-RS-5 (CLI half), AC-RS-12,
AC-RS-13 (CLI half), AC-RS-14, AC-RS-15 (CLI half), AC-RS-17 —
`partgraph refresh` CLI command (issue #11, PR 2: stock/price refresh)

Specifies the behaviour of the NEW `partgraph refresh` CLI command: flags/
help, the schema addition it depends on, the Part selection query
(lcsc_id + staleness filter — NOT the embed filter, NOT refresh-links'
Datasheet filter), uid-keyset cursor pagination (mirroring embed's AC-EC-8 /
refresh-links' AC-RL-4 pattern exactly), reuse of the existing
``_stage_fetch``/``RAW_DB_RELPATH`` source-loading machinery, the DB-down
error path, and a structural guarantee that the loader/embed/refresh-links
pipelines are left untouched.

This file deliberately does NOT import `partgraph.refresh.stock` at module
level (unlike test_refresh_stock.py) so its collection/RED failures are
attributable strictly to the CLI layer (missing `refresh` command / missing
cli.* symbols), not a transitively missing leaf module — mirroring
test_cli_refresh_links.py's own attributable-RED discipline exactly.

NEW symbol flagged for the impl gate (not spelled out verbatim in the given
target contract's function list, but REQUIRED by "CLI tests patch the
source-loading seam to return a prebuilt dict"): ``_load_stock_index(dest)
-> dict`` — a CLI-level helper mirroring ``_stage_normalize``'s reuse of
``open_jlcparts_db``/``JlcpartsAdapter``, expected to open the jlcparts sqlite
file at *dest*, run it through ``JlcpartsAdapter`` and
``partgraph.refresh.stock.build_stock_index``, and return the resulting dict.
Every test below that needs to reach the Dgraph-paging step patches this seam
directly (mirroring how test_cli_embed.py/test_cli_refresh_links.py patch
``_build_dgraph_client``/``_build_http_client``), so NO test in this file ever
opens the real (gitignored, ~1.6 GB) source database or a real socket.
``_utcnow()`` (the existing patchable "now" seam refresh-links already
established) is reused unchanged for the staleness cutoff.

Environment hazard (verified, not assumed): this development machine already
has a real ``data/raw/jlcpcb-components.sqlite3`` (~1.6 GB) on disk from a
prior `ingest jlcparts --fetch` run — `cli_mod.RAW_DB_PATH` therefore points
at a REAL, large, existing file by default in THIS environment (though it is
gitignored and would be absent on a fresh clone/CI). Every test below that
invokes `partgraph refresh` and expects it to proceed past the initial flag
validation therefore explicitly monkeypatches `cli_mod.RAW_DB_PATH` to an
isolated `tmp_path` location (present-but-empty, or deliberately absent for
the missing-file tests) — mirroring test_cli_ingest.py's own established
`monkeypatch.setattr("partgraph.cli.RAW_DB_PATH", ...)` isolation pattern —
so no test ever touches that real file regardless of which machine runs the
suite. The one exception is the plain `--help` and `--limit <bad>` tests: the
former never executes the command body at all, and the latter delegates to
the ALREADY-SHIPPED, already-tested `_validate_limit` helper, which is
confirmed (by reading cli.py) to exit before any `dest`/file access in every
sibling command (`ingest jlcparts`, `refresh-links`) — so those two test
groups are left unpatched, matching the refresh-links precedent's own
(unpatched) `--limit`/`--help` tests exactly.

AC-RS-1 (schema) is deliberately placed in THIS file, not test_refresh_stock.py:
it is a plain, standalone assertion against schema/partgraph.dql's text that
has no dependency whatsoever on `partgraph.refresh.stock` (the yet-nonexistent
leaf) or on any new `cli.*` symbol. Since this file's own module-level imports
(`partgraph.cli`) already succeed today (embed/refresh-links are already
merged), placing AC-RS-1 here means it fails as its OWN clean, attributable
assertion failure (schema text missing `stock_checked_at`) in the RED run,
rather than being silently swallowed into test_refresh_stock.py's blanket
module-level ImportError/collection-error for the whole file.

Idempotency (AC-RS-13) split across files, mirroring AC-RL-13's split: this
file pins the CLI-level half only — that the staleness cutoff
`_select_parts_for_refresh` embeds is deterministically derived from the
injected `_utcnow` seam (byte-identical across repeated calls sharing the
same fixed "now"), which is the necessary client-side precondition for
Dgraph's own server-side `@filter` to skip a freshly-stamped part on a
same-window re-run. A mocked-client unit test cannot observe the Dgraph
server's own filter *evaluation* (there is no real Dgraph in this suite), so
this file is explicit that it proves only query-construction determinism, not
end-to-end server-side filtering — the latter would require an integration
test against a live Dgraph instance, out of scope here. The leaf-level half
(a duplicate uid within one page is checked exactly once) is pinned in
test_refresh_stock.py.

AC-RS-15's structural-isolation guarantee is likewise split: this file pins
the CLI-level half (`_select_parts_for_refresh` / `_refresh_stock_all_pages`
source purity) plus the cross-PR constant regression anchors
(`_EMBED_SELECT_PAGE_SIZE`, `_REFRESH_SELECT_PAGE_SIZE`, `_REFRESH_DB_ERROR`
text) — mirroring test_cli_refresh_links.py's own AC-RL-15 tests exactly. The
leaf-level half (`refresh_stock_write` source purity) is pinned in
test_refresh_stock.py.

Gate 3 (security review, test-contract stage) — SHOULD hardenings added
test-first, RED against the not-yet-written impl:
  - ``_load_stock_index`` non-ValueError path: patches the source-loading
    seam to raise ``sqlite3.DatabaseError("file is not a database")`` — a
    corrupt, LOCALLY CACHED snapshot (``--fetch`` is NOT passed; this is not
    a re-fetch scenario), distinct from the adapter's own ``ValueError``
    already covered under AC-RS-12. Forecloses a narrow ``except ValueError``
    implementation that would let a ``DatabaseError`` propagate as a raw,
    leaking traceback instead of the same path-free handling every other
    failure mode gets.
  - ``_stage_fetch`` failure via ``refresh --fetch``: patches ``_stage_fetch``
    itself to raise at this NEW call site (refresh is the first command
    besides ``ingest jlcparts`` to reuse it) and asserts the failure is
    handled path-freely here too, independent of whatever ``_stage_fetch``'s
    own internal handler would otherwise do (bypassed entirely by the mock).

Each new test below follows the same "positive, attributable" discipline as
the fix already applied to test_ac_rs_12_adapter_value_error_exits_1_path_free
(an assertion that the patched seam was actually CALLED) — a lesson learned
directly from running the PR 1 RED suite: an all-negative-assertion test
(only checking things that must NOT appear) is trivially satisfied by the
UNRELATED "No such command 'refresh'" error that fires before this command
exists, which would silently mask the real RED reason.

NOTE: `partgraph.cli` already exists (embed/refresh-links are merged), so this
file collects fine; each test below fails at RUN time — either Click reports
"No such command 'refresh'" (exit code 2) or a plain AttributeError fires when
a test references a cli.* symbol that does not exist yet. Both are the
correct, clearly-attributed RED state before PR 2 implementation.
"""

from __future__ import annotations

import json
import os

# Pin a wide terminal so Rich/Typer never wraps long tokens. Must precede the
# partgraph.cli import: Rich caches terminal width at Console construction.
os.environ["COLUMNS"] = "200"

import re  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from partgraph.cli import _EMBED_DB_ERROR, _REFRESH_DB_ERROR, app  # noqa: E402, F401

RUNNER = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _StrippedResult:
    def __init__(self, result: object) -> None:
        self._result = result

    @property
    def output(self) -> str:
        return _ANSI_RE.sub("", self._result.output)

    def __getattr__(self, name: str) -> object:
        return getattr(self._result, name)


def _invoke(args: list[str]) -> _StrippedResult:
    return _StrippedResult(RUNNER.invoke(app, args))


def _fmt(dt: datetime) -> str:
    """RFC-3339 'Z'-suffixed UTC string — matches the leaf module's convention
    (duplicated locally, on purpose; see module docstring)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Deliberately distinct from test_refresh_links.py (2030-01-15),
# test_cli_refresh_links.py (2030-06-01) and test_refresh_stock.py's own fixed
# moment (2031-03-03), so a failure is never ambiguous about its origin.
_FIXED_NOW = datetime(2031, 9, 9, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Mock pydgraph client/txn helpers (mirrors test_cli_refresh_links.py's
# _make_write_txn / _make_cursor_aware_read_txn / _make_dispatch_client /
# _selection_query_calls, adapted: no reverse-edge purge lookup exists for
# stock refresh, so the dispatcher is simpler).
# ---------------------------------------------------------------------------

def _make_write_txn() -> MagicMock:
    txn = MagicMock()
    txn.mutate.return_value = MagicMock()
    txn.commit.return_value = None
    txn.discard.return_value = None
    txn.__enter__ = MagicMock(return_value=txn)
    txn.__exit__ = MagicMock(return_value=False)
    return txn


def _part_row(uid: str, lcsc_id: str = "C1") -> dict:
    return {"uid": uid, "lcsc_id": lcsc_id}


def _make_cursor_aware_read_txn(pages: list[dict]) -> MagicMock:
    """Serve *pages* (Part-selection responses) in call order to any query
    containing 'type(Part)', 'first:' AND 'stock_checked_at' — the last
    condition disambiguates this selection from embed's own
    type(Part)+first: selection query (which filters on 'embedding', never
    'stock_checked_at'), so the two selection kinds can never be confused
    even though both root at the same Dgraph type.

    If more selection queries are issued than *pages* provides, the next call
    raises StopIteration — a fast, bounded failure (never a hang) that flags a
    pagination loop which does not terminate on its own (mirrors
    test_cli_embed.py's _make_cursor_aware_read_txn).
    """
    remaining_pages = iter(pages)

    def _query_side_effect(query_text, *args, **kwargs):
        resp = MagicMock()
        if (
            "type(Part)" in query_text
            and "first:" in query_text
            and "stock_checked_at" in query_text
        ):
            resp.json = json.dumps(next(remaining_pages)).encode()
        else:
            resp.json = json.dumps({"q": []}).encode()
        return resp

    txn = MagicMock()
    txn.query.side_effect = _query_side_effect
    txn.discard.return_value = None
    txn.__enter__ = MagicMock(return_value=txn)
    txn.__exit__ = MagicMock(return_value=False)
    return txn


def _make_dispatch_client(
    read_txn: MagicMock, write_type_txns: list[MagicMock] | None = None
) -> MagicMock:
    """Route client.txn(read_only=True) -> read_txn; every other client.txn()
    call pops the next mock from *write_type_txns* (or a fresh one)."""
    client = MagicMock()
    queue = list(write_type_txns or [])

    def _factory(*args, **kwargs):
        if kwargs.get("read_only"):
            return read_txn
        return queue.pop(0) if queue else _make_write_txn()

    client.txn.side_effect = _factory
    return client


def _selection_query_calls(read_txn: MagicMock) -> list[str]:
    return [
        c.args[0] for c in read_txn.query.call_args_list
        if c.args
        and "type(Part)" in c.args[0]
        and "first:" in c.args[0]
        and "stock_checked_at" in c.args[0]
    ]


def _patch_dgraph(mock_client: MagicMock):
    import partgraph.cli as cli_mod
    return patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock()))


def _patch_utcnow(fixed: datetime):
    import partgraph.cli as cli_mod
    return patch.object(cli_mod, "_utcnow", lambda: fixed, create=True)


def _patch_load_stock_index(prebuilt: dict):
    import partgraph.cli as cli_mod
    return patch.object(cli_mod, "_load_stock_index", return_value=prebuilt, create=True)


def _existing_dummy_source(tmp_path: Path) -> Path:
    """Create and return a real (empty) dummy source-file path.

    Used to satisfy a "does the source file exist" check without ever
    touching the real (gitignored, ~1.6 GB) data/raw/jlcpcb-components.sqlite3
    that may already be present on a developer machine (verified present on
    this one). `_load_stock_index` is separately patched in every test using
    this helper, so this dummy (empty, structurally invalid) file's bytes are
    never actually parsed.
    """
    dummy = tmp_path / "dummy-jlcpcb-components.sqlite3"
    dummy.write_bytes(b"")
    return dummy


def _type_block_text(text: str, type_name: str) -> str:
    """Return the raw field-list text of `type <type_name> { ... }` (mirrors
    test_schema_file.py's own helper of the same name/behaviour)."""
    pattern = re.compile(rf"\btype\s+{re.escape(type_name)}\s*\{{(.*?)\}}", re.DOTALL)
    match = pattern.search(text)
    return match.group(1) if match else ""


# ===========================================================================
# AC-RS-1: schema addition (stock_checked_at: datetime, no index)
# ===========================================================================

def test_ac_rs_1_schema_declares_stock_checked_at_datetime_with_no_index() -> None:
    """AC-RS-1: Given schema/partgraph.dql.
    When we scan for the stock_checked_at predicate declaration.
    Then it is declared with type 'datetime' and carries NO '@index(...)'
    directive (mirrors verified_at's own index-free datetime declaration —
    the staleness filter uses plain lt()/has(), which Dgraph v25.3.4 already
    supports without an index, per the existing fail_count/verified_at
    precedent in this same schema file).
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    schema_path = repo_root / "schema" / "partgraph.dql"
    assert schema_path.exists(), f"{schema_path} does not exist."
    text = schema_path.read_text(encoding="utf-8")

    declaration_line = ""
    for line in text.splitlines():
        if line.lstrip().startswith("stock_checked_at:"):
            declaration_line = line
            break

    assert declaration_line, (
        "AC-RS-1: predicate 'stock_checked_at' not declared in "
        "schema/partgraph.dql."
    )
    assert "datetime" in declaration_line, (
        f"AC-RS-1: 'stock_checked_at' must be declared as type datetime. "
        f"Got: {declaration_line!r}"
    )
    assert "@index" not in declaration_line, (
        f"AC-RS-1: 'stock_checked_at' must carry NO index. "
        f"Got: {declaration_line!r}"
    )


def test_ac_rs_1_part_type_block_includes_stock_checked_at() -> None:
    """AC-RS-1: Given the `type Part { ... }` block in schema/partgraph.dql.
    When we inspect its field list.
    Then it includes 'stock_checked_at'.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    schema_path = repo_root / "schema" / "partgraph.dql"
    text = schema_path.read_text(encoding="utf-8")

    block = _type_block_text(text, "Part")
    assert block, "AC-RS-1: 'type Part { ... }' not found in schema/partgraph.dql."
    assert re.search(r"\bstock_checked_at\b", block), (
        f"AC-RS-1: type Part must include 'stock_checked_at'. "
        f"Found block:\n{block}"
    )


@pytest.mark.parametrize(
    "existing_field",
    ["stock", "is_basic", "price_usd", "lcsc_id", "xid", "mpn"],
)
def test_ac_rs_1_part_type_still_includes_existing_fields(existing_field: str) -> None:
    """AC-RS-1: Given the Part type declaration is extended with
    stock_checked_at.
    When we inspect its field list.
    Then every pre-existing commerce-relevant field is still present — adding
    stock_checked_at must not displace any existing field.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    schema_path = repo_root / "schema" / "partgraph.dql"
    text = schema_path.read_text(encoding="utf-8")

    block = _type_block_text(text, "Part")
    assert block, "AC-RS-1: 'type Part { ... }' not found in schema/partgraph.dql."
    assert re.search(rf"\b{re.escape(existing_field)}\b", block), (
        f"AC-RS-1: type Part must still include {existing_field!r} after "
        f"adding stock_checked_at. Found block:\n{block}"
    )


# ===========================================================================
# AC-RS-2: flags / help / validation
# ===========================================================================

def test_ac_rs_2_partgraph_help_lists_refresh_as_a_distinct_command() -> None:
    """AC-RS-2: Given the partgraph CLI.
    When `partgraph --help` is invoked.
    Then the output lists a distinct 'refresh' command — NOT merely as a
    substring of the pre-existing 'refresh-links' command name.
    """
    result = _invoke(["--help"])
    assert re.search(r"\brefresh\b(?!-links)", result.output), (
        f"AC-RS-2: partgraph --help must list a DISTINCT 'refresh' command "
        f"(not just as a substring of 'refresh-links'). Got:\n{result.output}"
    )


def test_ac_rs_2_partgraph_help_still_lists_refresh_links() -> None:
    """Regression guard (AC-RS-15 spirit): Given the partgraph CLI after
    adding the new 'refresh' command.
    When `partgraph --help` is invoked.
    Then the pre-existing 'refresh-links' command is still listed, unaffected.
    """
    result = _invoke(["--help"])
    assert "refresh-links" in result.output, (
        f"Adding 'refresh' must not disturb the existing 'refresh-links' "
        f"command. Got:\n{result.output}"
    )


def test_ac_rs_2_refresh_help_exits_zero_and_shows_exact_flags() -> None:
    """AC-RS-2: Given the refresh command.
    When `partgraph refresh --help` is invoked.
    Then exit code is 0 and the output shows EXACTLY --stale-days (default
    7), --limit, --fetch, --force — and does NOT show --timeout or
    --max-failures (refresh has no HTTP timeout or failure-threshold concept;
    those belong to refresh-links).
    """
    result = _invoke(["refresh", "--help"])
    assert result.exit_code == 0, (
        f"AC-RS-2: refresh --help must exit 0. Got {result.exit_code}.\n"
        f"{result.output}"
    )
    assert "sage" in result.output, f"AC-RS-2: must contain 'Usage'. Got:\n{result.output}"

    assert "--stale-days" in result.output, result.output
    assert "[default: 7]" in result.output, (
        f"AC-RS-2: --stale-days must default to 7. Got:\n{result.output}"
    )
    assert "--limit" in result.output, result.output
    assert "--fetch" in result.output, result.output
    assert "--force" in result.output, result.output

    assert "--timeout" not in result.output, (
        f"AC-RS-2: refresh must NOT show --timeout. Got:\n{result.output}"
    )
    assert "--max-failures" not in result.output, (
        f"AC-RS-2: refresh must NOT show --max-failures. Got:\n{result.output}"
    )


@pytest.mark.parametrize("bad_limit", ["0", "abc"])
def test_ac_rs_2_limit_invalid_exits_1_reuses_validate_limit_message(bad_limit: str) -> None:
    """AC-RS-2: Given --limit 0 or --limit abc.
    When `partgraph refresh --limit <bad>` is invoked.
    Then exit code is non-zero and the output contains the EXACT existing
    "--limit must be a positive integer." message (proving _validate_limit is
    reused, not re-implemented). _validate_limit is confirmed (by reading
    cli.py) to exit before any dest/file access in every sibling command, so
    this test needs no RAW_DB_PATH isolation.
    """
    result = _invoke(["refresh", "--limit", bad_limit])
    assert result.exit_code != 0, (
        f"AC-RS-2: --limit {bad_limit!r} must exit non-zero. Got "
        f"{result.exit_code}.\n{result.output}"
    )
    assert "--limit must be a positive integer." in result.output, (
        f"AC-RS-2: must reuse the exact _validate_limit message. "
        f"Got:\n{result.output!r}"
    )


# ===========================================================================
# AC-RS-3: selection query (Part + lcsc_id + parenthesized staleness filter)
# ===========================================================================

def test_ac_rs_3_selection_query_targets_part_with_exact_parenthesized_filter() -> None:
    """AC-RS-3: Given an injected fixed 'now' and stale_days=7.
    When cli._select_parts_for_refresh(client, limit, stale_days=7) is called.
    Then the query text roots at type(Part), selects uid and lcsc_id, and
    carries the EXACT parenthesized filter
    '@filter(has(lcsc_id) AND (NOT has(stock_checked_at) OR
    lt(stock_checked_at, "<T>")))' with T = now - 7 days — pinning the exact
    parenthesization: a mis-grouped 'A AND B OR C' (missing the inner parens)
    would wrongly select lcsc_id-less parts whenever the OR's second arm is
    true, which this exact-string assertion forecloses.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)
    expected_t = _fmt(_FIXED_NOW - timedelta(days=7))

    with _patch_utcnow(_FIXED_NOW):
        cli_mod._select_parts_for_refresh(client, 100, stale_days=7)

    query_text = read_txn.query.call_args.args[0]

    assert "type(Part)" in query_text, f"Got query: {query_text!r}"
    assert "uid" in query_text and "lcsc_id" in query_text, f"Got query: {query_text!r}"

    expected_filter = (
        f'@filter(has(lcsc_id) AND (NOT has(stock_checked_at) OR '
        f'lt(stock_checked_at, "{expected_t}")))'
    )
    assert expected_filter in query_text, (
        f"AC-RS-3: expected the EXACT parenthesized filter {expected_filter!r} "
        f"in the query text. Got: {query_text!r}"
    )


def test_ac_rs_3_selection_query_excludes_datasheet_type_and_embedding_filter() -> None:
    """AC-RS-3: Given the same selection call.
    Then the query text contains NEITHER 'type(Datasheet)' NOR
    'NOT has(embedding)' — proving this is an independent Part+lcsc_id
    selection, not a reuse/mutation of the refresh-links or embed selection
    queries.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)

    with _patch_utcnow(_FIXED_NOW):
        cli_mod._select_parts_for_refresh(client, 100, stale_days=7)

    query_text = read_txn.query.call_args.args[0]
    assert "type(Datasheet)" not in query_text, f"Got query: {query_text!r}"
    assert "NOT has(embedding)" not in query_text, f"Got query: {query_text!r}"


def test_ac_rs_3_selection_does_not_call_select_parts_for_embed_or_datasheets() -> None:
    """AC-RS-3: Given cli._select_parts_for_embed and
    cli._select_datasheets_for_refresh are spied on.
    When cli._select_parts_for_refresh runs.
    Then neither is ever called.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)

    with _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_select_parts_for_embed") as embed_spy, \
         patch.object(cli_mod, "_select_datasheets_for_refresh") as links_spy:
        cli_mod._select_parts_for_refresh(client, 100, stale_days=7)

    embed_spy.assert_not_called()
    links_spy.assert_not_called()


# ===========================================================================
# AC-RS-4: uid keyset cursor (mirrors embed's AC-EC-8 / refresh-links' AC-RL-4)
# ===========================================================================

def test_ac_rs_4_page_one_omits_after(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AC-RS-4: Given no prior page.
    When `partgraph refresh` runs its first selection page.
    Then that page's query has NO 'after:' clause.
    """
    import partgraph.cli as cli_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    page1 = {"q": [_part_row("0xF001")]}
    page2_empty = {"q": []}
    read_txn = _make_cursor_aware_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])

    with _patch_dgraph(client), _patch_load_stock_index({"C1": (1, 1.0, False)}), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    selection_queries = _selection_query_calls(read_txn)
    assert selection_queries, "expected at least one selection query"
    assert "after:" not in selection_queries[0], (
        f"AC-RS-4: page 1 must have NO 'after:' clause. Got: {selection_queries[0]!r}"
    )


def test_ac_rs_4_page_two_carries_after_numeric_max_uid_mixed_digit_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-4: Given a page containing mixed hex-digit-length uids ("0x9" and
    "0x10").
    When refresh pages past this block.
    Then the next page's query carries 'after: 0x10' (the NUMERICALLY larger
    uid; 0x10 == 16 > 0x9 == 9) and NEVER 'after: 0x9' (a lexicographic string
    max would wrongly pick "0x9").
    """
    import partgraph.cli as cli_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    page1 = {"q": [_part_row("0x9"), _part_row("0x10")]}
    page2_empty = {"q": []}
    read_txn = _make_cursor_aware_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])

    with _patch_dgraph(client), _patch_load_stock_index({"C1": (1, 1.0, False)}), \
         _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_REFRESH_STOCK_SELECT_PAGE_SIZE", 2, create=True):
        result = _invoke(["refresh"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 2, (
        f"AC-RS-4: expected exactly 2 selection queries. Got "
        f"{len(selection_queries)}: {selection_queries!r}"
    )
    assert "after: 0x10" in selection_queries[1], (
        f"AC-RS-4: cursor must be the numerically larger uid (0x10). "
        f"Got: {selection_queries[1]!r}"
    )
    assert "after: 0x9" not in selection_queries[1], (
        f"AC-RS-4: a lexicographic string max would wrongly select '0x9'. "
        f"Got: {selection_queries[1]!r}"
    )


def test_ac_rs_4_terminates_on_empty_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-4: Given the first selection page is already empty.
    When refresh runs.
    Then it exits 0 after exactly one selection query (no further fetch).
    """
    import partgraph.cli as cli_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])

    with _patch_dgraph(client), _patch_load_stock_index({}), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    assert len(_selection_query_calls(read_txn)) == 1


def test_ac_rs_4_terminates_on_short_page_without_extra_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-4: Given a page shorter than the requested page size.
    When refresh runs (page size patched to 2, but only 1 row returned).
    Then it terminates without an extra fetch.
    """
    import partgraph.cli as cli_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    short_page = {"q": [_part_row("0xF010")]}
    read_txn = _make_cursor_aware_read_txn([short_page])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])

    with _patch_dgraph(client), _patch_load_stock_index({"C1": (1, 1.0, False)}), \
         _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_REFRESH_STOCK_SELECT_PAGE_SIZE", 2, create=True):
        result = _invoke(["refresh"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    assert len(_selection_query_calls(read_txn)) == 1, (
        "AC-RS-4: a short page must terminate the run without a further fetch."
    )


def test_ac_rs_4_defensive_guard_on_non_advancing_cursor_path_free_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-4: Given a mocked server that keeps returning the SAME full page
    (the max uid never advances).
    When refresh pages past it.
    Then the defensive guard breaks out after exactly 2 selection queries,
    exits 0, and prints a path-free stall notice.
    """
    import partgraph.cli as cli_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    same_full_page = {"q": [_part_row("0xE001"), _part_row("0xE002")]}
    read_txn = _make_cursor_aware_read_txn(
        [same_full_page, same_full_page, same_full_page]
    )
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])

    with _patch_dgraph(client), _patch_load_stock_index({"C1": (1, 1.0, False)}), \
         _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_REFRESH_STOCK_SELECT_PAGE_SIZE", 2, create=True):
        result = _invoke(["refresh"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    assert len(_selection_query_calls(read_txn)) == 2, (
        f"AC-RS-4: the defensive guard must stop after exactly 2 selection "
        f"calls. Got: {_selection_query_calls(read_txn)!r}"
    )
    output_lower = result.output.lower()
    stall_phrases = (
        "did not advance", "not advance", "stopping early", "stopped early",
        "no further progress", "cursor did not move", "cursor stalled",
    )
    assert any(phrase in output_lower for phrase in stall_phrases), (
        f"AC-RS-4: output must contain an explicit stall notice. "
        f"Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RS-4: the stall notice must be path-free."
    )


def test_ac_rs_4_malformed_or_missing_uid_never_interpolated_raw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-4: Given selection rows with an invalid uid (missing entirely,
    and a value shaped like a query-injection payload) alongside one valid
    row.
    When refresh pages past this block.
    Then the run completes successfully (a bad row is skipped, never a
    crash), the cursor computation uses ONLY the one valid uid, and neither
    invalid value is EVER interpolated raw into a subsequent query or leaked
    into CLI output.
    """
    import partgraph.cli as cli_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    malformed_uid = '0x1) } mutation { set { _:x <bad> "1" . } } #'
    page1 = {
        "q": [
            {"lcsc_id": "C-nouid"},
            {"uid": malformed_uid, "lcsc_id": "C-bad"},
            _part_row("0xB002", "C-good"),
        ]
    }
    page2_empty = {"q": []}
    read_txn = _make_cursor_aware_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])

    with _patch_dgraph(client), _patch_load_stock_index({"C-good": (1, 1.0, False)}), \
         _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_REFRESH_STOCK_SELECT_PAGE_SIZE", 3, create=True):
        result = _invoke(["refresh"])

    assert result.exit_code == 0, (
        f"AC-RS-4: a page containing a missing/malformed uid alongside a "
        f"valid row must still complete successfully. Got "
        f"{result.exit_code}.\n{result.output}"
    )

    all_query_texts = [c.args[0] for c in read_txn.query.call_args_list if c.args]
    assert all_query_texts, "expected at least one query to have been issued"
    for query_text in all_query_texts:
        assert malformed_uid not in query_text, (
            f"AC-RS-4: malformed uid must never be interpolated raw. "
            f"Found in: {query_text!r}"
        )
        assert "after: None" not in query_text, (
            f"AC-RS-4: a missing uid must never render as literal 'after: None'. "
            f"Found in: {query_text!r}"
        )

    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 2, (
        f"AC-RS-4: expected page1 (with the bad rows) then page2 (empty). "
        f"Got {len(selection_queries)}: {selection_queries!r}"
    )
    assert "after: 0xB002" in selection_queries[1], (
        f"AC-RS-4: cursor must skip the missing/malformed uids and use the "
        f"one valid uid (0xB002). Got: {selection_queries[1]!r}"
    )
    assert malformed_uid not in result.output, (
        f"AC-RS-4: the malformed value must never leak into CLI output. "
        f"Got:\n{result.output!r}"
    )


# ===========================================================================
# AC-RS-5 (CLI half): the stock_index is built ONCE and the SAME object is
# threaded into every page's refresh_stock_write call.
# ===========================================================================

def test_ac_rs_5_cli_builds_stock_index_once_and_threads_same_object_into_every_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-5 (CLI half): Given `partgraph refresh` pages across THREE
    selection pages (page size patched to 2).
    When the run executes, with partgraph.refresh.stock.refresh_stock_write
    replaced by a bare mock (return_value is a well-formed summary dict, so
    the CLI's own totals-aggregation logic keeps working unmodified).
    Then refresh_stock_write is called more than once (multiple pages), and
    EVERY call's stock_index= keyword argument is the SAME object, by
    identity — proving the index is built ONCE up front (never rebuilt per
    page) and threaded through unchanged. Patches both the leaf module
    (partgraph.refresh.stock) and, if present, an eagerly-imported name on
    cli_mod — robust to either an eager or lazy import style in the impl
    (mirrors test_cli_refresh_links.py's AC-RL-7 CLI-half spy pattern).
    """
    from contextlib import ExitStack

    import partgraph.cli as cli_mod
    from partgraph.refresh import stock as stock_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    page1 = {"q": [_part_row("0xG001", "C1"), _part_row("0xG002", "C2")]}
    page2 = {"q": [_part_row("0xG003", "C3")]}
    page3_empty = {"q": []}
    read_txn = _make_cursor_aware_read_txn([page1, page2, page3_empty])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn, write_txn])
    prebuilt_index = {"C1": (1, 1.0, False)}

    fake_write = MagicMock(return_value={"checked": 0, "matched": 0, "absent": 0})

    with ExitStack() as stack:
        stack.enter_context(_patch_dgraph(client))
        stack.enter_context(_patch_load_stock_index(prebuilt_index))
        stack.enter_context(_patch_utcnow(_FIXED_NOW))
        stack.enter_context(
            patch.object(cli_mod, "_REFRESH_STOCK_SELECT_PAGE_SIZE", 2, create=True)
        )
        stack.enter_context(patch.object(stock_mod, "refresh_stock_write", fake_write))
        if hasattr(cli_mod, "refresh_stock_write"):
            stack.enter_context(patch.object(cli_mod, "refresh_stock_write", fake_write))
        result = _invoke(["refresh"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    assert fake_write.call_count >= 2, (
        f"AC-RS-5: expected at least 2 pages -> at least 2 refresh_stock_write "
        f"calls. Got {fake_write.call_count}."
    )
    indexes = [kwargs.get("stock_index") for _, kwargs in fake_write.call_args_list]
    assert all(idx is not None for idx in indexes), (
        f"AC-RS-5: every refresh_stock_write call must carry a stock_index= "
        f"keyword. Got call_args_list: {fake_write.call_args_list!r}"
    )
    assert all(idx is prebuilt_index for idx in indexes), (
        "AC-RS-5: the CLI must build the stock_index ONCE and thread the "
        "SAME object (by identity) into every refresh_stock_write call — "
        "not rebuild it per page."
    )


# ===========================================================================
# AC-RS-12: source loading (--fetch reuses _stage_fetch; missing-file /
# adapter-ValueError error paths)
# ===========================================================================

def test_ac_rs_12_fetch_flag_reuses_stage_fetch_no_real_download_or_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-12: Given `partgraph refresh --fetch` is invoked, with
    cli._stage_fetch and the source-loading seam (_load_stock_index) both
    patched.
    Then _stage_fetch is called exactly once as _stage_fetch(dest,
    force=False) — proving reuse of the EXISTING helper (not a
    reimplementation) — _load_stock_index runs afterwards, exit code is 0,
    and NO real network socket is ever opened (no real download, no real
    sqlite parse).
    """
    import partgraph.cli as cli_mod

    dummy = _existing_dummy_source(tmp_path)
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", dummy)

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)
    prebuilt_index = {"C1": (1, 1.0, False)}

    fake_stage_fetch = MagicMock()
    fake_load_index = MagicMock(return_value=prebuilt_index)

    with _patch_dgraph(client), _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_stage_fetch", fake_stage_fetch), \
         patch.object(cli_mod, "_load_stock_index", fake_load_index, create=True), \
         patch("socket.create_connection") as mock_conn:
        result = _invoke(["refresh", "--fetch"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    fake_stage_fetch.assert_called_once_with(dummy, force=False)
    assert fake_load_index.called, (
        "AC-RS-12: the source-loading seam must run after --fetch completes."
    )
    assert not mock_conn.called, (
        "AC-RS-12: --fetch must never open a real network socket in the "
        "unit suite (the source-loading seam is patched)."
    )


def test_ac_rs_12_fetch_and_force_threads_force_true_into_stage_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-12: Given `partgraph refresh --fetch --force` is invoked.
    Then _stage_fetch is called with force=True (threading --force through
    to the reused _stage_fetch helper, exactly as `ingest jlcparts --fetch
    --force` already does).
    """
    import partgraph.cli as cli_mod

    dummy = _existing_dummy_source(tmp_path)
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", dummy)

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)

    fake_stage_fetch = MagicMock()
    fake_load_index = MagicMock(return_value={"C1": (1, 1.0, False)})

    with _patch_dgraph(client), _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_stage_fetch", fake_stage_fetch), \
         patch.object(cli_mod, "_load_stock_index", fake_load_index, create=True):
        result = _invoke(["refresh", "--fetch", "--force"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    fake_stage_fetch.assert_called_once_with(dummy, force=True)


def test_ac_rs_12_no_fetch_missing_file_exits_1_relative_path_hints_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-12: Given no --fetch and the source file is absent.
    When `partgraph refresh` is invoked.
    Then exit code is non-zero, the error message contains the RELATIVE
    RAW_DB_RELPATH (never the absolute tmp_path location), hints --fetch as
    the remedy, and _load_stock_index is never called (the missing-file check
    short-circuits before any parse attempt).
    """
    import partgraph.cli as cli_mod

    absent = tmp_path / "absent-jlcpcb-components.sqlite3"  # deliberately never created
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", absent)

    fake_load_index = MagicMock()

    with patch.object(cli_mod, "_load_stock_index", fake_load_index, create=True):
        result = _invoke(["refresh"])

    assert result.exit_code != 0, (
        f"AC-RS-12: a missing source file with no --fetch must exit "
        f"non-zero. Got {result.exit_code}."
    )
    assert cli_mod.RAW_DB_RELPATH in result.output, (
        f"AC-RS-12: the error must name the RELATIVE RAW_DB_RELPATH "
        f"({cli_mod.RAW_DB_RELPATH!r}). Got:\n{result.output!r}"
    )
    assert "--fetch" in result.output, (
        f"AC-RS-12: the error must hint --fetch as the remedy. "
        f"Got:\n{result.output!r}"
    )
    assert str(absent) not in result.output, (
        f"AC-RS-12: the ABSOLUTE tmp_path source location must never leak. "
        f"Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RS-12: the missing-file error must be path-free."
    )
    fake_load_index.assert_not_called()


def test_ac_rs_12_adapter_value_error_exits_1_path_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-12: Given the source-loading seam raises a ValueError (mirroring
    JlcpartsAdapter's own unrecognized-schema failure mode).
    When `partgraph refresh` is invoked.
    Then _load_stock_index is actually reached and raises (a POSITIVE,
    attributable check — proving the failure is exercised via the real
    source-loading path, not merely any unrelated non-zero exit such as a
    "no such command" error, which would otherwise trivially satisfy the
    negative-only checks below before this command even exists), exit code
    is non-zero, and NEITHER the raw ValueError text NOR any absolute
    filesystem path ever leaks into the output — a stricter, path-free bar
    than `ingest jlcparts`'s own normalize-failure handler (which does
    interpolate `{exc}`); refresh's error handling must never do that.
    """
    import partgraph.cli as cli_mod

    dummy = _existing_dummy_source(tmp_path)
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", dummy)

    detail = "Unrecognized jlcparts schema: weirdcol123"
    fake_load_index = MagicMock(side_effect=ValueError(detail))

    with patch.object(cli_mod, "_load_stock_index", fake_load_index, create=True):
        result = _invoke(["refresh"])

    assert fake_load_index.called, (
        "AC-RS-12: the source-loading seam must actually be reached (this "
        "fails today because the 'refresh' command does not exist yet, "
        "which is the correct attributable RED reason — not a coincidental "
        "unrelated non-zero exit)."
    )
    assert result.exit_code != 0, (
        f"AC-RS-12: an adapter ValueError must exit non-zero. "
        f"Got {result.exit_code}."
    )
    assert detail not in result.output, (
        f"AC-RS-12: the raw ValueError text must never leak. "
        f"Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RS-12: the adapter-failure error must be path-free."
    )


def test_gate3_load_stock_index_database_error_exits_nonzero_path_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gate 3 hardening (security): Given the source-loading seam raises
    sqlite3.DatabaseError("file is not a database") — a corrupt, LOCALLY
    CACHED snapshot (--fetch is NOT passed; this is not a re-fetch scenario),
    distinct from the adapter's own ValueError already covered by AC-RS-12.
    Forecloses a narrow `except ValueError` implementation that would let a
    DatabaseError propagate as a raw, leaking traceback.
    When `partgraph refresh` is invoked.
    Then _load_stock_index is actually reached (a POSITIVE, attributable
    check — see module docstring), exit code is non-zero, no raw exception
    text/class name/traceback leaks, the output is path-free, and SOME
    actionable error notice is shown (a _REFRESH_STOCK_DB_ERROR-or-equivalent
    message — this test does not hardcode the exact wording).
    """
    import sqlite3

    import partgraph.cli as cli_mod

    dummy = _existing_dummy_source(tmp_path)
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", dummy)

    detail = "file is not a database"
    fake_load_index = MagicMock(side_effect=sqlite3.DatabaseError(detail))

    with patch.object(cli_mod, "_load_stock_index", fake_load_index, create=True):
        result = _invoke(["refresh"])

    assert fake_load_index.called, (
        "Gate 3: the source-loading seam must actually be reached (this "
        "fails today because the 'refresh' command does not exist yet, "
        "which is the correct attributable RED reason — not a coincidental "
        "unrelated non-zero exit)."
    )
    assert result.exit_code != 0, (
        f"Gate 3: a corrupt-database DatabaseError must exit non-zero. "
        f"Got {result.exit_code}."
    )
    assert detail not in result.output, (
        f"Gate 3: the raw DatabaseError text must never leak. "
        f"Got:\n{result.output!r}"
    )
    assert "DatabaseError" not in result.output, (
        f"Gate 3: the raw exception CLASS NAME must never leak. "
        f"Got:\n{result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"Gate 3: no raw traceback may ever leak. Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "Gate 3: the corrupt-database error must be path-free."
    )
    assert "error" in result.output.lower(), (
        f"Gate 3: SOME actionable error notice must be shown "
        f"(_REFRESH_STOCK_DB_ERROR-or-equivalent). Got:\n{result.output!r}"
    )


def test_gate3_stage_fetch_failure_during_refresh_fetch_exits_nonzero_path_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gate 3 hardening (security): Given `partgraph refresh --fetch` is
    invoked and the reused _stage_fetch helper itself raises — a download
    failure at this NEW call site (refresh is the first command besides
    `ingest jlcparts` to reuse _stage_fetch, so its failure path here must be
    independently proven, not merely assumed from the ingest precedent).
    When the run executes.
    Then _stage_fetch is actually reached (a POSITIVE, attributable check),
    exit code is non-zero, no raw exception text leaks, the output is
    path-free, and the pipeline aborts before ever reaching the
    source-loading seam (mirrors ingest's own
    "fetch failure aborts normalize/load" guarantee).
    """
    import partgraph.cli as cli_mod

    dummy = _existing_dummy_source(tmp_path)
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", dummy)

    detail = "connection reset by peer while downloading"
    fake_stage_fetch = MagicMock(side_effect=RuntimeError(detail))
    fake_load_index = MagicMock()

    with patch.object(cli_mod, "_stage_fetch", fake_stage_fetch), \
         patch.object(cli_mod, "_load_stock_index", fake_load_index, create=True):
        result = _invoke(["refresh", "--fetch"])

    assert fake_stage_fetch.called, (
        "Gate 3: _stage_fetch must actually be reached at this NEW "
        "refresh --fetch call site (this fails today because the "
        "'refresh' command does not exist yet)."
    )
    assert result.exit_code != 0, (
        f"Gate 3: a _stage_fetch failure during refresh --fetch must exit "
        f"non-zero. Got {result.exit_code}."
    )
    assert detail not in result.output, (
        f"Gate 3: the raw _stage_fetch exception text must never leak. "
        f"Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "Gate 3: the fetch-failure error must be path-free."
    )
    fake_load_index.assert_not_called()


# ===========================================================================
# AC-RS-13 (CLI half): staleness cutoff is deterministic (see module
# docstring for what this test can/cannot prove).
# ===========================================================================

def test_ac_rs_13_repeated_calls_with_same_fixed_now_yield_identical_staleness_cutoff() -> None:
    """AC-RS-13 (CLI half): Given TWO independent
    _select_parts_for_refresh(client, ..., stale_days=7) calls sharing the
    SAME injected _utcnow.
    Then both calls' queries carry a BYTE-IDENTICAL staleness cutoff — proving
    the cutoff is deterministically derived from the injected clock, never
    real wall-clock drift. This is the necessary client-side precondition for
    Dgraph's own server-side @filter to skip a freshly-stamped part on a
    same-window re-run; the server-side filter *evaluation* itself is outside
    what a mocked-client unit test can observe (see module docstring).
    """
    import partgraph.cli as cli_mod

    read_txn_1 = _make_cursor_aware_read_txn([{"q": []}])
    client_1 = _make_dispatch_client(read_txn_1)
    read_txn_2 = _make_cursor_aware_read_txn([{"q": []}])
    client_2 = _make_dispatch_client(read_txn_2)

    with _patch_utcnow(_FIXED_NOW):
        cli_mod._select_parts_for_refresh(client_1, 100, stale_days=7)
        cli_mod._select_parts_for_refresh(client_2, 100, stale_days=7)

    query_1 = _selection_query_calls(read_txn_1)[0]
    query_2 = _selection_query_calls(read_txn_2)[0]
    assert query_1 == query_2, (
        f"AC-RS-13: two independent selection calls sharing the same "
        f"injected 'now' must produce a byte-identical query/cutoff. "
        f"Got:\n1: {query_1!r}\n2: {query_2!r}"
    )


# ===========================================================================
# AC-RS-14: DB error path (distinct, path-free constant)
# ===========================================================================

def test_ac_rs_14_selection_txn_raises_exit_1_shows_refresh_stock_db_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-14: Given the selection txn.query raises (DB down).
    When `partgraph refresh` runs.
    Then exit code is 1, the output contains the NEW path-free
    `_REFRESH_STOCK_DB_ERROR` constant's text (hinting `partgraph db up`),
    and no raw exception text leaks.
    """
    import partgraph.cli as cli_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    failing_txn = MagicMock()
    failing_txn.query.side_effect = RuntimeError("connection refused")
    failing_txn.discard.return_value = None
    failing_txn.__enter__ = MagicMock(return_value=failing_txn)
    failing_txn.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.txn.return_value = failing_txn

    with _patch_dgraph(client), _patch_load_stock_index({"C1": (1, 1.0, False)}), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh"])

    assert result.exit_code != 0, (
        f"AC-RS-14: a DB failure must exit non-zero. Got {result.exit_code}."
    )
    assert "partgraph db up" in result.output, (
        f"AC-RS-14: output must contain the 'partgraph db up' hint. "
        f"Got:\n{result.output!r}"
    )
    assert "connection refused" not in result.output, (
        f"AC-RS-14: raw exception text must never leak. Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RS-14: the DB error message must be path-free."
    )


def test_ac_rs_14_write_back_mutation_raises_exit_1_path_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-14: Given selection succeeds (one Part row returned), but the
    WRITE-BACK txn's mutate() itself raises (DB down mid-run).
    When `partgraph refresh` runs.
    Then exit code is 1, output contains the path-free
    `_REFRESH_STOCK_DB_ERROR` hint, and no raw exception text leaks.
    """
    import partgraph.cli as cli_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    page1 = {"q": [_part_row("0xWF1", "C1")]}
    read_txn = _make_cursor_aware_read_txn([page1])
    write_txn = _make_write_txn()
    write_txn.mutate.side_effect = RuntimeError("connection refused")
    client = _make_dispatch_client(read_txn, [write_txn])

    with _patch_dgraph(client), _patch_load_stock_index({"C1": (1, 1.0, False)}), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh"])

    assert result.exit_code != 0, (
        f"AC-RS-14: a write-back mutation failure must exit non-zero. "
        f"Got {result.exit_code}."
    )
    assert "partgraph db up" in result.output, (
        f"AC-RS-14: output must contain the 'partgraph db up' hint even "
        f"for a write-step failure. Got:\n{result.output!r}"
    )
    assert "connection refused" not in result.output, (
        f"AC-RS-14: raw exception text must never leak. Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RS-14: the DB error message must be path-free."
    )


def test_ac_rs_14_refresh_stock_db_error_constant_exists_and_differs_from_others() -> None:
    """AC-RS-14: Given cli._REFRESH_STOCK_DB_ERROR (new constant).
    Then it exists, is a non-empty string mentioning 'partgraph db up', and
    is textually DISTINCT from both the existing cli._EMBED_DB_ERROR and
    cli._REFRESH_DB_ERROR.
    """
    import partgraph.cli as cli_mod

    error = cli_mod._REFRESH_STOCK_DB_ERROR
    assert isinstance(error, str) and error, (
        "AC-RS-14: _REFRESH_STOCK_DB_ERROR must be a non-empty string constant."
    )
    assert "partgraph db up" in error, (
        f"AC-RS-14: _REFRESH_STOCK_DB_ERROR must hint 'partgraph db up'. "
        f"Got: {error!r}"
    )
    assert error != _EMBED_DB_ERROR, (
        "AC-RS-14: _REFRESH_STOCK_DB_ERROR must be textually distinct from "
        "_EMBED_DB_ERROR."
    )
    assert error != _REFRESH_DB_ERROR, (
        "AC-RS-14: _REFRESH_STOCK_DB_ERROR must be textually distinct from "
        "_REFRESH_DB_ERROR (the refresh-links error)."
    )


# ===========================================================================
# AC-RS-15 (CLI half): structural isolation + cross-PR regression anchors
# ===========================================================================

def test_ac_rs_15_select_parts_for_refresh_never_references_forbidden_symbols() -> None:
    """AC-RS-15 (CLI half): Given cli._select_parts_for_refresh's own source
    code.
    Then it never references Loader, a bare 'load(' call, _build_part_obj,
    _embed_all_pages, _select_parts_for_embed, embed_write,
    refresh_links_write, _refresh_all_pages or _select_datasheets_for_refresh.
    """
    import inspect

    import partgraph.cli as cli_mod

    source = inspect.getsource(cli_mod._select_parts_for_refresh)
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
            f"AC-RS-15: _select_parts_for_refresh must not reference "
            f"{name!r}. Source:\n{source}"
        )


def test_ac_rs_15_refresh_stock_all_pages_never_references_forbidden_symbols() -> None:
    """AC-RS-15 (CLI half): Given cli._refresh_stock_all_pages's own source
    code.
    Then it never references any of the same forbidden loader/embed/
    refresh-links symbols.
    """
    import inspect

    import partgraph.cli as cli_mod

    source = inspect.getsource(cli_mod._refresh_stock_all_pages)
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
            f"AC-RS-15: _refresh_stock_all_pages must not reference "
            f"{name!r}. Source:\n{source}"
        )


def test_ac_rs_15_embed_select_page_size_constant_unchanged() -> None:
    """AC-RS-15: Given the pre-existing embed constant.
    Then it retains its original value (10_000) — this new PR must introduce
    its OWN constants, never repurpose embed's.
    """
    import partgraph.cli as cli_mod

    assert cli_mod._EMBED_SELECT_PAGE_SIZE == 10_000


def test_ac_rs_15_refresh_links_select_page_size_constant_unchanged() -> None:
    """AC-RS-15: Given the pre-existing refresh-links constant.
    Then it retains its original value (10_000) — this new PR must not
    repurpose it either.
    """
    import partgraph.cli as cli_mod

    assert cli_mod._REFRESH_SELECT_PAGE_SIZE == 10_000


def test_ac_rs_15_refresh_db_error_text_unchanged() -> None:
    """AC-RS-15: Given the pre-existing _REFRESH_DB_ERROR constant (verified,
    by reading cli.py directly, to currently read exactly as pinned below).
    Then its text is unchanged by this PR.
    """
    import partgraph.cli as cli_mod

    expected = (
        "[red]Error:[/red] could not refresh datasheet links. Is the "
        "database running? Start it with `partgraph db up`."
    )
    assert expected == cli_mod._REFRESH_DB_ERROR, (
        f"AC-RS-15: _REFRESH_DB_ERROR text must be unchanged by this PR. "
        f"Got: {cli_mod._REFRESH_DB_ERROR!r}"
    )


def test_ac_rs_15_refresh_stock_select_page_size_constant() -> None:
    """AC-RS-15 / target contract: Given the NEW _REFRESH_STOCK_SELECT_PAGE_SIZE
    constant.
    Then it equals 10_000 — its own constant, not an alias of the embed or
    refresh-links page-size constants.
    """
    import partgraph.cli as cli_mod

    assert cli_mod._REFRESH_STOCK_SELECT_PAGE_SIZE == 10_000


def test_ac_rs_15_refresh_stock_uid_re_matches_dgraph_uid_shape_only() -> None:
    """AC-RS-15 / target contract: Given cli._REFRESH_STOCK_UID_RE.
    Then it matches ONLY the '0x' + hex-digits Dgraph uid shape (behaviour,
    not the compiled pattern's literal repr).
    """
    import partgraph.cli as cli_mod

    pattern = cli_mod._REFRESH_STOCK_UID_RE
    assert pattern.match("0x1a"), "must match a lowercase-hex uid"
    assert pattern.match("0xFF"), "must match an uppercase-hex uid"
    assert not pattern.match("1a"), "must reject a uid missing the '0x' prefix"
    assert not pattern.match("0x"), "must reject a bare '0x' with no digits"
    assert not pattern.match("0xZZ"), "must reject non-hex characters"


def test_ac_rs_15_refresh_stock_cursor_stall_constant_is_path_free_and_nonempty() -> None:
    """AC-RS-15 / target contract: Given cli._REFRESH_STOCK_CURSOR_STALL.
    Then it is a non-empty, path-free string (mirrors _REFRESH_CURSOR_STALL/
    _EMBED_CURSOR_STALL).
    """
    import partgraph.cli as cli_mod

    notice = cli_mod._REFRESH_STOCK_CURSOR_STALL
    assert isinstance(notice, str) and notice, (
        "AC-RS-15: _REFRESH_STOCK_CURSOR_STALL must be a non-empty string."
    )
    assert not re.search(r"/(?:home|root|Users)/", notice), (
        "AC-RS-15: _REFRESH_STOCK_CURSOR_STALL must be path-free."
    )


# ===========================================================================
# AC-RS-17: semantics (--force no-op without --fetch; --stale-days 0/negative)
# ===========================================================================

def test_ac_rs_17_force_without_fetch_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-17: Given `partgraph refresh --force` is invoked WITHOUT --fetch.
    Then --force has no effect: cli._stage_fetch is never called (force only
    matters when combined with --fetch, to force a re-download).
    """
    import partgraph.cli as cli_mod

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)
    fake_stage_fetch = MagicMock()

    with _patch_dgraph(client), _patch_load_stock_index({}), \
         _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_stage_fetch", fake_stage_fetch):
        result = _invoke(["refresh", "--force"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    fake_stage_fetch.assert_not_called()


def test_ac_rs_17_stale_days_zero_sets_cutoff_equal_to_now() -> None:
    """AC-RS-17: Given --stale-days 0.
    When cli._select_parts_for_refresh(client, ..., stale_days=0) is called
    with a fixed injected 'now'.
    Then the staleness cutoff T embedded in the query equals 'now' EXACTLY
    (re-stamp-all semantics: every part not already checked at this exact
    instant is eligible).
    """
    import partgraph.cli as cli_mod

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)
    expected_t = _fmt(_FIXED_NOW)

    with _patch_utcnow(_FIXED_NOW):
        cli_mod._select_parts_for_refresh(client, 100, stale_days=0)

    query_text = _selection_query_calls(read_txn)[0]
    assert expected_t in query_text, (
        f"AC-RS-17: --stale-days 0 must set the cutoff T == now (exactly). "
        f"Expected {expected_t!r} in query: {query_text!r}"
    )


def test_ac_rs_17_negative_stale_days_exits_nonzero_path_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-RS-17: Given --stale-days -1 (negative — 0 IS valid, only negative
    is rejected).
    When `partgraph refresh --stale-days -1` is invoked.
    Then exit code is non-zero, the output names '--stale-days' and indicates
    it must not be negative, and no absolute path leaks.
    """
    import partgraph.cli as cli_mod

    # Defensive isolation only (see module docstring): validation is expected
    # to short-circuit before any file access, but this guarantees no test in
    # this suite can ever touch the real source file regardless of the
    # implementation's exact validation order.
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_source(tmp_path))

    result = _invoke(["refresh", "--stale-days", "-1"])

    assert result.exit_code != 0, (
        f"AC-RS-17: negative --stale-days must exit non-zero. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert "--stale-days" in result.output, result.output
    assert "negative" in result.output.lower(), (
        f"AC-RS-17: output must indicate --stale-days cannot be negative "
        f"(0 IS valid; only negative is rejected). Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RS-17: the --stale-days validation error must be path-free."
    )
