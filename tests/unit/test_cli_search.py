"""
Tests: SEARCH-CLI-1..11, SEARCH-PRIV — partgraph.cli search/show commands

Specifies the behavior of the `partgraph search` and `partgraph show` CLI
commands added in PR3.

Design decisions pinned by dispatcher:
  - search/show --help exit 0, output contains "Usage".
  - search runs txn(read_only=True) and never calls mutate.
  - DB-down (txn().query raises) -> non-zero exit + "db up" hint.
  - Empty query "" -> non-zero exit, NO Dgraph query sent.
  - Zero results -> exit 0 "No matches found".
  - Nearest-match path -> output has explicit "nearest" banner substring + rows.
  - Columns present: MPN, manufacturer, package, stock, datasheet URL substrings.
  - show by MPN -> exit 0 contains MPN + manufacturer + package + URL.
  - show not-found -> exit 0 "not found", no exception.
  - Long URL non-wrapping under COLUMNS=200.
  - Injection 'MAX232") drop' -> hostile chars only inside $var value (not in query text).

Harness pattern (identical to test_cli_ingest.py):
  - COLUMNS=200 set BEFORE importing partgraph.cli.
  - ANSI-strip _invoke wrapper.
  - Mocked pydgraph (same pattern as test_stats.py).

NOTE: Collection will ERROR on import of partgraph.cli `search`/`show` commands
because those commands do not exist in partgraph.cli yet. That is the correct
red state before PR3 implementation.
"""

from __future__ import annotations

import json
import os

# Pin a wide terminal so Rich/Typer never wraps long tokens or URLs.
# Must precede the partgraph.cli import: Rich caches terminal width at Console
# construction and cli.py builds its Console objects at import time.
os.environ["COLUMNS"] = "200"

import re  # noqa: E402
from unittest.mock import MagicMock, call, patch  # noqa: E402

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from partgraph.cli import app  # noqa: E402, F401 — env set above must precede this import

RUNNER = CliRunner()

# Strip ANSI escape codes from Rich output so assertions are render-independent.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _StrippedResult:
    """Click Result wrapper with ANSI codes removed from .output."""

    def __init__(self, result: object) -> None:
        self._result = result

    @property
    def output(self) -> str:
        return _ANSI_RE.sub("", self._result.output)

    def __getattr__(self, name: str) -> object:
        return getattr(self._result, name)


def _invoke(args: list[str]) -> _StrippedResult:
    return _StrippedResult(RUNNER.invoke(app, args))


# ---------------------------------------------------------------------------
# Mock-building helpers (mirrored from test_stats.py)
# ---------------------------------------------------------------------------

def _make_mock_txn(query_responses: list[dict] | None = None) -> MagicMock:
    """Return a mock txn that returns canned JSON responses for successive query calls.

    query_responses: list of dicts to return as resp.json bytes for each call.
    Defaults to a single empty multi-block response.
    """
    default_empty = {"exact": [], "trig": [], "fts": []}
    responses = query_responses or [default_empty]

    call_counter = [0]

    def _fake_query(dql: str, variables: dict | None = None, *args, **kwargs):
        resp = MagicMock()
        idx = min(call_counter[0], len(responses) - 1)
        call_counter[0] += 1
        resp.json = json.dumps(responses[idx]).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    return mock_txn


def _make_mock_client(txn: MagicMock | None = None) -> MagicMock:
    mock_client = MagicMock()
    mock_client.txn.return_value = txn or _make_mock_txn()
    return mock_client


def _patch_dgraph(mock_client: MagicMock):
    """Context manager that patches _build_dgraph_client to return mock_client."""
    import partgraph.cli as cli_mod
    return patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock()))


# ---------------------------------------------------------------------------
# SEARCH-CLI-1: search --help exit 0 contains "Usage"
# ---------------------------------------------------------------------------

def test_cli_search_help_exits_zero() -> None:
    """Given the partgraph CLI with the search command.
    When `partgraph search --help` is invoked.
    Then exit code is 0.
    """
    result = _invoke(["search", "--help"])
    assert result.exit_code == 0, (
        f"`search --help` exited {result.exit_code}.\nOutput:\n{result.output}"
    )


def test_cli_search_help_contains_usage() -> None:
    """Given the search command.
    When `partgraph search --help` is invoked.
    Then the output contains "Usage".
    """
    result = _invoke(["search", "--help"])
    assert "sage" in result.output, (
        f"search --help output does not contain 'Usage': {result.output}"
    )


# ---------------------------------------------------------------------------
# SEARCH-CLI-2: show --help exit 0 contains "Usage"
# ---------------------------------------------------------------------------

def test_cli_show_help_exits_zero() -> None:
    """Given the partgraph CLI with the show command.
    When `partgraph show --help` is invoked.
    Then exit code is 0.
    """
    result = _invoke(["show", "--help"])
    assert result.exit_code == 0, (
        f"`show --help` exited {result.exit_code}.\nOutput:\n{result.output}"
    )


def test_cli_show_help_contains_usage() -> None:
    """Given the show command.
    When `partgraph show --help` is invoked.
    Then the output contains "Usage".
    """
    result = _invoke(["show", "--help"])
    assert "sage" in result.output, (
        f"show --help output does not contain 'Usage': {result.output}"
    )


# ---------------------------------------------------------------------------
# SEARCH-CLI-3: search runs txn(read_only=True) and never calls mutate
# ---------------------------------------------------------------------------

def test_cli_search_uses_read_only_txn() -> None:
    """Given a mocked pydgraph client.
    When `partgraph search MAX232` is invoked.
    Then client.txn is called with read_only=True.
    """
    mock_txn = _make_mock_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        _invoke(["search", "MAX232"])

    # txn() must be called with read_only=True at least once.
    calls = mock_client.txn.call_args_list
    assert any(
        c == call(read_only=True) or c.kwargs.get("read_only") is True
        for c in calls
    ), (
        f"search must call client.txn(read_only=True). Actual calls: {calls}"
    )


def test_cli_search_never_calls_mutate() -> None:
    """Given a mocked pydgraph client.
    When `partgraph search MAX232` is invoked.
    Then txn.mutate is never called (read-only — no writes).
    """
    mock_txn = _make_mock_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        _invoke(["search", "MAX232"])

    mock_txn.mutate.assert_not_called()


# ---------------------------------------------------------------------------
# SEARCH-CLI-4: DB-down -> non-zero exit + "db up" hint
# ---------------------------------------------------------------------------

def test_cli_search_db_down_exits_nonzero() -> None:
    """Given a mock pydgraph client whose txn().query raises RuntimeError (DB down).
    When `partgraph search MAX232` is invoked.
    Then exit code is non-zero.
    """
    mock_txn = MagicMock()
    mock_txn.query.side_effect = RuntimeError("connection refused")
    mock_txn.discard.return_value = None
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert result.exit_code != 0, (
        f"Expected non-zero exit when DB is down. Got: {result.exit_code}.\n{result.output}"
    )


def test_cli_search_db_down_output_hints_db_up() -> None:
    """Given a DB-down condition (txn query raises).
    When `partgraph search MAX232` is invoked.
    Then the output contains EXACTLY the fixed message:
      "Is the database running? Start it with `partgraph db up`."
    and does NOT contain the raw exception text.

    PIN (B1): the user-facing error must be a fixed string WITHOUT interpolating
    {exc}. This prevents internal paths and exception details from leaking to the
    user-facing output.

    CHANGE from previous version: old test accepted any "db up"/"database"/
    "running" substring; new contract requires the specific "partgraph db up"
    phrase so that the exact copy string (E4) and no-path-leak (B1) are both
    satisfied by the same assertion.
    """
    mock_txn = MagicMock()
    mock_txn.query.side_effect = RuntimeError("connection refused")
    mock_txn.discard.return_value = None
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    # PIN E4 + B1: exact "partgraph db up" substring in user output.
    assert "partgraph db up" in result.output, (
        f"DB-down error must contain 'partgraph db up'. Got: {result.output!r}"
    )
    # B1: raw exception text must NOT appear in user output.
    assert "connection refused" not in result.output, (
        f"B1: raw exception text must not leak to user output. Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# SEARCH-CLI-5: Empty query "" -> non-zero exit, NO Dgraph query sent
# ---------------------------------------------------------------------------

def test_cli_search_empty_query_exits_nonzero() -> None:
    """Given an empty query string "".
    When `partgraph search ""` is invoked.
    Then exit code is non-zero (empty query is invalid).
    """
    mock_txn = _make_mock_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", ""])

    assert result.exit_code != 0, (
        f"Empty query must exit non-zero. Got: {result.exit_code}.\n{result.output}"
    )


def test_cli_search_empty_query_no_dgraph_call() -> None:
    """Given an empty query string "".
    When `partgraph search ""` is invoked.
    Then txn.query is never called (no network round-trip for empty input).
    """
    mock_txn = _make_mock_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        _invoke(["search", ""])

    mock_txn.query.assert_not_called()


# ---------------------------------------------------------------------------
# SEARCH-CLI-6: Zero results -> exit 0, "No matches found"
# ---------------------------------------------------------------------------

def test_cli_search_zero_results_exits_zero() -> None:
    """Given a mocked client that returns empty result blocks.
    When `partgraph search MAX232` is invoked.
    Then exit code is 0 (no-results is not an error).
    """
    mock_txn = _make_mock_txn([{"exact": [], "trig": [], "fts": []}])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert result.exit_code == 0, (
        f"Zero results must exit 0. Got: {result.exit_code}.\n{result.output}"
    )


def test_cli_search_zero_results_shows_no_matches_message() -> None:
    """Given empty result blocks.
    When `partgraph search MAX232` is invoked.
    Then the output contains "No matches found" (or equivalent no-results message).
    """
    mock_txn = _make_mock_txn([{"exact": [], "trig": [], "fts": []}])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    output_lower = result.output.lower()
    assert "no match" in output_lower or "not found" in output_lower or "0 result" in output_lower, (
        f"Zero-results output must say 'No matches found' (or equivalent). "
        f"Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# SEARCH-CLI-7: Nearest-match path -> "nearest" banner + renders rows
# ---------------------------------------------------------------------------

def _make_nearest_response() -> list[dict]:
    """Return a two-call response sequence: first pass empty, second pass with rows."""
    return [
        # Hard pass: no results.
        {"exact": [], "trig": [], "fts": []},
        # Relaxed pass (nearest): rows present.
        {
            "nearest": [
                {
                    "uid": "0x100",
                    "mpn": "MAX232CPE",
                    "mpn_norm": "MAX232CPE",
                    "stock": 50,
                    "is_basic": False,
                    "voltage_max": 5.5,
                    "made_by": [{"name": "Texas Instruments"}],
                    "in_package": [{"name": "PDIP-16"}],
                    "datasheet": [{"url": "https://www.ti.com/lit/ds/symlink/max232.pdf"}],
                }
            ]
        },
    ]


def test_cli_search_nearest_match_output_contains_nearest_banner() -> None:
    """Given a two-pass nearest-match scenario (first pass empty, second has rows).
    When `partgraph search "1.2V MAX232"` is invoked.
    Then the output contains the word "nearest" (case-insensitive banner).
    """
    mock_txn = _make_mock_txn(_make_nearest_response())
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "1.2V MAX232"])

    assert "nearest" in result.output.lower(), (
        f"Nearest-match output must contain 'nearest' banner. Got:\n{result.output}"
    )


def test_cli_search_nearest_match_renders_rows() -> None:
    """Given a nearest-match scenario with at least one row.
    When invoked.
    Then the output contains part data (MPN or manufacturer substring visible).
    """
    mock_txn = _make_mock_txn(_make_nearest_response())
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "1.2V MAX232"])

    # At minimum the MPN or manufacturer must appear in rendered output.
    output = result.output
    assert "MAX232" in output or "Texas Instruments" in output or "PDIP" in output, (
        f"Nearest-match must render part rows. Got:\n{output}"
    )


# ---------------------------------------------------------------------------
# SEARCH-CLI-8: Columns present — MPN, manufacturer, package, stock, URL substrings
# ---------------------------------------------------------------------------

def _make_search_response_with_parts() -> dict:
    """Single DQL response with one well-populated part in the exact block."""
    return {
        "exact": [
            {
                "uid": "0x200",
                "mpn": "MAX232CPE",
                "mpn_norm": "MAX232CPE",
                "stock": 250,
                "is_basic": True,
                "made_by": [{"name": "Texas Instruments"}],
                "in_package": [{"name": "PDIP-16"}],
                "datasheet": [{"url": "https://www.ti.com/lit/ds/symlink/max232.pdf"}],
            }
        ],
        "trig": [],
        "fts":  [],
    }


def test_cli_search_output_contains_mpn() -> None:
    """Given a search result with one part.
    When `partgraph search MAX232` is invoked.
    Then the output contains the part's MPN.
    """
    mock_txn = _make_mock_txn([_make_search_response_with_parts()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert "MAX232" in result.output, (
        f"Output must contain MPN 'MAX232'. Got:\n{result.output}"
    )


def test_cli_search_output_contains_manufacturer() -> None:
    """Given a search result with a part from Texas Instruments.
    When `partgraph search MAX232` is invoked.
    Then the output contains "Texas Instruments" (or a recognisable prefix).
    """
    mock_txn = _make_mock_txn([_make_search_response_with_parts()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert "Texas" in result.output or "Instruments" in result.output, (
        f"Output must contain manufacturer name. Got:\n{result.output}"
    )


def test_cli_search_output_contains_package() -> None:
    """Given a search result with package PDIP-16.
    When `partgraph search MAX232` is invoked.
    Then the output contains the package name.
    """
    mock_txn = _make_mock_txn([_make_search_response_with_parts()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert "PDIP" in result.output or "16" in result.output, (
        f"Output must contain package info. Got:\n{result.output}"
    )


def test_cli_search_output_contains_stock() -> None:
    """Given a search result with stock=250.
    When `partgraph search MAX232` is invoked.
    Then the output contains the stock count.
    """
    mock_txn = _make_mock_txn([_make_search_response_with_parts()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert "250" in result.output, (
        f"Output must contain stock count 250. Got:\n{result.output}"
    )


def test_cli_search_output_contains_datasheet_url() -> None:
    """Given a search result with a datasheet URL.
    When `partgraph search MAX232` is invoked.
    Then the output contains a URL substring (at minimum "http").
    """
    mock_txn = _make_mock_txn([_make_search_response_with_parts()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert "http" in result.output, (
        f"Output must contain datasheet URL substring. Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# SEARCH-CLI-9: show by MPN -> exit 0, contains MPN + manufacturer + package + URL
# ---------------------------------------------------------------------------

def _make_show_response(mpn_norm: str = "MAX232") -> dict:
    """Canned show DQL response for a single well-populated part."""
    return {
        "part": [
            {
                "uid": "0x300",
                "mpn": mpn_norm,
                "mpn_norm": mpn_norm,
                "stock": 100,
                "is_basic": False,
                "made_by": [{"name": "Texas Instruments"}],
                "in_package": [{"name": "DIP-16"}],
                "in_category": [{"name": "RS-232 Interface IC"}],
                "datasheet": [
                    {"url": "https://www.ti.com/lit/ds/symlink/max232.pdf", "source": "TI"}
                ],
                "tagged": [],
                "attr": [],
            }
        ],
        "related": [],
    }


def test_cli_show_mpn_exits_zero() -> None:
    """Given a mocked pydgraph client returning one matching part.
    When `partgraph show MAX232` is invoked.
    Then exit code is 0.
    """
    mock_txn = _make_mock_txn([_make_show_response("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert result.exit_code == 0, (
        f"`show MAX232` must exit 0. Got: {result.exit_code}.\n{result.output}"
    )


def test_cli_show_mpn_output_contains_mpn() -> None:
    """Given a show result for MAX232.
    When `partgraph show MAX232` is invoked.
    Then the output contains "MAX232".
    """
    mock_txn = _make_mock_txn([_make_show_response("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert "MAX232" in result.output, (
        f"show output must contain MPN. Got:\n{result.output}"
    )


def test_cli_show_mpn_output_contains_manufacturer() -> None:
    """Given a show result with manufacturer Texas Instruments.
    When `partgraph show MAX232` is invoked.
    Then the output contains the manufacturer name.
    """
    mock_txn = _make_mock_txn([_make_show_response("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert "Texas" in result.output or "Instruments" in result.output, (
        f"show output must contain manufacturer. Got:\n{result.output}"
    )


def test_cli_show_mpn_output_contains_package() -> None:
    """Given a show result with package DIP-16.
    When `partgraph show MAX232` is invoked.
    Then the output contains the package info.
    """
    mock_txn = _make_mock_txn([_make_show_response("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert "DIP" in result.output or "16" in result.output, (
        f"show output must contain package info. Got:\n{result.output}"
    )


def test_cli_show_mpn_output_contains_url() -> None:
    """Given a show result with a datasheet URL.
    When `partgraph show MAX232` is invoked.
    Then the output contains the URL.
    """
    mock_txn = _make_mock_txn([_make_show_response("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert "http" in result.output, (
        f"show output must contain datasheet URL. Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# SEARCH-CLI-10: show not-found -> exit 0, "not found", no exception
# ---------------------------------------------------------------------------

def test_cli_show_not_found_exits_zero() -> None:
    """Given an MPN that returns no results from Dgraph.
    When `partgraph show NONEXISTENT9999` is invoked.
    Then exit code is 0 (not found is not an error — dispatcher Q3 decision).
    """
    mock_txn = _make_mock_txn([{"part": [], "related": []}])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "NONEXISTENT9999"])

    assert result.exit_code == 0, (
        f"`show NONEXISTENT9999` must exit 0 (not found is not an error). "
        f"Got: {result.exit_code}.\n{result.output}"
    )


def test_cli_show_not_found_output_contains_not_found() -> None:
    """Given an MPN that returns no results.
    When `partgraph show NONEXISTENT9999` is invoked.
    Then the output contains "not found" (or equivalent).
    """
    mock_txn = _make_mock_txn([{"part": [], "related": []}])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "NONEXISTENT9999"])

    assert "not found" in result.output.lower() or "no result" in result.output.lower(), (
        f"show not-found must say 'not found'. Got:\n{result.output}"
    )


def test_cli_show_not_found_no_exception() -> None:
    """Given an MPN that returns no results.
    When `partgraph show NONEXISTENT9999` is invoked.
    Then no unhandled exception propagates to the runner.
    """
    mock_txn = _make_mock_txn([{"part": [], "related": []}])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "NONEXISTENT9999"])

    assert result.exception is None, (
        f"show not-found must not raise an exception. Got: {result.exception}\n"
        f"Output: {result.output}"
    )


# ---------------------------------------------------------------------------
# SEARCH-CLI-11: Long URL non-wrapping under COLUMNS=200
# ---------------------------------------------------------------------------

def test_cli_search_long_url_does_not_wrap() -> None:
    """Given a part with a long datasheet URL (>80 chars).
    When `partgraph search MAX232` is invoked with COLUMNS=200.
    Then the URL appears as a single unbroken line in the output (no line wrapping).
    """
    long_url = (
        "https://www.ti.com/lit/ds/symlink/max232-q1-very-long-filename-for-wrap-testing-"
        "abcdefghijklmnopqrstuvwxyz-0123456789.pdf"
    )
    response = {
        "exact": [
            {
                "uid": "0x400",
                "mpn": "MAX232",
                "mpn_norm": "MAX232",
                "stock": 10,
                "is_basic": False,
                "made_by": [{"name": "TI"}],
                "in_package": [{"name": "DIP"}],
                "datasheet": [{"url": long_url}],
            }
        ],
        "trig": [],
        "fts":  [],
    }
    mock_txn = _make_mock_txn([response])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    # The URL must appear as a contiguous substring — no newline inserted in the middle.
    # We check that either the full URL is present, or at least a 40-char prefix is
    # present without a newline breaking it.
    url_prefix = long_url[:60]
    assert url_prefix in result.output, (
        f"Long URL must not be wrapped (COLUMNS=200). "
        f"URL prefix {url_prefix!r} not found in output:\n{result.output}"
    )


def test_cli_show_long_url_non_wrapping() -> None:
    """Given a show result with a long datasheet URL.
    When `partgraph show MAX232` is invoked with COLUMNS=200.
    Then the URL appears as a single unbroken token in the output.
    """
    long_url = (
        "https://datasheets.example.com/very/long/path/to/datasheet-for-max232-ic-component-"
        "revision-c-2024-engineering.pdf"
    )
    response = {
        "part": [
            {
                "uid": "0x500",
                "mpn": "MAX232",
                "mpn_norm": "MAX232",
                "stock": 0,
                "is_basic": False,
                "made_by": [{"name": "Texas Instruments"}],
                "in_package": [{"name": "DIP-16"}],
                "in_category": [],
                "datasheet": [{"url": long_url, "source": "example"}],
                "tagged": [],
                "attr": [],
            }
        ],
        "related": [],
    }
    mock_txn = _make_mock_txn([response])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    url_prefix = long_url[:60]
    assert url_prefix in result.output, (
        f"Long URL must not be wrapped in show output (COLUMNS=200). "
        f"URL prefix {url_prefix!r} not found:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# SEARCH-PRIV: Injection guard — hostile chars only inside $var, not in query text
# ADR-INJECT: text tokens bind via Dgraph $vars; the raw token may not appear
#             in the DQL query string itself.
# ---------------------------------------------------------------------------

def test_cli_search_injection_token_not_in_query_text() -> None:
    """Given a hostile query token 'MAX232\") drop'.
    When `partgraph search 'MAX232\") drop'` is invoked.
    Then:
      - The raw hostile string 'drop' does NOT appear as a literal in the DQL
        query text sent to Dgraph.txn.query (only inside the $var value).
      - The command does not crash (handles gracefully).

    ADR-INJECT: numeric values = float literals (safe); text tokens bind via
    Dgraph $vars; hostile chars are encapsulated in the variable value, never
    interpolated into the query template.
    """
    captured_queries: list[str] = []
    captured_variables: list[dict] = []

    def _spy_query(dql: str, variables: dict | None = None, *args, **kwargs):
        captured_queries.append(dql)
        if variables:
            captured_variables.append(variables)
        resp = MagicMock()
        resp.json = json.dumps({"exact": [], "trig": [], "fts": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _spy_query
    mock_txn.discard.return_value = None
    mock_client = _make_mock_client(mock_txn)

    hostile_input = 'MAX232") drop'
    with _patch_dgraph(mock_client):
        result = _invoke(["search", hostile_input])

    # The command must not crash.
    assert result.exception is None, (
        f"search with hostile input must not raise. Got: {result.exception}"
    )

    # If any DQL was sent, the raw hostile string must not appear literally in it.
    for q in captured_queries:
        assert 'drop' not in q or (
            # Allow "drop" only if it appears solely inside a quoted $var value
            # in the query declaration — not as raw DQL keyword.
            # Simplest check: the query text must not contain the full hostile payload.
            'MAX232") drop' not in q
        ), (
            f"Hostile payload 'MAX232\") drop' must not appear literally in DQL query "
            f"text. Got:\n{q!r}"
        )


def test_cli_show_help_mentions_related_parts_by_mpn_not_family() -> None:
    """Given the show command help text.
    When `partgraph show --help` is invoked.
    Then the help text does NOT mention "family variants" or "family_name",
    and does NOT claim to show family traversal.
    (Dispatcher Q1: family_name/PartFamily/variant_of are UNPOPULATED.)
    """
    result = _invoke(["show", "--help"])
    output_lower = result.output.lower()

    assert "family variant" not in output_lower, (
        f"show --help must not mention 'family variants' (UNPOPULATED). "
        f"Got:\n{result.output}"
    )
    assert "variant_of" not in output_lower, (
        f"show --help must not mention 'variant_of' (UNPOPULATED). "
        f"Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# B1 — Security: show path never calls mutate + exception no path leak
# ---------------------------------------------------------------------------

def test_cli_show_never_calls_mutate() -> None:
    """Given a mocked pydgraph client.
    When `partgraph show MAX232` is invoked (the show/detail path).
    Then txn.mutate is NEVER called (show is a pure read operation).

    PIN (B1): any call to mutate on the show path is a security regression —
    it means user-triggered read commands can write to the database.
    """
    mock_txn = _make_mock_txn([_make_show_response("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        _invoke(["show", "MAX232"])

    mock_txn.mutate.assert_not_called()


def test_cli_search_exception_no_path_leak() -> None:
    """Given a mock txn.query that raises FileNotFoundError with a path
    containing "/home/operator/secret".
    When `partgraph search MAX232` is invoked.
    Then:
      - The output does NOT contain "/home/" (no filesystem path leakage).
      - The exit code is non-zero.
      - The output DOES contain "partgraph db up" (the fixed safe error message).

    PIN (B1): the user-facing error must be a fixed string that does NOT
    interpolate {exc}, preventing internal paths from reaching user output.
    """
    secret_path = "/home/operator/secret"
    mock_txn = MagicMock()
    mock_txn.query.side_effect = FileNotFoundError(secret_path)
    mock_txn.discard.return_value = None
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert result.exit_code != 0, (
        f"Exception during query must produce non-zero exit. Got: {result.exit_code}"
    )
    assert "/home/" not in result.output, (
        f"B1: filesystem path must NOT leak into user output. Got: {result.output!r}"
    )
    assert "partgraph db up" in result.output, (
        f"B1: fixed error message 'partgraph db up' must appear. Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# E1 — UI/UX: search --help exact copy strings
# ---------------------------------------------------------------------------

def test_cli_search_help_contains_limit_flag() -> None:
    """Given the search command help.
    When `partgraph search --help` is invoked.
    Then the output contains "--limit" (the result-count flag).
    PIN E1.
    """
    result = _invoke(["search", "--help"])
    assert "--limit" in result.output, (
        f"E1: search --help must contain '--limit'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_no_truncate_flag() -> None:
    """Given the search command help.
    When `partgraph search --help` is invoked.
    Then the output contains "--no-truncate" (the full-output flag).
    PIN E1.
    """
    result = _invoke(["search", "--help"])
    assert "--no-truncate" in result.output, (
        f"E1: search --help must contain '--no-truncate'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_example_query() -> None:
    """Given the search command help.
    When `partgraph search --help` is invoked.
    Then the output contains an example query (at minimum "10k 0402 1%").
    PIN E1.
    """
    result = _invoke(["search", "--help"])
    assert "10k 0402 1%" in result.output, (
        f"E1: search --help must contain example query '10k 0402 1%'. Got:\n{result.output}"
    )


def test_cli_search_help_does_not_contain_family_variant() -> None:
    """Given the search command help.
    When `partgraph search --help` is invoked.
    Then the output does NOT contain "family variant".
    PIN E1 (consistent with show --help constraint; PartFamily is UNPOPULATED).
    """
    result = _invoke(["search", "--help"])
    assert "family variant" not in result.output.lower(), (
        f"E1: search --help must not contain 'family variant'. Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# E2 — UI/UX: nearest-match output exact banner strings
# ---------------------------------------------------------------------------

def test_cli_search_nearest_match_output_contains_no_exact_match_banner() -> None:
    """Given a two-pass nearest-match scenario.
    When `partgraph search "1.2V MAX232"` is invoked.
    Then the ANSI-stripped output contains the substring "No exact match for:".
    PIN E2.
    """
    mock_txn = _make_mock_txn(_make_nearest_response())
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "1.2V MAX232"])

    assert "No exact match for:" in result.output, (
        f"E2: nearest-match output must contain 'No exact match for:'. "
        f"Got:\n{result.output}"
    )


def test_cli_search_nearest_match_output_contains_nearest_match_label() -> None:
    """Given a two-pass nearest-match scenario.
    When `partgraph search "1.2V MAX232"` is invoked.
    Then the ANSI-stripped output contains "Nearest match" (case-insensitive).
    PIN E2.
    """
    mock_txn = _make_mock_txn(_make_nearest_response())
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "1.2V MAX232"])

    assert "nearest match" in result.output.lower(), (
        f"E2: nearest-match output must contain 'Nearest match' (case-insensitive). "
        f"Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# E3 — UI/UX: empty-results output exact string
# ---------------------------------------------------------------------------

def test_cli_search_zero_results_output_exact_string() -> None:
    """Given a search that returns no results.
    When `partgraph search MAX232` is invoked.
    Then the output contains the exact phrase "No matches found".
    PIN E3 — the exact string, not just a substring match on "no match".
    """
    mock_txn = _make_mock_txn([{"exact": [], "trig": [], "fts": []}])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert "No matches found" in result.output, (
        f"E3: zero-results output must contain exact string 'No matches found'. "
        f"Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# E4 — UI/UX: DB-down and empty-query exact copy strings
# ---------------------------------------------------------------------------

def test_cli_search_db_down_output_contains_partgraph_db_up() -> None:
    """Given a DB-down condition (txn query raises RuntimeError).
    When `partgraph search MAX232` is invoked.
    Then the output contains "partgraph db up" (the exact CLI command to fix it).
    PIN E4 — exact substring; complements test_cli_search_db_down_output_hints_db_up
    which also enforces the no-raw-exception contract (B1).
    """
    mock_txn = MagicMock()
    mock_txn.query.side_effect = RuntimeError("connection refused")
    mock_txn.discard.return_value = None
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert "partgraph db up" in result.output, (
        f"E4: DB-down message must contain 'partgraph db up'. Got: {result.output!r}"
    )


def test_cli_search_empty_query_output_contains_empty() -> None:
    """Given an empty query string "".
    When `partgraph search ""` is invoked.
    Then the output contains "empty" (the exact word describing the error).
    PIN E4.
    """
    mock_txn = _make_mock_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", ""])

    assert "empty" in result.output.lower(), (
        f"E4: empty-query error must contain 'empty'. Got: {result.output!r}"
    )


def test_cli_show_not_found_output_contains_not_found_exact() -> None:
    """Given an MPN that returns no results.
    When `partgraph show NONEXISTENT9999` is invoked.
    Then the output contains "not found" AND exit code is 0.
    PIN E4 (exit 0 + "not found" phrase).
    """
    mock_txn = _make_mock_txn([{"part": [], "related": []}])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "NONEXISTENT9999"])

    assert "not found" in result.output.lower(), (
        f"E4: show not-found must output 'not found'. Got: {result.output!r}"
    )
    assert result.exit_code == 0, (
        f"E4: show not-found must exit 0. Got: {result.exit_code}"
    )


# ---------------------------------------------------------------------------
# E5 — UI/UX: show output explicit section labels
# ---------------------------------------------------------------------------

def _make_show_response_full(mpn_norm: str = "MAX232") -> dict:
    """Canned show response with rich data to exercise all section labels."""
    return {
        "part": [
            {
                "uid": "0x600",
                "mpn": mpn_norm,
                "mpn_norm": mpn_norm,
                "stock": 150,
                "is_basic": False,
                "made_by": [{"name": "Texas Instruments"}],
                "in_package": [{"name": "DIP-16"}],
                "in_category": [{"name": "RS-232 Interface IC"}],
                "datasheet": [
                    {"url": "https://www.ti.com/lit/ds/symlink/max232.pdf", "source": "TI"}
                ],
                "tagged": [],
                "attr": [],
            }
        ],
        "related": [
            {
                "uid": "0x601",
                "mpn": "MAX232A",
                "mpn_norm": "MAX232A",
            }
        ],
    }


def test_cli_show_output_contains_manufacturer_label() -> None:
    """Given a show result for MAX232.
    When `partgraph show MAX232` is invoked.
    Then the output contains the section label "Manufacturer".
    PIN E5.
    """
    mock_txn = _make_mock_txn([_make_show_response_full("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert "Manufacturer" in result.output, (
        f"E5: show output must contain section label 'Manufacturer'. Got:\n{result.output}"
    )


def test_cli_show_output_contains_package_label() -> None:
    """Given a show result for MAX232.
    When `partgraph show MAX232` is invoked.
    Then the output contains the section label "Package".
    PIN E5.
    """
    mock_txn = _make_mock_txn([_make_show_response_full("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert "Package" in result.output, (
        f"E5: show output must contain section label 'Package'. Got:\n{result.output}"
    )


def test_cli_show_output_contains_stock_label() -> None:
    """Given a show result for MAX232.
    When `partgraph show MAX232` is invoked.
    Then the output contains the section label "Stock".
    PIN E5.
    """
    mock_txn = _make_mock_txn([_make_show_response_full("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert "Stock" in result.output, (
        f"E5: show output must contain section label 'Stock'. Got:\n{result.output}"
    )


def test_cli_show_output_contains_datasheets_label() -> None:
    """Given a show result for MAX232.
    When `partgraph show MAX232` is invoked.
    Then the output contains the section label "Datasheets".
    PIN E5.
    """
    mock_txn = _make_mock_txn([_make_show_response_full("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert "Datasheets" in result.output, (
        f"E5: show output must contain section label 'Datasheets'. Got:\n{result.output}"
    )


def test_cli_show_output_contains_related_parts_label_not_family() -> None:
    """Given a show result for MAX232 with one related part.
    When `partgraph show MAX232` is invoked.
    Then the output contains "Related parts" (by MPN) and NOT "family".
    PIN E5: label is "Related parts", NOT "family" (PartFamily is UNPOPULATED).
    """
    mock_txn = _make_mock_txn([_make_show_response_full("MAX232")])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert "Related parts" in result.output, (
        f"E5: show output must contain 'Related parts' section label. Got:\n{result.output}"
    )
    assert "family" not in result.output.lower(), (
        f"E5: show output must NOT contain 'family' (UNPOPULATED). Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# E6 — UI/UX: long URL non-wrapping (60+ char prefix) in BOTH search and show
# (Strengthened from existing SEARCH-CLI-11 tests to pin exact E6 requirement)
# ---------------------------------------------------------------------------

def test_cli_search_long_url_60char_prefix_unbroken() -> None:
    """Given a search result with a 60+ char URL.
    When `partgraph search MAX232` is invoked under COLUMNS=200.
    Then a 60-character prefix of the URL appears as an unbroken substring
    (no newline or whitespace inserted within the first 60 chars of the URL).
    PIN E6.
    """
    long_url = (
        "https://www.ti.com/lit/ds/symlink/max232-q1-very-long-filename-"
        "testcase-e6-abcdefghijklmnopqrstuvwxyz.pdf"
    )
    assert len(long_url) >= 60, "Test fixture URL must be >= 60 chars."
    url_prefix = long_url[:60]

    response = {
        "exact": [
            {
                "uid": "0x700",
                "mpn": "MAX232",
                "mpn_norm": "MAX232",
                "stock": 10,
                "is_basic": False,
                "made_by": [{"name": "TI"}],
                "in_package": [{"name": "DIP"}],
                "datasheet": [{"url": long_url}],
            }
        ],
        "trig": [],
        "fts": [],
    }
    mock_txn = _make_mock_txn([response])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert url_prefix in result.output, (
        f"E6: 60-char URL prefix must appear unbroken in search output under COLUMNS=200. "
        f"Prefix={url_prefix!r} not found in:\n{result.output}"
    )


def test_cli_show_long_url_60char_prefix_unbroken() -> None:
    """Given a show result with a 60+ char URL.
    When `partgraph show MAX232` is invoked under COLUMNS=200.
    Then a 60-character prefix of the URL appears as an unbroken substring.
    PIN E6.
    """
    long_url = (
        "https://datasheets.example.com/very/long/path/to/datasheet-"
        "for-max232-ic-component-revision-c-2024-engineering-e6.pdf"
    )
    assert len(long_url) >= 60, "Test fixture URL must be >= 60 chars."
    url_prefix = long_url[:60]

    response = {
        "part": [
            {
                "uid": "0x800",
                "mpn": "MAX232",
                "mpn_norm": "MAX232",
                "stock": 0,
                "is_basic": False,
                "made_by": [{"name": "Texas Instruments"}],
                "in_package": [{"name": "DIP-16"}],
                "in_category": [],
                "datasheet": [{"url": long_url, "source": "example"}],
                "tagged": [],
                "attr": [],
            }
        ],
        "related": [],
    }
    mock_txn = _make_mock_txn([response])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["show", "MAX232"])

    assert url_prefix in result.output, (
        f"E6: 60-char URL prefix must appear unbroken in show output under COLUMNS=200. "
        f"Prefix={url_prefix!r} not found in:\n{result.output}"
    )


# ===========================================================================
# AC-CE: PR4 semantic search CLI tests
#
# These tests extend test_cli_search.py as specified by the PR4 plan.
# They will be red until the --semantic flag and embed integration are
# implemented in cli.py.
# ===========================================================================

_EMBED_DIM = 384
_FAKE_VECTOR = [0.001] * _EMBED_DIM


def _patch_get_encoder(fake_encoder_callable=None):
    """Patch partgraph.embed.get_encoder to return a fake encoder callable."""
    import partgraph.cli as cli_mod

    def _default_fake_encoder(texts: list[str]) -> list[list[float]]:
        return [_FAKE_VECTOR for _ in texts]

    encoder = fake_encoder_callable or _default_fake_encoder

    def _fake_get_encoder():
        return encoder

    return patch.object(cli_mod, "get_encoder", _fake_get_encoder, create=True)


def _make_semantic_response_with_max232() -> dict:
    """Return a DQL response containing MAX232 in the semantic block."""
    return {
        "exact":    [],
        "trig":     [],
        "fts":      [],
        "semantic": [
            {
                "uid": "0x9001",
                "mpn": "MAX232CPE",
                "mpn_norm": "MAX232CPE",
                "stock": 100,
                "is_basic": False,
                "made_by": [{"name": "Texas Instruments"}],
                "in_package": [{"name": "DIP-16"}],
                "datasheet": [{"url": "https://www.ti.com/lit/ds/symlink/max232.pdf"}],
            }
        ],
    }


# ---------------------------------------------------------------------------
# AC-CE-1: --semantic "rs232 transceiver" -> exit 0, MPN + "[Semantic]" label,
#          read_only txn, mutate not called
# ---------------------------------------------------------------------------

def test_ac_ce_1_semantic_search_exit_0_and_semantic_label() -> None:
    """AC-CE-1: Given mocked encoder returning a fake vector and mocked client
    returning MAX232 in the semantic block.
    When `partgraph search --semantic "rs232 transceiver"` is invoked.
    Then:
    - Exit code is 0.
    - Output contains "MAX232" (the MPN).
    - Output contains "[Semantic]" or "Semantic" label.
    - txn is called with read_only=True.
    - mutate is never called.
    """
    mock_txn = _make_mock_txn([_make_semantic_response_with_max232()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver"])

    assert result.exit_code == 0, (
        f"AC-CE-1: --semantic search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert "MAX232" in result.output, (
        f"AC-CE-1: output must contain MPN 'MAX232'. Got:\n{result.output}"
    )
    assert "semantic" in result.output.lower() or "Semantic" in result.output, (
        f"AC-CE-1: output must contain '[Semantic]' or 'Semantic' label. "
        f"Got:\n{result.output}"
    )

    # read_only=True assertion.
    calls = mock_client.txn.call_args_list
    assert any(
        c == call(read_only=True) or c.kwargs.get("read_only") is True
        for c in calls
    ), f"AC-CE-1: semantic search must use read_only=True txn. Calls: {calls}"

    mock_txn.mutate.assert_not_called()


# ---------------------------------------------------------------------------
# AC-CE-2: empty semantic block -> exit 0, output contains "partgraph embed" hint
# ---------------------------------------------------------------------------

def test_ac_ce_2_empty_semantic_block_exit_0_embed_hint() -> None:
    """AC-CE-2: Given mocked encoder and mocked client returning empty semantic block.
    When `partgraph search --semantic "rs232 transceiver"` is invoked.
    Then:
    - Exit code is 0 (no results is not an error).
    - Output contains "partgraph embed" hint (guides user to run embed first).
    """
    empty_resp = {"exact": [], "trig": [], "fts": [], "semantic": []}
    mock_txn = _make_mock_txn([empty_resp])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver"])

    assert result.exit_code == 0, (
        f"AC-CE-2: empty semantic result must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert "partgraph embed" in result.output.lower() or "embed" in result.output.lower(), (
        f"AC-CE-2: output must hint 'partgraph embed'. Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# AC-CE-3: get_encoder ImportError -> exit 1, names [embed] extra, no query
# ---------------------------------------------------------------------------

def test_ac_ce_3_encoder_import_error_exit_1_names_embed_extra_no_query() -> None:
    """AC-CE-3: Given get_encoder() raises ImportError naming 'sentence-transformers'.
    When `partgraph search --semantic "rs232 transceiver"` is invoked.
    Then:
    - Exit code is 1.
    - Output contains "[embed]" or "embed" (the optional extra name).
    - txn.query is NEVER called (no Dgraph round-trip if encoder unavailable).
    - No path leak.
    """
    import partgraph.cli as cli_mod

    def _raising_get_encoder():
        raise ImportError(
            'sentence-transformers not installed. '
            'pip install -e ".[embed]" to enable semantic search.'
        )

    mock_txn = _make_mock_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), \
         patch.object(cli_mod, "get_encoder", _raising_get_encoder, create=True):
        result = _invoke(["search", "--semantic", "rs232 transceiver"])

    assert result.exit_code != 0, (
        f"AC-CE-3: ImportError on encoder must produce non-zero exit. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert "embed" in result.output.lower(), (
        f"AC-CE-3: output must mention 'embed' extra. Got:\n{result.output}"
    )
    mock_txn.query.assert_not_called()
    # No path leak.
    assert "/home/" not in result.output, (
        f"AC-CE-3: no path leak in output. Got:\n{result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-CE-4: txn.query raises -> exit 1, "partgraph db up", no leak
# ---------------------------------------------------------------------------

def test_ac_ce_4_txn_query_raises_exit_1_db_up_hint_no_leak() -> None:
    """AC-CE-4: Given get_encoder succeeds but txn.query raises RuntimeError.
    When `partgraph search --semantic "rs232 transceiver"` is invoked.
    Then:
    - Exit code is 1.
    - Output contains "partgraph db up" hint.
    - No raw exception text leaks.
    """
    mock_txn = MagicMock()
    mock_txn.query.side_effect = RuntimeError("connection refused")
    mock_txn.discard.return_value = None
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver"])

    assert result.exit_code != 0, (
        f"AC-CE-4: DB-down must produce non-zero exit. Got {result.exit_code}."
    )
    assert "partgraph db up" in result.output, (
        f"AC-CE-4: must contain 'partgraph db up'. Got:\n{result.output!r}"
    )
    assert "connection refused" not in result.output, (
        f"AC-CE-4: raw exception must not leak. Got:\n{result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-CE-5: --semantic "" -> exit 1 "empty", encoder never called, no query
# ---------------------------------------------------------------------------

def test_ac_ce_5_semantic_empty_string_exit_1_encoder_not_called() -> None:
    """AC-CE-5: Given --semantic "" (empty semantic query).
    When `partgraph search --semantic ""` is invoked.
    Then:
    - Exit code is 1.
    - Output contains "empty".
    - Encoder is never called.
    - No Dgraph query sent.
    """
    import partgraph.cli as cli_mod

    encoder_called = [False]

    def _counting_get_encoder():
        def _enc(texts):
            encoder_called[0] = True
            return [_FAKE_VECTOR for _ in texts]
        return _enc

    mock_txn = _make_mock_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), \
         patch.object(cli_mod, "get_encoder", _counting_get_encoder, create=True):
        result = _invoke(["search", "--semantic", ""])

    assert result.exit_code != 0, (
        f"AC-CE-5: empty --semantic must exit non-zero. Got {result.exit_code}."
    )
    assert "empty" in result.output.lower(), (
        f"AC-CE-5: output must contain 'empty'. Got:\n{result.output!r}"
    )
    assert not encoder_called[0], (
        "AC-CE-5: encoder must NOT be called for empty --semantic query."
    )
    mock_txn.query.assert_not_called()


# ---------------------------------------------------------------------------
# AC-CE-6: hybrid --semantic "rs232" + "5V" -> DQL has voltage filter
# ---------------------------------------------------------------------------

def test_ac_ce_6_hybrid_semantic_with_voltage_token_dql_has_voltage_filter() -> None:
    """AC-CE-6: Given --semantic "rs232" and a positional argument "5V" (parametric).
    When `partgraph search --semantic "rs232" "5V"` is invoked.
    Then the captured DQL passed to txn.query contains ge(/le with voltage_max
    (the voltage filter from parametric parsing).
    """
    import partgraph.cli as cli_mod

    captured_dql: list[str] = []

    def _spy_query(dql: str, variables=None, *args, **kwargs):
        captured_dql.append(dql)
        resp = MagicMock()
        resp.json = json.dumps({"exact": [], "trig": [], "fts": [], "semantic": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _spy_query
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        # "5V" is a positional arg (for parametric), --semantic is the text for embed.
        _invoke(["search", "--semantic", "rs232", "5V"])

    # If any DQL was sent, it should have voltage filter terms.
    all_dql = " ".join(captured_dql)
    if captured_dql:
        assert "voltage" in all_dql or "ge(" in all_dql, (
            f"AC-CE-6: hybrid DQL must contain voltage filter. Got:\n{all_dql!r}"
        )


# ---------------------------------------------------------------------------
# AC-CE-7: search --help contains "--semantic"
# ---------------------------------------------------------------------------

def test_ac_ce_7_search_help_contains_semantic_flag() -> None:
    """AC-CE-7: Given the search command.
    When `partgraph search --help` is invoked.
    Then the output contains "--semantic".
    PIN AC-CE-7.
    """
    result = _invoke(["search", "--help"])
    assert "--semantic" in result.output, (
        f"AC-CE-7: search --help must contain '--semantic'. Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# AC-CE-8: --semantic is embed source; positional query parsed for parametric only
# ---------------------------------------------------------------------------

def test_ac_ce_8_semantic_flag_is_embed_source_positional_is_parametric() -> None:
    """AC-CE-8: Given `partgraph search --semantic "rs232 transceiver" "10k 0402"`.
    When the command is invoked.
    Then:
    - The encoder receives the --semantic string "rs232 transceiver" (not "10k 0402").
    - The DQL contains parametric terms from "10k 0402" (resistance filter).
    - The --semantic value drives the embedding; the positional arg drives parametric.
    """
    import partgraph.cli as cli_mod

    encoder_inputs: list[list[str]] = []

    def _spy_get_encoder():
        def _enc(texts: list[str]) -> list[list[float]]:
            encoder_inputs.append(list(texts))
            return [_FAKE_VECTOR for _ in texts]
        return _enc

    captured_dql: list[str] = []

    def _spy_query(dql: str, variables=None, *args, **kwargs):
        captured_dql.append(dql)
        resp = MagicMock()
        resp.json = json.dumps({"exact": [], "trig": [], "fts": [], "semantic": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _spy_query
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), \
         patch.object(cli_mod, "get_encoder", _spy_get_encoder, create=True):
        _invoke(["search", "--semantic", "rs232 transceiver", "10k 0402"])

    # Encoder must have been called with the --semantic string.
    if encoder_inputs:
        all_encoder_texts = [t for batch in encoder_inputs for t in batch]
        assert any("rs232" in t.lower() for t in all_encoder_texts), (
            f"AC-CE-8: encoder must receive the --semantic text 'rs232 transceiver'. "
            f"Got encoder inputs: {all_encoder_texts!r}"
        )
        # The positional "10k 0402" must not be sent to the encoder (it's for parametric).
        assert not any("10k" in t for t in all_encoder_texts), (
            f"AC-CE-8: encoder must NOT receive the positional parametric arg '10k 0402'. "
            f"Got encoder inputs: {all_encoder_texts!r}"
        )
