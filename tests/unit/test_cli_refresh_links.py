"""
Tests: AC-RL-2, AC-RL-3, AC-RL-4, AC-RL-7 (CLI half), AC-RL-12 (end-to-end),
AC-RL-14, AC-RL-15 — `partgraph refresh-links` CLI command

Specifies the behaviour of the NEW `partgraph refresh-links` CLI command
(issue #11, PR 1): flags/help, the Datasheet selection query (staleness
filter, NOT the embed filter), uid-keyset cursor pagination (mirroring embed's
AC-EC-8 pattern exactly), an end-to-end multi-Part purge, the DB-down error
path, and a structural guarantee that the embed pipeline is left untouched.

Gate 3 hardening pass (security FAIL + architecture flag) — closed gaps:
  - AC-RL-7 (CLI half): a dedicated test proves the CLI actually
    CONSTRUCTS a real `partgraph.refresh.links.HostRateLimiter` and threads
    it into `refresh_links_write`'s `rate_limiter=` keyword — an impl that
    accepts the leaf's `rate_limiter` parameter but never builds/passes one
    would otherwise pass every other test in this suite silently.
  - AC-RL-14 extended: a write-back-mutation failure and a
    purge-mutation failure (not just the original selection-read failure)
    must EACH independently exit 1 with the same path-free
    `_REFRESH_DB_ERROR` hint.
  - AC-RL-15 (architecture cross-PR hazard): the embed-constant regression
    guard no longer references `_EMBED_SELECT_DEFAULT`, which the open
    embed-hardening PR #12 deletes — asserting on it here would make this
    very "refresh doesn't touch embed" guard crash with an unrelated
    AttributeError the moment #12 merges. `_EMBED_SELECT_PAGE_SIZE` (which
    #12 keeps) is the stable anchor instead.

Suggested CLI-side names asserted against (per the dispatcher's plan):
  - `_select_datasheets_for_refresh(client, limit, *, stale_days=30, after=None)
    -> list` — one page of Datasheet rows (uid/url/http_status/fail_count).
  - `_refresh_all_pages(client, *, http_client, max_failures, timeout,
    remaining, progress_bar) -> dict` — pages through the whole run, returns
    the aggregated {"checked","alive","dead","purged"} summary.
  - `_REFRESH_DB_ERROR` — path-free, "partgraph db up"-hinting constant,
    distinct from the existing `_EMBED_DB_ERROR`.
  - `_REFRESH_SELECT_DEFAULT = 200_000`, `_REFRESH_SELECT_PAGE_SIZE = 10_000`.
  - `_REFRESH_CURSOR_STALL` — path-free stall notice (mirrors
    `_EMBED_CURSOR_STALL`).
  - the uid-keyset cursor reuses the SAME shape-validation as embed's
    `_UID_RE` (``^0x[0-9a-fA-F]+$``); this file tests the OBSERVABLE cursor
    behaviour (query text), not the private regex object's identity.

NEW symbol flagged for the impl gate (not in the dispatcher's suggested-names
list): `_build_http_client()` — a lazy httpx-client factory mirroring the
existing `_build_dgraph_client()` pattern, patched here exactly like that
function is patched in test_cli_embed.py, so `refresh-links` never opens a
real socket in the unit suite. Also `_utcnow()` — a patchable "current time"
seam mirroring how `get_encoder` is imported at module level "ON PURPOSE ...
the test suite patches it" (see cli.py's own comment); `refresh-links` needs
an equivalent injectable clock so the staleness cutoff T (`now - stale_days`)
is deterministic in tests, never real wall-clock.

This file deliberately does NOT import `partgraph.refresh.links` (unlike
test_refresh_links.py) so its collection/RED failures are attributable
strictly to the CLI layer (missing command / missing cli.* symbols), not a
transitively missing leaf module. Expected timestamp strings are computed
locally via `_fmt()` (RFC-3339 'Z'-suffixed UTC), matching the leaf module's
`format_verified_at` convention without depending on it.

AC-RL-15's "embed tests still pass" half is NOT re-asserted inside this file
(that would be an oddly circular, non-deterministic meta-test); it is
verified externally by running test_embed.py + test_cli_embed.py in the same
pytest invocation as these new files (see the red-run command). This file
only pins the STRUCTURAL half: the new refresh-links functions' own source
must never reference the embed helpers.

NOTE: `partgraph.cli` already exists (embed/search/show/stats are merged), so
this file collects fine; each test below fails at RUN time — either Click
reports "No such command 'refresh-links'" (exit code 2) or a plain
AttributeError fires when a test references a cli.* symbol that does not
exist yet. Both are the correct, clearly-attributed RED state before PR 1
implementation.
"""

from __future__ import annotations

import json
import os

# Pin a wide terminal so Rich/Typer never wraps long tokens (same pattern as
# test_cli_embed.py / test_cli_search.py). Must precede the partgraph.cli
# import: Rich caches terminal width at Console construction.
os.environ["COLUMNS"] = "200"

import re  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from partgraph.cli import _EMBED_DB_ERROR, app  # noqa: E402, F401

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


_FIXED_NOW = datetime(2030, 6, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake HTTP client (local, self-contained — see module docstring)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHttpClient:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[dict] = []

    def head(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append({"method": "HEAD", "url": url, "kwargs": kwargs})
        return _FakeResponse(self.status_code)

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, "kwargs": kwargs})
        return _FakeResponse(self.status_code)


# ---------------------------------------------------------------------------
# Mock pydgraph client/txn helpers
# ---------------------------------------------------------------------------

def _make_write_txn() -> MagicMock:
    txn = MagicMock()
    txn.mutate.return_value = MagicMock()
    txn.commit.return_value = None
    txn.discard.return_value = None
    txn.__enter__ = MagicMock(return_value=txn)
    txn.__exit__ = MagicMock(return_value=False)
    return txn


def _make_cursor_aware_read_txn(
    pages: list[dict],
    reverse_lookups: dict[str, list[str]] | None = None,
) -> MagicMock:
    """Serve *pages* (Datasheet-selection responses) in call order to any
    query containing BOTH 'type(Datasheet)' and 'first:'. A query containing
    '~datasheet' is answered from *reverse_lookups* (datasheet uid -> list of
    referencing Part uids); anything else gets an empty {"q": []}.

    If more selection queries are issued than *pages* provides, the next call
    raises StopIteration — a fast, bounded failure (never a hang) that flags a
    pagination loop which does not terminate on its own (mirrors
    test_cli_embed.py's _make_cursor_aware_read_txn).
    """
    remaining_pages = iter(pages)
    reverse_lookups = reverse_lookups or {}

    def _query_side_effect(query_text, *args, **kwargs):
        resp = MagicMock()
        if "type(Datasheet)" in query_text and "first:" in query_text:
            resp.json = json.dumps(next(remaining_pages)).encode()
        elif "~datasheet" in query_text:
            matched: list[str] = []
            for ds_uid, part_uids in reverse_lookups.items():
                if ds_uid in query_text:
                    matched = part_uids
                    break
            resp.json = json.dumps(
                {"q": [{"~datasheet": [{"uid": u} for u in matched]}]}
            ).encode()
        else:
            resp.json = json.dumps({"q": []}).encode()
        return resp

    txn = MagicMock()
    txn.query.side_effect = _query_side_effect
    txn.discard.return_value = None
    txn.__enter__ = MagicMock(return_value=txn)
    txn.__exit__ = MagicMock(return_value=False)
    return txn


def _make_dispatch_client(read_txn: MagicMock, write_type_txns: list[MagicMock] | None = None) -> MagicMock:
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
        if c.args and "type(Datasheet)" in c.args[0] and "first:" in c.args[0]
    ]


def _patch_dgraph(mock_client: MagicMock):
    import partgraph.cli as cli_mod
    return patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock()))


def _patch_http_client(fake_client: object):
    import partgraph.cli as cli_mod
    return patch.object(cli_mod, "_build_http_client", return_value=fake_client, create=True)


def _patch_utcnow(fixed: datetime):
    import partgraph.cli as cli_mod
    return patch.object(cli_mod, "_utcnow", lambda: fixed, create=True)


# ===========================================================================
# AC-RL-2: flags / help / validation
# ===========================================================================

def test_ac_rl_2_partgraph_help_lists_refresh_links() -> None:
    """AC-RL-2: Given the partgraph CLI.
    When `partgraph --help` is invoked.
    Then the output lists the 'refresh-links' command.
    """
    result = _invoke(["--help"])
    assert "refresh-links" in result.output, (
        f"AC-RL-2: partgraph --help must list 'refresh-links'. Got:\n{result.output}"
    )


def test_ac_rl_2_refresh_links_help_exits_zero_and_shows_flags_with_defaults() -> None:
    """AC-RL-2: Given the refresh-links command.
    When `partgraph refresh-links --help` is invoked.
    Then exit code is 0 and the output shows --stale-days (default 30),
    --limit, --max-failures (default 3), --timeout (default 10.0).
    """
    result = _invoke(["refresh-links", "--help"])
    assert result.exit_code == 0, (
        f"AC-RL-2: refresh-links --help must exit 0. Got {result.exit_code}.\n"
        f"{result.output}"
    )
    assert "sage" in result.output, f"AC-RL-2: must contain 'Usage'. Got:\n{result.output}"

    assert "--stale-days" in result.output, result.output
    assert "[default: 30]" in result.output, (
        f"AC-RL-2: --stale-days must default to 30. Got:\n{result.output}"
    )
    assert "--limit" in result.output, result.output
    assert "--max-failures" in result.output, result.output
    assert "[default: 3]" in result.output, (
        f"AC-RL-2: --max-failures must default to 3. Got:\n{result.output}"
    )
    assert "--timeout" in result.output, result.output
    assert "[default: 10.0]" in result.output or "[default: 10]" in result.output, (
        f"AC-RL-2: --timeout must default to 10.0. Got:\n{result.output}"
    )


@pytest.mark.parametrize("bad_limit", ["0", "abc"])
def test_ac_rl_2_limit_invalid_exits_1_reuses_validate_limit_message(bad_limit: str) -> None:
    """AC-RL-2: Given --limit 0 or --limit abc.
    When `partgraph refresh-links --limit <bad>` is invoked.
    Then exit code is non-zero and the output contains the EXACT existing
    "--limit must be a positive integer." message (proving _validate_limit
    is reused, not re-implemented).
    """
    result = _invoke(["refresh-links", "--limit", bad_limit])
    assert result.exit_code != 0, (
        f"AC-RL-2: --limit {bad_limit!r} must exit non-zero. Got {result.exit_code}.\n"
        f"{result.output}"
    )
    assert "--limit must be a positive integer." in result.output, (
        f"AC-RL-2: must reuse the exact _validate_limit message. Got:\n{result.output!r}"
    )


@pytest.mark.parametrize("bad_value", ["0", "-1"])
def test_ac_rl_2_max_failures_non_positive_exits_1(bad_value: str) -> None:
    """AC-RL-2: Given --max-failures 0 or a negative value.
    When `partgraph refresh-links --max-failures <bad>` is invoked.
    Then exit code is non-zero and the output names '--max-failures' and
    indicates it must be positive.
    """
    result = _invoke(["refresh-links", "--max-failures", bad_value])
    assert result.exit_code != 0, (
        f"AC-RL-2: --max-failures {bad_value!r} must exit non-zero. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert "--max-failures" in result.output, result.output
    assert "positive" in result.output.lower(), (
        f"AC-RL-2: output must indicate --max-failures must be positive. "
        f"Got:\n{result.output!r}"
    )


@pytest.mark.parametrize("bad_value", ["0", "-1", "-0.5"])
def test_ac_rl_2_timeout_non_positive_exits_1(bad_value: str) -> None:
    """AC-RL-2: Given --timeout 0 or a negative value.
    When `partgraph refresh-links --timeout <bad>` is invoked.
    Then exit code is non-zero and the output names '--timeout' and indicates
    it must be positive.
    """
    result = _invoke(["refresh-links", "--timeout", bad_value])
    assert result.exit_code != 0, (
        f"AC-RL-2: --timeout {bad_value!r} must exit non-zero. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert "--timeout" in result.output, result.output
    assert "positive" in result.output.lower(), (
        f"AC-RL-2: output must indicate --timeout must be positive. "
        f"Got:\n{result.output!r}"
    )


# ===========================================================================
# AC-RL-3: selection query (Datasheet type + staleness filter, NOT embed's)
# ===========================================================================

def test_ac_rl_3_selection_query_targets_datasheet_with_staleness_filter() -> None:
    """AC-RL-3: Given an injected fixed 'now' and stale_days=30.
    When cli._select_datasheets_for_refresh(client, limit, stale_days=30) is
    called.
    Then the query text roots at type(Datasheet), carries
    '@filter(NOT has(verified_at) OR lt(verified_at, ...))' whose cutoff T
    equals now - 30 days (found either inline in the query text or in the
    bound $-variables passed to txn.query), and selects
    uid/url/http_status/fail_count.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)
    expected_t = _fmt(_FIXED_NOW - timedelta(days=30))

    with _patch_utcnow(_FIXED_NOW):
        cli_mod._select_datasheets_for_refresh(client, 100, stale_days=30)

    call = read_txn.query.call_args
    query_text = call.args[0]
    variables = call.kwargs.get("variables") or (call.args[1] if len(call.args) > 1 else {})

    assert "type(Datasheet)" in query_text, f"Got query: {query_text!r}"
    assert "NOT has(verified_at)" in query_text, f"Got query: {query_text!r}"
    assert "lt(verified_at" in query_text, f"Got query: {query_text!r}"
    assert "OR" in query_text, f"Got query: {query_text!r}"
    for field in ("uid", "url", "http_status", "fail_count"):
        assert field in query_text, f"AC-RL-3: field {field!r} missing. Got: {query_text!r}"

    cutoff_present = expected_t in query_text or expected_t in (variables or {}).values()
    assert cutoff_present, (
        f"AC-RL-3: expected staleness cutoff {expected_t!r} (now - 30 days) "
        f"in either the query text or bound variables. Got query: "
        f"{query_text!r}; variables: {variables!r}"
    )


def test_ac_rl_3_selection_query_excludes_part_type_and_embedding_filter() -> None:
    """AC-RL-3: Given the same selection call.
    Then the query text contains NEITHER 'type(Part)' NOR
    'NOT has(embedding)' — proving this is an independent Datasheet-only
    selection, not a reuse/mutation of the embed selection query.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)

    with _patch_utcnow(_FIXED_NOW):
        cli_mod._select_datasheets_for_refresh(client, 100, stale_days=30)

    query_text = read_txn.query.call_args.args[0]
    assert "type(Part)" not in query_text, f"Got query: {query_text!r}"
    assert "NOT has(embedding)" not in query_text, f"Got query: {query_text!r}"


def test_ac_rl_3_selection_does_not_call_select_parts_for_embed() -> None:
    """AC-RL-3: Given cli._select_parts_for_embed is spied on.
    When cli._select_datasheets_for_refresh runs.
    Then _select_parts_for_embed is never called.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    client = _make_dispatch_client(read_txn)

    with _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_select_parts_for_embed") as spy:
        cli_mod._select_datasheets_for_refresh(client, 100, stale_days=30)

    spy.assert_not_called()


# ===========================================================================
# AC-RL-4: uid keyset cursor (mirrors embed's AC-EC-8 pattern)
# ===========================================================================

def _ds_row(uid: str, *, url: str = "https://lcsc.com/x.pdf", fail_count: int = 0) -> dict:
    return {"uid": uid, "url": url, "http_status": 0, "fail_count": fail_count}


def test_ac_rl_4_page_one_omits_after() -> None:
    """AC-RL-4: Given no prior page.
    When `partgraph refresh-links` runs its first selection page.
    Then that page's query has NO 'after:' clause.
    """
    page1 = {"q": [_ds_row("0xF001")]}
    page2_empty = {"q": []}
    read_txn = _make_cursor_aware_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])
    http_client = _FakeHttpClient(status_code=200)

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh-links"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    selection_queries = _selection_query_calls(read_txn)
    assert selection_queries, "expected at least one selection query"
    assert "after:" not in selection_queries[0], (
        f"AC-RL-4: page 1 must have NO 'after:' clause. Got: {selection_queries[0]!r}"
    )


def test_ac_rl_4_page_two_carries_after_numeric_max_uid_mixed_digit_length() -> None:
    """AC-RL-4: Given a page containing mixed hex-digit-length uids
    ("0x9" and "0x10").
    When refresh-links pages past this block.
    Then the next page's query carries 'after: 0x10' (the NUMERICALLY larger
    uid; 0x10 == 16 > 0x9 == 9) and NEVER 'after: 0x9' (a lexicographic string
    max would wrongly pick "0x9").
    """
    import partgraph.cli as cli_mod

    page1 = {"q": [_ds_row("0x9"), _ds_row("0x10")]}
    page2_empty = {"q": []}
    read_txn = _make_cursor_aware_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])
    http_client = _FakeHttpClient(status_code=200)

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_REFRESH_SELECT_PAGE_SIZE", 2, create=True):
        result = _invoke(["refresh-links"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 2, (
        f"AC-RL-4: expected exactly 2 selection queries. Got "
        f"{len(selection_queries)}: {selection_queries!r}"
    )
    assert "after: 0x10" in selection_queries[1], (
        f"AC-RL-4: cursor must be the numerically larger uid (0x10). "
        f"Got: {selection_queries[1]!r}"
    )
    assert "after: 0x9" not in selection_queries[1], (
        f"AC-RL-4: a lexicographic string max would wrongly select '0x9'. "
        f"Got: {selection_queries[1]!r}"
    )


def test_ac_rl_4_terminates_on_empty_page() -> None:
    """AC-RL-4: Given the first selection page is already empty.
    When refresh-links runs.
    Then it exits 0 after exactly one selection query (no further fetch).
    """
    read_txn = _make_cursor_aware_read_txn([{"q": []}])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])
    http_client = _FakeHttpClient(status_code=200)

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh-links"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    assert len(_selection_query_calls(read_txn)) == 1


def test_ac_rl_4_terminates_on_short_page_without_extra_fetch() -> None:
    """AC-RL-4: Given a page shorter than the requested page size.
    When refresh-links runs (page size patched to 2, but only 1 row returned).
    Then it terminates without an extra fetch.
    """
    import partgraph.cli as cli_mod

    short_page = {"q": [_ds_row("0xF010")]}
    read_txn = _make_cursor_aware_read_txn([short_page])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])
    http_client = _FakeHttpClient(status_code=200)

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_REFRESH_SELECT_PAGE_SIZE", 2, create=True):
        result = _invoke(["refresh-links"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    assert len(_selection_query_calls(read_txn)) == 1, (
        "AC-RL-4: a short page must terminate the run without a further fetch."
    )


def test_ac_rl_4_defensive_guard_on_non_advancing_cursor_path_free_notice() -> None:
    """AC-RL-4: Given a mocked server that keeps returning the SAME full page
    (the max uid never advances).
    When refresh-links pages past it.
    Then the defensive guard breaks out after exactly 2 selection queries,
    exits 0, and prints a path-free stall notice (never a plausible-looking
    success line only).
    """
    import partgraph.cli as cli_mod

    same_full_page = {"q": [_ds_row("0xE001"), _ds_row("0xE002")]}
    read_txn = _make_cursor_aware_read_txn(
        [same_full_page, same_full_page, same_full_page]
    )
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])
    http_client = _FakeHttpClient(status_code=200)

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_REFRESH_SELECT_PAGE_SIZE", 2, create=True):
        result = _invoke(["refresh-links"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    assert len(_selection_query_calls(read_txn)) == 2, (
        f"AC-RL-4: the defensive guard must stop after exactly 2 selection "
        f"calls. Got: {_selection_query_calls(read_txn)!r}"
    )
    output_lower = result.output.lower()
    stall_phrases = (
        "did not advance", "not advance", "stopping early", "stopped early",
        "no further progress", "cursor did not move", "cursor stalled",
    )
    assert any(phrase in output_lower for phrase in stall_phrases), (
        f"AC-RL-4: output must contain an explicit stall notice. "
        f"Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RL-4: the stall notice must be path-free."
    )


def test_ac_rl_4_malformed_or_missing_uid_never_interpolated_raw() -> None:
    """AC-RL-4: Given selection rows with an invalid uid (missing entirely,
    and a value shaped like a query-injection payload) alongside one valid
    row.
    When refresh-links pages past this block.
    Then the run completes successfully (a bad row is skipped, never a
    crash), the cursor computation uses ONLY the one valid uid, and neither
    invalid value is EVER interpolated raw into a subsequent query.

    Deliberately asserts exit_code == 0 unconditionally (no "or a clean
    error exit" escape hatch): a run that merely fails without leaking would
    trivially satisfy a weaker check even before 'refresh-links' exists (a
    "no such command" exit is itself a leak-free non-zero exit), which would
    mask the real RED state instead of proving it. Requiring success here
    means this test is honestly RED until the command exists AND correctly
    skips bad rows.
    """
    malformed_uid = '0x1) } mutation { set { _:x <bad> "1" . } } #'
    page1 = {
        "q": [
            {"url": "https://lcsc.com/a.pdf", "http_status": 0, "fail_count": 0},
            {"uid": malformed_uid, "url": "https://lcsc.com/b.pdf",
             "http_status": 0, "fail_count": 0},
            _ds_row("0xB002"),
        ]
    }
    page2_empty = {"q": []}
    read_txn = _make_cursor_aware_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])
    http_client = _FakeHttpClient(status_code=200)

    import partgraph.cli as cli_mod

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW), \
         patch.object(cli_mod, "_REFRESH_SELECT_PAGE_SIZE", 3, create=True):
        result = _invoke(["refresh-links"])

    assert result.exit_code == 0, (
        f"AC-RL-4: a page containing a missing/malformed uid alongside a "
        f"valid row must still complete successfully (the bad rows are "
        f"skipped for cursor purposes, not fatal). Got "
        f"{result.exit_code}.\n{result.output}"
    )

    all_query_texts = [c.args[0] for c in read_txn.query.call_args_list if c.args]
    assert all_query_texts, "expected at least one query to have been issued"
    for query_text in all_query_texts:
        assert malformed_uid not in query_text, (
            f"AC-RL-4: malformed uid must never be interpolated raw. "
            f"Found in: {query_text!r}"
        )
        assert "after: None" not in query_text, (
            f"AC-RL-4: a missing uid must never render as literal 'after: None'. "
            f"Found in: {query_text!r}"
        )

    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 2, (
        f"AC-RL-4: expected page1 (with the bad rows) then page2 (empty). "
        f"Got {len(selection_queries)}: {selection_queries!r}"
    )
    assert "after: 0xB002" in selection_queries[1], (
        f"AC-RL-4: cursor must skip the missing/malformed uids and use "
        f"the one valid uid (0xB002). Got: {selection_queries[1]!r}"
    )
    assert malformed_uid not in result.output, (
        f"AC-RL-4: the malformed value must never leak into CLI output "
        f"either. Got:\n{result.output!r}"
    )


# ===========================================================================
# AC-RL-7 (Gate 3, CLI half): the CLI must construct and thread through a
# REAL HostRateLimiter, not merely accept the leaf's rate_limiter= parameter
# without ever calling it. Unlike every other test in this file, this one
# imports partgraph.refresh.links — but ONLY locally inside the test body
# (not at module level), so only THIS test is affected if the leaf module
# does not exist yet; the rest of this file's collection stays independent
# of it (see the module docstring's isolation rationale).
# ===========================================================================

def test_ac_rl_7_cli_constructs_and_threads_a_real_host_rate_limiter() -> None:
    """AC-RL-7 (Gate 3): Given `partgraph refresh-links` runs against two
    Datasheet rows (so at least one rate-limiting decision point exists).
    When the run executes, with partgraph.refresh.links.HostRateLimiter and
    .refresh_links_write spied on (each still delegates to the real
    implementation via side_effect, so behaviour is unchanged — only calls
    are recorded).
    Then: (a) HostRateLimiter is actually CONSTRUCTED at least once (an impl
    that never builds one — e.g. always passing rate_limiter=None — fails
    this), and (b) the constructed instance is the SAME object passed to
    refresh_links_write's rate_limiter= keyword (by identity) — an impl that
    builds a limiter but forgets to thread it through, or that swallows the
    parameter entirely, fails this.

    This spies at BOTH plausible import sites (partgraph.refresh.links
    itself, and partgraph.cli — in case the CLI imports these names eagerly
    at module level for patchability, mirroring the existing
    get_encoder-in-cli.py precedent) so the test is robust to either an
    eager or a lazy import style in the implementation.
    """
    from contextlib import ExitStack

    from partgraph.refresh import links as links_mod

    import partgraph.cli as cli_mod

    page1 = {"q": [_ds_row("0xG001"), _ds_row("0xG002")]}
    page2_empty = {"q": []}
    read_txn = _make_cursor_aware_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn])
    http_client = _FakeHttpClient(status_code=200)

    constructed_limiters: list[object] = []
    original_limiter_cls = links_mod.HostRateLimiter

    def _spy_ctor(*args, **kwargs):
        instance = original_limiter_cls(*args, **kwargs)
        constructed_limiters.append(instance)
        return instance

    captured_write_kwargs: list[dict] = []
    original_write_fn = links_mod.refresh_links_write

    def _spy_write(*args, **kwargs):
        captured_write_kwargs.append(kwargs)
        return original_write_fn(*args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(_patch_dgraph(client))
        stack.enter_context(_patch_http_client(http_client))
        stack.enter_context(_patch_utcnow(_FIXED_NOW))
        stack.enter_context(
            patch.object(links_mod, "HostRateLimiter", side_effect=_spy_ctor)
        )
        stack.enter_context(
            patch.object(links_mod, "refresh_links_write", side_effect=_spy_write)
        )
        if hasattr(cli_mod, "HostRateLimiter"):
            stack.enter_context(
                patch.object(cli_mod, "HostRateLimiter", side_effect=_spy_ctor)
            )
        if hasattr(cli_mod, "refresh_links_write"):
            stack.enter_context(
                patch.object(cli_mod, "refresh_links_write", side_effect=_spy_write)
            )
        result = _invoke(["refresh-links"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    assert constructed_limiters, (
        "AC-RL-7 (Gate 3): refresh-links must construct a REAL "
        "HostRateLimiter instance (an impl that always passes "
        "rate_limiter=None, or skips rate limiting entirely, fails this)."
    )
    assert captured_write_kwargs, "refresh_links_write must have been called."
    threaded_limiters = [
        kw.get("rate_limiter")
        for kw in captured_write_kwargs
        if kw.get("rate_limiter") is not None
    ]
    assert threaded_limiters, (
        "AC-RL-7 (Gate 3): the constructed HostRateLimiter must be threaded "
        "into refresh_links_write's rate_limiter= keyword — an impl that "
        "builds one but never passes it through fails this."
    )
    assert any(inst in constructed_limiters for inst in threaded_limiters), (
        "AC-RL-7 (Gate 3): the rate_limiter passed to refresh_links_write "
        "must be (one of) the SAME HostRateLimiter instance(s) actually "
        "constructed, by identity — not an unrelated stand-in."
    )


# ===========================================================================
# AC-RL-12: end-to-end multi-Part purge via a mocked reverse-edge response
# ===========================================================================

def test_ac_rl_12_end_to_end_multi_part_purge_notice_is_path_free() -> None:
    """AC-RL-12: Given a Datasheet (uid 0xDS1) shared by 5 Parts, checked dead
    this run (HTTP 500) with a prior fail_count of 10 and --max-failures 11
    (so the new fail_count of 11 crosses the threshold).
    When `partgraph refresh-links --max-failures 11` runs end-to-end (the
    reverse '~datasheet' lookup is mocked to return the 5 referencing Parts).
    Then the run exits 0 and prints a destructive notice mentioning the
    Datasheet uid, its new fail_count (11) and the count of Parts unlinked
    (5) — with NO operator filesystem path and no raw exception text.
    """
    page1 = {"q": [_ds_row("0xDS1", url="https://lcsc.com/shared.pdf", fail_count=10)]}
    page2_empty = {"q": []}
    part_uids = ["0xP1", "0xP2", "0xP3", "0xP4", "0xP5"]
    read_txn = _make_cursor_aware_read_txn(
        [page1, page2_empty], reverse_lookups={"0xDS1": part_uids}
    )
    write_txn = _make_write_txn()
    purge_txn = _make_write_txn()
    client = _make_dispatch_client(read_txn, [write_txn, purge_txn])
    http_client = _FakeHttpClient(status_code=500)

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh-links", "--max-failures", "11"])

    assert result.exit_code == 0, f"Got {result.exit_code}.\n{result.output}"
    assert "0xDS1" in result.output, (
        f"AC-RL-12: destructive notice must mention the Datasheet uid. "
        f"Got:\n{result.output!r}"
    )
    assert "11" in result.output, (
        f"AC-RL-12: destructive notice must mention the new fail_count (11). "
        f"Got:\n{result.output!r}"
    )
    assert "5" in result.output, (
        f"AC-RL-12: destructive notice must mention the #Parts unlinked (5). "
        f"Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RL-12: the destructive notice must be path-free (no operator path)."
    )

    # The purge txn's delete payload shape: an edge-delete triple per Part
    # plus a node-delete triple for the Datasheet itself.
    _, purge_kwargs = purge_txn.mutate.call_args
    del_nquads = purge_kwargs.get("del_nquads", "")
    for part_uid in part_uids:
        assert f"<{part_uid}> <datasheet> <0xDS1> ." in del_nquads, (
            f"AC-RL-12: missing edge-delete triple for {part_uid}. "
            f"del_nquads:\n{del_nquads}"
        )
    assert "<0xDS1> * * ." in del_nquads, (
        f"AC-RL-12: missing Datasheet node-delete triple. del_nquads:\n{del_nquads}"
    )


# ===========================================================================
# AC-RL-14: DB error path (distinct, path-free constant)
# ===========================================================================

def test_ac_rl_14_txn_raises_exit_1_shows_refresh_db_error_no_leak() -> None:
    """AC-RL-14: Given the selection txn.query raises (DB down).
    When `partgraph refresh-links` runs.
    Then exit code is 1, the output contains the NEW path-free
    `_REFRESH_DB_ERROR` constant's text (hinting `partgraph db up`), and no
    raw exception text leaks.
    """
    failing_txn = MagicMock()
    failing_txn.query.side_effect = RuntimeError("connection refused")
    failing_txn.discard.return_value = None
    failing_txn.__enter__ = MagicMock(return_value=failing_txn)
    failing_txn.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.txn.return_value = failing_txn
    http_client = _FakeHttpClient(status_code=200)

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh-links"])

    assert result.exit_code != 0, (
        f"AC-RL-14: a DB failure must exit non-zero. Got {result.exit_code}."
    )
    assert "partgraph db up" in result.output, (
        f"AC-RL-14: output must contain the 'partgraph db up' hint. "
        f"Got:\n{result.output!r}"
    )
    assert "connection refused" not in result.output, (
        f"AC-RL-14: raw exception text must never leak. Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RL-14: the DB error message must be path-free."
    )


def test_ac_rl_14_write_back_mutation_raises_exit_1_path_free() -> None:
    """AC-RL-14 (Gate 3, item 8): Given selection succeeds (one Datasheet
    row is returned and checked normally), but the WRITE-BACK txn's
    mutate() itself raises (DB down mid-run, not merely at the initial
    selection read).
    When `partgraph refresh-links` runs.
    Then exit code is 1, output contains the path-free `_REFRESH_DB_ERROR`
    hint, and no raw exception text leaks — a failure at the write step
    must be handled exactly like a failure at the read/selection step.
    """
    page1 = {"q": [_ds_row("0xWF1")]}
    read_txn = _make_cursor_aware_read_txn([page1])
    write_txn = _make_write_txn()
    write_txn.mutate.side_effect = RuntimeError("connection refused")
    client = _make_dispatch_client(read_txn, [write_txn])
    http_client = _FakeHttpClient(status_code=200)

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh-links"])

    assert result.exit_code != 0, (
        f"AC-RL-14: a write-back mutation failure must exit non-zero. "
        f"Got {result.exit_code}."
    )
    assert "partgraph db up" in result.output, (
        f"AC-RL-14: output must contain the 'partgraph db up' hint even "
        f"for a write-step failure. Got:\n{result.output!r}"
    )
    assert "connection refused" not in result.output, (
        f"AC-RL-14: raw exception text must never leak. Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RL-14: the DB error message must be path-free."
    )


def test_ac_rl_14_purge_mutation_raises_exit_1_path_free() -> None:
    """AC-RL-14 (Gate 3, item 8): Given selection and the write-back both
    succeed, but the SEPARATE purge txn's mutate() raises (e.g. the DB
    connection drops between the write-back commit and the purge step).
    When `partgraph refresh-links --max-failures 1` runs against a Datasheet
    whose new fail_count crosses that threshold.
    Then exit code is 1, output contains the path-free `_REFRESH_DB_ERROR`
    hint, and no raw exception text leaks — a failure specifically at the
    purge step (not just the selection or write-back steps) must be handled
    identically.
    """
    page1 = {"q": [_ds_row("0xWF2", fail_count=0)]}
    read_txn = _make_cursor_aware_read_txn(
        [page1], reverse_lookups={"0xWF2": ["0xPZ"]}
    )
    write_txn = _make_write_txn()
    purge_txn = _make_write_txn()
    purge_txn.mutate.side_effect = RuntimeError("connection refused")
    client = _make_dispatch_client(read_txn, [write_txn, purge_txn])
    http_client = _FakeHttpClient(status_code=500)  # dead -> new fail_count 1

    with _patch_dgraph(client), _patch_http_client(http_client), \
         _patch_utcnow(_FIXED_NOW):
        result = _invoke(["refresh-links", "--max-failures", "1"])

    assert result.exit_code != 0, (
        f"AC-RL-14: a purge mutation failure must exit non-zero. "
        f"Got {result.exit_code}."
    )
    assert "partgraph db up" in result.output, (
        f"AC-RL-14: output must contain the 'partgraph db up' hint even "
        f"for a purge-step failure. Got:\n{result.output!r}"
    )
    assert "connection refused" not in result.output, (
        f"AC-RL-14: raw exception text must never leak. Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-RL-14: the DB error message must be path-free."
    )


def test_ac_rl_14_refresh_db_error_constant_exists_and_differs_from_embed() -> None:
    """AC-RL-14: Given cli._REFRESH_DB_ERROR (new constant).
    Then it exists, is a non-empty string mentioning 'partgraph db up', and
    is textually DISTINCT from the existing cli._EMBED_DB_ERROR.
    """
    import partgraph.cli as cli_mod

    refresh_error = cli_mod._REFRESH_DB_ERROR
    assert isinstance(refresh_error, str) and refresh_error, (
        "AC-RL-14: _REFRESH_DB_ERROR must be a non-empty string constant."
    )
    assert "partgraph db up" in refresh_error, (
        f"AC-RL-14: _REFRESH_DB_ERROR must hint 'partgraph db up'. "
        f"Got: {refresh_error!r}"
    )
    assert refresh_error != _EMBED_DB_ERROR, (
        "AC-RL-14: _REFRESH_DB_ERROR must be textually distinct from "
        "_EMBED_DB_ERROR (a copy-paste-identical constant would defeat the "
        "point of a dedicated error message)."
    )


# ===========================================================================
# AC-RL-15: embed pipeline left untouched (structural)
# ===========================================================================

def test_ac_rl_15_select_datasheets_for_refresh_never_references_embed_helpers() -> None:
    """AC-RL-15: Given cli._select_datasheets_for_refresh's own source code.
    Then it never references _select_parts_for_embed, _embed_all_pages or
    embed_write — proving the new selection path is fully independent of the
    embed pipeline (which the design explicitly forbids modifying).
    """
    import inspect

    import partgraph.cli as cli_mod

    source = inspect.getsource(cli_mod._select_datasheets_for_refresh)
    for forbidden in ("_select_parts_for_embed", "_embed_all_pages", "embed_write"):
        assert forbidden not in source, (
            f"AC-RL-15: _select_datasheets_for_refresh must not reference "
            f"{forbidden!r}. Source:\n{source}"
        )


def test_ac_rl_15_refresh_all_pages_never_references_embed_helpers() -> None:
    """AC-RL-15: Given cli._refresh_all_pages's own source code.
    Then it never references _select_parts_for_embed, _embed_all_pages or
    embed_write.
    """
    import inspect

    import partgraph.cli as cli_mod

    source = inspect.getsource(cli_mod._refresh_all_pages)
    for forbidden in ("_select_parts_for_embed", "_embed_all_pages", "embed_write"):
        assert forbidden not in source, (
            f"AC-RL-15: _refresh_all_pages must not reference {forbidden!r}. "
            f"Source:\n{source}"
        )


def test_ac_rl_15_embed_select_page_size_constant_unchanged() -> None:
    """AC-RL-15: Given the pre-existing embed constant _EMBED_SELECT_PAGE_SIZE.
    Then it retains its original value (10_000) — a refresh-links
    implementation must introduce its OWN constants, never repurpose embed's.

    Gate 3 (architecture cross-PR hazard): this test deliberately does NOT
    also assert on _EMBED_SELECT_DEFAULT. The open embed-hardening PR #12
    DELETES that constant; asserting on it here would make this very test
    (whose entire POINT is "refresh-links doesn't touch embed") crash with
    an unrelated AttributeError the moment #12 merges — the opposite of its
    intent. _EMBED_SELECT_PAGE_SIZE is the constant #12 keeps, so it is the
    stable anchor for this regression guard.
    """
    import partgraph.cli as cli_mod

    assert cli_mod._EMBED_SELECT_PAGE_SIZE == 10_000


def test_ac_rl_15_refresh_select_default_and_page_size_constants() -> None:
    """AC-RL-15 / suggested names: Given the NEW refresh-links constants.
    Then _REFRESH_SELECT_DEFAULT == 200_000 and _REFRESH_SELECT_PAGE_SIZE ==
    10_000 (own constants, not aliases of the embed ones — see the previous
    test for the embed side of that guarantee).
    """
    import partgraph.cli as cli_mod

    assert cli_mod._REFRESH_SELECT_DEFAULT == 200_000
    assert cli_mod._REFRESH_SELECT_PAGE_SIZE == 10_000
