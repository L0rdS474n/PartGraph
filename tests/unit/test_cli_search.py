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
    """AC-CE-2 (REWRITTEN — hybrid semantic search PR, AC-HY-13/14: the
    empty-semantic-result path now issues a has(embedding) PROBE before
    deciding which hint to print): Given mocked encoder and mocked client
    where BOTH the semantic search query AND the follow-up has(embedding)
    probe return EMPTY.
    When `partgraph search --semantic "rs232 transceiver"` is invoked.
    Then:
    - Exit code is 0 (no results is not an error).
    - Output contains "partgraph embed" hint (guides user to run embed
      first) — the genuine-empty-index case (the probe finds nothing
      either).

    CHANGED FROM PRE-HYBRID (documented, not silent): the pre-hybrid CLI
    printed the embed hint unconditionally on any empty semantic result. The
    hybrid CLI now probes `{ probe(func: has(embedding), first: 1) { uid } }`
    first; only an EMPTY probe (0 rows — genuinely no embeddings at all)
    still prints this hint. The mock client's second canned response
    ({"probe": []}) supplies that empty probe result so this still-valid
    scenario keeps passing; AC-HY-13 covers the NEW populated-probe
    ("starvation", not "run embed") branch.
    """
    empty_resp = {"exact": [], "trig": [], "fts": [], "semantic": []}
    empty_probe_resp = {"probe": []}
    mock_txn = _make_mock_txn([empty_resp, empty_probe_resp])
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


# ===========================================================================
# AC-SF: issue #15 PR1 — structured search filters (CLI flags)
#
# New `partgraph search` flags under test:
#   --manufacturer TEXT   --package TEXT   --category TEXT
#   --in-stock            --min-stock INT  --basic  --extended
#   --max-price FLOAT
#
# These flags DO NOT EXIST YET on the search command. Until implemented,
# Click/Typer rejects them as "No such option" (a usage error, exit code 2) —
# that is the correct RED state for the help/flag-presence and DQL-spy tests
# below (a per-test runtime failure, never a collection error, since
# partgraph.cli itself imports fine today).
#
# PINNED fixed error strings (this test suite defines them as the acceptance
# contract — AC-SF-28: every new validation error is a FIXED, path-free
# string, exit 1, emitted BEFORE _build_dgraph_client is ever called):
#   - bad --package charset:         "--package must be"
#   - --package given twice:         "package given twice" ... "only one"
#   - --in-stock + --min-stock:      "--in-stock and --min-stock"
#   - bad --min-stock value:         "--min-stock must be"
#   - --basic + --extended:          "--basic and --extended"
#   - bad --max-price value:         "--max-price must be"
# ===========================================================================

def _make_capturing_txn(
    responses: dict | list[dict] | None = None,
) -> tuple[MagicMock, list[tuple[str, dict]]]:
    """Return (mock_txn, captured) where captured accumulates (dql, variables)
    for every txn.query call, in order.

    Generalises the ad hoc spy pattern already used by
    test_cli_search_injection_token_not_in_query_text and
    test_ac_ce_6_hybrid_semantic_with_voltage_token_dql_has_voltage_filter so
    the new AC-SF filter-flag tests can inspect the exact DQL/variables sent
    to Dgraph without duplicating the boilerplate per test.
    """
    response_list = (
        responses if isinstance(responses, list) else [responses or {"exact": [], "trig": [], "fts": []}]
    )
    captured: list[tuple[str, dict]] = []
    call_counter = [0]

    def _spy_query(dql: str, variables: dict | None = None, *args, **kwargs):
        captured.append((dql, variables or {}))
        idx = min(call_counter[0], len(response_list) - 1)
        call_counter[0] += 1
        resp = MagicMock()
        resp.json = json.dumps(response_list[idx]).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _spy_query
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    return mock_txn, captured


# ---------------------------------------------------------------------------
# Help-copy pins: every new flag must be documented in --help (mirrors E1).
# ---------------------------------------------------------------------------

def test_cli_search_help_contains_manufacturer_flag() -> None:
    """PIN: `partgraph search --help` must document --manufacturer."""
    result = _invoke(["search", "--help"])
    assert "--manufacturer" in result.output, (
        f"AC-SF: search --help must contain '--manufacturer'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_package_flag_option() -> None:
    """PIN: `partgraph search --help` must document --package."""
    result = _invoke(["search", "--help"])
    assert "--package" in result.output, (
        f"AC-SF: search --help must contain '--package'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_category_flag() -> None:
    """PIN: `partgraph search --help` must document --category."""
    result = _invoke(["search", "--help"])
    assert "--category" in result.output, (
        f"AC-SF: search --help must contain '--category'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_in_stock_flag() -> None:
    """PIN: `partgraph search --help` must document --in-stock."""
    result = _invoke(["search", "--help"])
    assert "--in-stock" in result.output, (
        f"AC-SF: search --help must contain '--in-stock'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_min_stock_flag() -> None:
    """PIN: `partgraph search --help` must document --min-stock."""
    result = _invoke(["search", "--help"])
    assert "--min-stock" in result.output, (
        f"AC-SF: search --help must contain '--min-stock'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_basic_flag() -> None:
    """PIN: `partgraph search --help` must document --basic."""
    result = _invoke(["search", "--help"])
    assert "--basic" in result.output, (
        f"AC-SF: search --help must contain '--basic'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_extended_flag() -> None:
    """PIN: `partgraph search --help` must document --extended."""
    result = _invoke(["search", "--help"])
    assert "--extended" in result.output, (
        f"AC-SF: search --help must contain '--extended'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_max_price_flag() -> None:
    """PIN: `partgraph search --help` must document --max-price."""
    result = _invoke(["search", "--help"])
    assert "--max-price" in result.output, (
        f"AC-SF: search --help must contain '--max-price'. Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# AC-SF-1: --manufacturer binds $var, omitted from query text, exit 0
# ---------------------------------------------------------------------------

def test_ac_sf_1_manufacturer_flag_binds_value_and_omits_from_query_text() -> None:
    """AC-SF-1: Given `partgraph search MAX232 --manufacturer "Texas Instruments"`.
    When invoked against a mocked pydgraph client (DQL spy).
    Then exit 0; the captured variables carry "Texas Instruments" as a value;
    the captured DQL never contains the literal manufacturer string; the
    made_by filter uses allofterms.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--manufacturer", "Texas Instruments"])

    assert result.exit_code == 0, (
        f"AC-SF-1: search with --manufacturer must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "AC-SF-1: expected at least one Dgraph query to be sent."
    dql, variables = captured[0]
    assert "Texas Instruments" in variables.values(), (
        f"AC-SF-1: expected 'Texas Instruments' bound as a $var. Got: {variables}"
    )
    assert "Texas Instruments" not in dql, (
        f"AC-SF-1: manufacturer string must never appear in the query text. Got:\n{dql}"
    )
    assert "made_by" in dql
    mb_idx = dql.index("made_by")
    assert "allofterms(" in dql[mb_idx : mb_idx + 120]


# ---------------------------------------------------------------------------
# AC-SF-2: mixed-case manufacturer -> allofterms (case-insensitive recall)
# ---------------------------------------------------------------------------

def test_ac_sf_2_manufacturer_mixed_case_uses_allofterms_case_insensitive() -> None:
    """AC-SF-2: Given `--manufacturer "texas instruments"` (lowercase input).
    When invoked.
    Then the captured DQL uses allofterms(name, $var) for the made_by filter
    (LIVE-CONFIRMED: a lowercase-bound $var matches "Texas Instruments" /
    "TEXAS INSTRUMENTS" / "texas instruments" nodes via allofterms).
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        _invoke(["search", "MAX232", "--manufacturer", "texas instruments"])

    assert captured, "Expected at least one Dgraph query to be sent."
    dql, variables = captured[0]
    mb_idx = dql.index("made_by")
    nearby = dql[mb_idx : mb_idx + 120]
    assert "allofterms(" in nearby, (
        f"AC-SF-2: made_by filter must use allofterms for case-insensitive recall. "
        f"Nearby: {nearby!r}"
    )
    assert "texas instruments" in variables.values()


# ---------------------------------------------------------------------------
# AC-SF-3 / AC-SF-18: injection — hostile manufacturer/category value
# ---------------------------------------------------------------------------

def test_ac_sf_3_manufacturer_injection_value_only_in_variables() -> None:
    """AC-SF-3: Given a hostile --manufacturer value 'TI") OR eq(x,"'.
    When invoked.
    Then the command does not crash, and the hostile value appears only in
    the captured variables, never in the DQL text.
    """
    hostile = 'TI") OR eq(x,"'
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--manufacturer", hostile])

    assert result.exception is None, (
        f"AC-SF-3: hostile --manufacturer must not raise. Got: {result.exception}"
    )
    assert captured, "Expected at least one Dgraph query to be sent."
    dql, variables = captured[0]
    assert hostile in variables.values(), (
        f"AC-SF-3: expected hostile value bound as a $var. Got: {variables}"
    )
    assert hostile not in dql, (
        f"AC-SF-3: hostile manufacturer value must never appear in query text. Got:\n{dql}"
    )


def test_ac_sf_18_category_injection_value_only_in_variables() -> None:
    """AC-SF-18: Given a hostile --category value 'RS232 ICs") OR eq(x,"'.
    When invoked.
    Then the command does not crash, and the hostile value appears only in
    the captured variables, never in the DQL text.
    """
    hostile = 'RS232 ICs") OR eq(x,"'
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--category", hostile])

    assert result.exception is None, (
        f"AC-SF-18: hostile --category must not raise. Got: {result.exception}"
    )
    assert captured, "Expected at least one Dgraph query to be sent."
    dql, variables = captured[0]
    assert hostile in variables.values(), (
        f"AC-SF-18: expected hostile value bound as a $var. Got: {variables}"
    )
    assert hostile not in dql, (
        f"AC-SF-18: hostile category value must never appear in query text. Got:\n{dql}"
    )


# ---------------------------------------------------------------------------
# AC-SF-4: --package "soic-16" -> uppercased, bound as $pkg
# ---------------------------------------------------------------------------

def test_ac_sf_4_package_flag_lowercase_uppercased_and_bound() -> None:
    """AC-SF-4: Given `--package "soic-16"` (lowercase input).
    When invoked.
    Then exit 0; the captured variables carry "SOIC-16" (uppercased) bound to
    $pkg; the query text contains in_package and eq(.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--package", "soic-16"])

    assert result.exit_code == 0, (
        f"AC-SF-4: valid --package must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "Expected at least one Dgraph query to be sent."
    dql, variables = captured[0]
    assert "SOIC-16" in variables.values(), (
        f"AC-SF-4: expected the uppercased 'SOIC-16' bound as a $var. Got: {variables}"
    )
    assert "in_package" in dql
    assert "eq(" in dql


# ---------------------------------------------------------------------------
# AC-SF-4b: bad --package charset -> exit 1, fixed error, no DB query
# ---------------------------------------------------------------------------

def test_ac_sf_4b_package_with_spaces_exits_1_no_db_query() -> None:
    """AC-SF-4b: Given `--package "RS232 ICs"` (contains spaces — fails the
    ^[A-Z0-9][A-Z0-9-]{0,19}$ charset).
    When invoked.
    Then exit code 1 and NO Dgraph query is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--package", "RS232 ICs"])

    assert result.exit_code == 1, (
        f"AC-SF-4b: bad --package charset must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert not captured, "AC-SF-4b: no Dgraph query may be sent for an invalid --package."
    assert "--package must be" in result.output, (
        f"AC-SF-4b/28: expected fixed '--package must be' error text. Got:\n{result.output}"
    )


def test_ac_sf_4b_package_too_long_exits_1_no_db_query() -> None:
    """AC-SF-4b: Given a --package value of 21 chars (over the 20-char limit).
    When invoked.
    Then exit code 1 and NO Dgraph query is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--package", "A" * 21])

    assert result.exit_code == 1, (
        f"AC-SF-4b: too-long --package must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert not captured, "AC-SF-4b: no Dgraph query may be sent for an invalid --package."


def test_ac_sf_4b_package_charset_error_is_path_free() -> None:
    """AC-SF-4b / AC-SF-28: Given an invalid --package value.
    When invoked.
    Then the output does not leak a filesystem path or a raw traceback.
    """
    mock_txn, _captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--package", "RS232 ICs"])

    assert "/home/" not in result.output, (
        f"AC-SF-28: no path leak. Got: {result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"AC-SF-28: no raw traceback. Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-SF-5: --package given both positionally and via --package -> exit 1
# ---------------------------------------------------------------------------

def test_ac_sf_5_package_given_twice_exits_1_no_db_query() -> None:
    """AC-SF-5: Given a positional query that ALSO carries a package token
    ("SOIC-16 MAX232" -> parsed.package="SOIC-16") PLUS --package "PDIP-16"
    supplying a second, different package.
    When invoked.
    Then exit code 1 with a fixed, path-free "package given twice ... use only
    one" error, and NO Dgraph query is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "SOIC-16 MAX232", "--package", "PDIP-16"])

    assert result.exit_code == 1, (
        f"AC-SF-5: package given twice must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert not captured, "AC-SF-5: no Dgraph query may be sent when package is given twice."
    assert "package given twice" in result.output.lower(), (
        f"AC-SF-5/28: expected fixed 'package given twice' error text. Got:\n{result.output}"
    )
    assert "only one" in result.output.lower(), (
        f"AC-SF-5/28: expected fixed '... use only one' error text. Got:\n{result.output}"
    )


def test_ac_sf_5_package_given_twice_error_is_path_free() -> None:
    """AC-SF-5 / AC-SF-28: Given the package-given-twice collision.
    When invoked.
    Then the output contains no filesystem path or raw traceback.
    """
    mock_txn, _captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "SOIC-16 MAX232", "--package", "PDIP-16"])

    assert "/home/" not in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# AC-SF-6: --category binds $cat, filters in_category, exit 0
# ---------------------------------------------------------------------------

def test_ac_sf_6_category_flag_binds_cat_and_filters_in_category() -> None:
    """AC-SF-6: Given `--category "RS232 ICs"`.
    When invoked.
    Then exit 0; captured variables carry "RS232 ICs"; the query text contains
    in_category and allofterms(; the category string is absent from the query
    text.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--category", "RS232 ICs"])

    assert result.exit_code == 0, (
        f"AC-SF-6: search with --category must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "Expected at least one Dgraph query to be sent."
    dql, variables = captured[0]
    assert "RS232 ICs" in variables.values()
    assert "RS232 ICs" not in dql
    assert "in_category" in dql
    cat_idx = dql.index("in_category")
    assert "allofterms(" in dql[cat_idx : cat_idx + 120]


# ---------------------------------------------------------------------------
# AC-SF-7: --in-stock -> ge(stock, 1) INT literal
# ---------------------------------------------------------------------------

def test_ac_sf_7_in_stock_flag_emits_ge_stock_1_int_literal() -> None:
    """AC-SF-7: Given `--in-stock`.
    When invoked.
    Then the captured DQL contains ge(stock, 1) — never ge(stock, 1.0).
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--in-stock"])

    assert result.exit_code == 0, (
        f"AC-SF-7: --in-stock must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured
    dql, _variables = captured[0]
    assert re.search(r"ge\(\s*stock\s*,\s*1\s*\)", dql), (
        f"AC-SF-7: expected ge(stock, 1). Got:\n{dql}"
    )
    assert not re.search(r"ge\(\s*stock\s*,\s*1\.0\s*\)", dql), (
        f"AC-SF-7: stock literal must be an INT, never '1.0'. Got:\n{dql}"
    )


# ---------------------------------------------------------------------------
# AC-SF-8: --min-stock 5 -> ge(stock, 5) INT literal
# ---------------------------------------------------------------------------

def test_ac_sf_8_min_stock_flag_emits_ge_stock_n_int_literal() -> None:
    """AC-SF-8: Given `--min-stock 5`.
    When invoked.
    Then the captured DQL contains ge(stock, 5) — never ge(stock, 5.0).
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--min-stock", "5"])

    assert result.exit_code == 0, (
        f"AC-SF-8: --min-stock 5 must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured
    dql, _variables = captured[0]
    assert re.search(r"ge\(\s*stock\s*,\s*5\s*\)", dql), (
        f"AC-SF-8: expected ge(stock, 5). Got:\n{dql}"
    )
    assert not re.search(r"ge\(\s*stock\s*,\s*5\.0\s*\)", dql), (
        f"AC-SF-8: stock literal must be an INT, never '5.0'. Got:\n{dql}"
    )


# ---------------------------------------------------------------------------
# AC-SF-9: --in-stock + --min-stock together -> exit 1, no DB query
# ---------------------------------------------------------------------------

def test_ac_sf_9_in_stock_and_min_stock_together_exits_1_no_db_query() -> None:
    """AC-SF-9: Given both `--in-stock` and `--min-stock 5`.
    When invoked.
    Then exit code 1 with a fixed error naming both flags, and NO Dgraph query
    is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--in-stock", "--min-stock", "5"])

    assert result.exit_code == 1, (
        f"AC-SF-9: --in-stock + --min-stock together must exit 1. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert not captured, "AC-SF-9: no Dgraph query may be sent on this collision."
    assert "--in-stock and --min-stock" in result.output, (
        f"AC-SF-9/28: expected fixed '--in-stock and --min-stock' error text. "
        f"Got:\n{result.output}"
    )
    assert "/home/" not in result.output, (
        f"AC-SF-9/Security-SHOULD-1: no path leak. Got: {result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"AC-SF-9/Security-SHOULD-1: no raw traceback. Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-SF-10: bad --min-stock values -> exit 1, no DB query
# ---------------------------------------------------------------------------

def test_ac_sf_10_min_stock_negative_exits_1_no_db_query() -> None:
    """AC-SF-10: Given `--min-stock -1`.
    When invoked.
    Then exit code 1 and NO Dgraph query is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--min-stock", "-1"])

    assert result.exit_code == 1, (
        f"AC-SF-10: --min-stock -1 must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert not captured
    assert "/home/" not in result.output, (
        f"AC-SF-10/Security-SHOULD-1: no path leak. Got: {result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"AC-SF-10/Security-SHOULD-1: no raw traceback. Got: {result.output!r}"
    )


def test_ac_sf_10_min_stock_fractional_exits_1_no_db_query() -> None:
    """AC-SF-10: Given `--min-stock 1.5`.
    When invoked.
    Then exit code 1 and NO Dgraph query is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--min-stock", "1.5"])

    assert result.exit_code == 1, (
        f"AC-SF-10: --min-stock 1.5 must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert not captured
    assert "/home/" not in result.output, (
        f"AC-SF-10/Security-SHOULD-1: no path leak. Got: {result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"AC-SF-10/Security-SHOULD-1: no raw traceback. Got: {result.output!r}"
    )


def test_ac_sf_10_min_stock_non_numeric_exits_1_no_db_query() -> None:
    """AC-SF-10: Given `--min-stock foo`.
    When invoked.
    Then exit code 1 and NO Dgraph query is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--min-stock", "foo"])

    assert result.exit_code == 1, (
        f"AC-SF-10: --min-stock foo must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert not captured
    assert "--min-stock must be" in result.output, (
        f"AC-SF-10/28: expected fixed '--min-stock must be' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output, (
        f"AC-SF-10/Security-SHOULD-1: no path leak. Got: {result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"AC-SF-10/Security-SHOULD-1: no raw traceback. Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-SF-11 / AC-SF-12: --basic / --extended
# ---------------------------------------------------------------------------

def test_ac_sf_11_basic_flag_emits_eq_is_basic_true() -> None:
    """AC-SF-11: Given `--basic`.
    When invoked.
    Then the captured DQL contains eq(is_basic, true).
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--basic"])

    assert result.exit_code == 0, (
        f"AC-SF-11: --basic must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured
    dql, _variables = captured[0]
    assert re.search(r"eq\(\s*is_basic\s*,\s*true\s*\)", dql), (
        f"AC-SF-11: expected eq(is_basic, true). Got:\n{dql}"
    )


def test_ac_sf_12_extended_flag_emits_eq_is_basic_false() -> None:
    """AC-SF-12: Given `--extended`.
    When invoked.
    Then the captured DQL contains eq(is_basic, false).
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--extended"])

    assert result.exit_code == 0, (
        f"AC-SF-12: --extended must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured
    dql, _variables = captured[0]
    assert re.search(r"eq\(\s*is_basic\s*,\s*false\s*\)", dql), (
        f"AC-SF-12: expected eq(is_basic, false). Got:\n{dql}"
    )


# ---------------------------------------------------------------------------
# AC-SF-13: --basic + --extended together -> exit 1, no DB query
# ---------------------------------------------------------------------------

def test_ac_sf_13_basic_and_extended_together_exits_1_no_db_query() -> None:
    """AC-SF-13: Given both `--basic` and `--extended`.
    When invoked.
    Then exit code 1 with a fixed error naming both flags, and NO Dgraph query
    is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--basic", "--extended"])

    assert result.exit_code == 1, (
        f"AC-SF-13: --basic + --extended together must exit 1. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert not captured, "AC-SF-13: no Dgraph query may be sent on this collision."
    assert "--basic and --extended" in result.output, (
        f"AC-SF-13/28: expected fixed '--basic and --extended' error text. "
        f"Got:\n{result.output}"
    )
    assert "/home/" not in result.output, (
        f"AC-SF-13/Security-SHOULD-1: no path leak. Got: {result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"AC-SF-13/Security-SHOULD-1: no raw traceback. Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-SF-14: --max-price
# ---------------------------------------------------------------------------

def test_ac_sf_14_max_price_valid_emits_le_price_usd_float() -> None:
    """AC-SF-14: Given `--max-price 0.5`.
    When invoked.
    Then the captured DQL contains le(price_usd, 0.5).
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--max-price", "0.5"])

    assert result.exit_code == 0, (
        f"AC-SF-14: --max-price 0.5 must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured
    dql, _variables = captured[0]
    assert re.search(r"le\(\s*price_usd\s*,\s*0\.5\s*\)", dql), (
        f"AC-SF-14: expected le(price_usd, 0.5). Got:\n{dql}"
    )


def test_ac_sf_14_max_price_negative_exits_1_no_db_query() -> None:
    """AC-SF-14: Given `--max-price -1`.
    When invoked.
    Then exit code 1 and NO Dgraph query is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--max-price", "-1"])

    assert result.exit_code == 1, (
        f"AC-SF-14: --max-price -1 must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert not captured
    assert "/home/" not in result.output, (
        f"AC-SF-14/Security-SHOULD-1: no path leak. Got: {result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"AC-SF-14/Security-SHOULD-1: no raw traceback. Got: {result.output!r}"
    )


def test_ac_sf_14_max_price_non_numeric_exits_1_no_db_query() -> None:
    """AC-SF-14: Given `--max-price abc`.
    When invoked.
    Then exit code 1 and NO Dgraph query is sent.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--max-price", "abc"])

    assert result.exit_code == 1, (
        f"AC-SF-14: --max-price abc must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert not captured
    assert "--max-price must be" in result.output, (
        f"AC-SF-14/28: expected fixed '--max-price must be' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output, (
        f"AC-SF-14/Security-SHOULD-1: no path leak. Got: {result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"AC-SF-14/Security-SHOULD-1: no raw traceback. Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-SF-15: composition — MPN + manufacturer + min_stock -> ONE query
# ---------------------------------------------------------------------------

def test_ac_sf_15_cli_composition_mpn_manufacturer_min_stock_single_query() -> None:
    """AC-SF-15: Given `search "MAX232 0402 1%" --manufacturer "Texas Instruments"
    --min-stock 10`, with the mocked hard-pass response non-empty (so the
    relaxed pass never triggers).
    When invoked.
    Then exactly ONE query is sent to Dgraph, and it carries the MPN text
    terms, the tolerance_pct parametric term, the package eq() filter, the
    manufacturer allofterms() filter, and ge(stock, 10) — all AND-composed.
    """
    non_empty = {
        "exact": [
            {
                "uid": "0xF01",
                "mpn": "MAX232",
                "mpn_norm": "MAX232",
                "stock": 50,
                "is_basic": False,
                "made_by": [{"name": "Texas Instruments"}],
                "in_package": [{"name": "0402"}],
                "datasheet": [{"url": "https://example.com/ds.pdf"}],
            }
        ],
        "trig": [],
        "fts": [],
    }
    mock_txn, captured = _make_capturing_txn(non_empty)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(
            [
                "search",
                "MAX232 0402 1%",
                "--manufacturer",
                "Texas Instruments",
                "--min-stock",
                "10",
            ]
        )

    assert result.exit_code == 0, (
        f"AC-SF-15: composed search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert len(captured) == 1, (
        f"AC-SF-15: expected exactly ONE Dgraph query (hard pass has rows; no "
        f"relaxed pass needed). Got {len(captured)} calls: {[c[0] for c in captured]}"
    )
    dql, variables = captured[0]
    assert "mpn_norm" in dql
    assert "tolerance_pct" in dql
    assert "in_package" in dql
    assert "made_by" in dql
    assert re.search(r"ge\(\s*stock\s*,\s*10\s*\)", dql), (
        f"AC-SF-15: expected ge(stock, 10). Got:\n{dql}"
    )
    assert variables.get("$mfr") == "Texas Instruments" or "Texas Instruments" in variables.values()


# ---------------------------------------------------------------------------
# AC-SF-16: --semantic + --manufacturer composition
# ---------------------------------------------------------------------------

def test_ac_sf_16_semantic_search_with_manufacturer_flag_composes_in_dql() -> None:
    """AC-SF-16: Given `--semantic "rs232 transceiver" --manufacturer "Texas
    Instruments"`.
    When invoked (mocked encoder + mocked client).
    Then exit 0; the captured DQL carries the made_by allofterms filter
    extending the SAME similar_to(...) block (not a Python post-filter); the
    manufacturer string never appears in the query text.
    """
    captured: list[tuple[str, dict]] = []

    def _spy_query(dql: str, variables: dict | None = None, *args, **kwargs):
        captured.append((dql, variables or {}))
        resp = MagicMock()
        resp.json = json.dumps(_make_semantic_response_with_max232()).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _spy_query
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(
            ["search", "--semantic", "rs232 transceiver", "--manufacturer", "Texas Instruments"]
        )

    assert result.exit_code == 0, (
        f"AC-SF-16: semantic + manufacturer must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "AC-SF-16: expected at least one Dgraph query to be sent."
    dql, variables = captured[0]
    assert "similar_to" in dql
    assert "made_by" in dql
    mb_idx = dql.index("made_by")
    assert "allofterms(" in dql[mb_idx : mb_idx + 120]
    assert "Texas Instruments" not in dql
    assert "Texas Instruments" in variables.values()


# ---------------------------------------------------------------------------
# AC-SF-16 (Gate 3 scope decision — ALL filters compose with --semantic, not
# just --manufacturer): --semantic + --category / --min-stock / --basic /
# --max-price composition.
# ---------------------------------------------------------------------------

def test_ac_sf_16_semantic_search_with_category_flag_composes_in_dql() -> None:
    """AC-SF-16: Given `--semantic "rs232 transceiver" --category "RS232 ICs"`.
    When invoked (mocked encoder + mocked client).
    Then the captured DQL carries the in_category allofterms filter extending
    the SAME similar_to(...) block; the category string never appears in the
    query text.
    """
    captured: list[tuple[str, dict]] = []

    def _spy_query(dql: str, variables: dict | None = None, *args, **kwargs):
        captured.append((dql, variables or {}))
        resp = MagicMock()
        resp.json = json.dumps(_make_semantic_response_with_max232()).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _spy_query
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(
            ["search", "--semantic", "rs232 transceiver", "--category", "RS232 ICs"]
        )

    assert result.exit_code == 0, (
        f"AC-SF-16: semantic + category must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "AC-SF-16: expected at least one Dgraph query to be sent."
    dql, variables = captured[0]
    assert "similar_to" in dql
    assert "in_category" in dql
    cat_idx = dql.index("in_category")
    assert "allofterms(" in dql[cat_idx : cat_idx + 120]
    assert "RS232 ICs" not in dql
    assert "RS232 ICs" in variables.values()


def test_ac_sf_16_semantic_search_with_min_stock_flag_composes_in_dql() -> None:
    """AC-SF-16: Given `--semantic "rs232" --min-stock 10`.
    When invoked.
    Then the captured DQL carries ge(stock, 10) inside the semantic query.
    """
    mock_txn, captured = _make_capturing_txn(_make_semantic_response_with_max232())
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232", "--min-stock", "10"])

    assert result.exit_code == 0, (
        f"AC-SF-16: semantic + min-stock must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "AC-SF-16: expected at least one Dgraph query to be sent."
    dql, _variables = captured[0]
    assert "similar_to" in dql
    assert re.search(r"ge\(\s*stock\s*,\s*10\s*\)", dql), (
        f"AC-SF-16: expected ge(stock, 10) inside the semantic query. Got:\n{dql}"
    )


def test_ac_sf_16_semantic_search_with_basic_flag_composes_in_dql() -> None:
    """AC-SF-16: Given `--semantic "rs232" --basic`.
    When invoked.
    Then the captured DQL carries eq(is_basic, true) inside the semantic query.
    """
    mock_txn, captured = _make_capturing_txn(_make_semantic_response_with_max232())
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232", "--basic"])

    assert result.exit_code == 0, (
        f"AC-SF-16: semantic + basic must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "AC-SF-16: expected at least one Dgraph query to be sent."
    dql, _variables = captured[0]
    assert "similar_to" in dql
    assert re.search(r"eq\(\s*is_basic\s*,\s*true\s*\)", dql), (
        f"AC-SF-16: expected eq(is_basic, true) inside the semantic query. Got:\n{dql}"
    )


def test_ac_sf_16_semantic_search_with_max_price_flag_composes_in_dql() -> None:
    """AC-SF-16: Given `--semantic "rs232" --max-price 0.5`.
    When invoked.
    Then the captured DQL carries le(price_usd, 0.5) inside the semantic query.
    """
    mock_txn, captured = _make_capturing_txn(_make_semantic_response_with_max232())
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232", "--max-price", "0.5"])

    assert result.exit_code == 0, (
        f"AC-SF-16: semantic + max-price must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "AC-SF-16: expected at least one Dgraph query to be sent."
    dql, _variables = captured[0]
    assert "similar_to" in dql
    assert re.search(r"le\(\s*price_usd\s*,\s*0\.5\s*\)", dql), (
        f"AC-SF-16: expected le(price_usd, 0.5) inside the semantic query. Got:\n{dql}"
    )


# ---------------------------------------------------------------------------
# AC-SF-17: relaxed (nearest-match) pass still applies hard filter kwargs
# ---------------------------------------------------------------------------

def test_ac_sf_17_relaxed_pass_still_applies_min_stock_hard_constraint() -> None:
    """AC-SF-17: Given a nearest-match scenario ("1.2V MAX232", hard pass
    empty) PLUS `--min-stock 5`.
    When invoked.
    Then BOTH the hard-pass query and the relaxed-pass query carry
    ge(stock, 5) — min_stock is a hard constraint that must NOT be dropped by
    the relaxed pass (only the query-derived parametric quantities relax).
    """
    responses = [
        {"exact": [], "trig": [], "fts": []},  # Pass 1 (hard): empty.
        {
            "nearest": [
                {
                    "uid": "0x900",
                    "mpn": "MAX232CPE",
                    "mpn_norm": "MAX232CPE",
                    "stock": 50,
                    "is_basic": False,
                    "made_by": [{"name": "Texas Instruments"}],
                    "in_package": [{"name": "PDIP-16"}],
                    "datasheet": [{"url": "https://example.com/ds.pdf"}],
                }
            ]
        },
    ]
    mock_txn, captured = _make_capturing_txn(responses)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "1.2V MAX232", "--min-stock", "5"])

    assert result.exit_code == 0, (
        f"AC-SF-17: relaxed-pass search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert len(captured) == 2, (
        f"AC-SF-17: expected TWO Dgraph queries (hard pass empty -> relaxed "
        f"pass). Got {len(captured)} calls."
    )
    for i, (dql, _variables) in enumerate(captured):
        assert re.search(r"ge\(\s*stock\s*,\s*5\s*\)", dql), (
            f"AC-SF-17: pass {i + 1} must still carry ge(stock, 5) — min_stock "
            f"is a hard constraint, not relaxed. Got:\n{dql}"
        )


# ---------------------------------------------------------------------------
# AC-SF-17 (Gate 3 Architecture MUST-1): siblings for manufacturer / category /
# basic / max_price — EVERY hard filter kwarg must survive the relaxed pass,
# not just min_stock.
# ---------------------------------------------------------------------------

def _two_pass_nearest_responses(extra_row_fields: dict) -> list[dict]:
    """Return the standard two-pass (hard-empty, relaxed-nearest) response
    sequence used by every AC-SF-17 sibling test, with the given extra part
    fields merged into the single relaxed-pass row.
    """
    row = {
        "uid": "0x901",
        "mpn": "MAX232CPE",
        "mpn_norm": "MAX232CPE",
        "stock": 50,
        "is_basic": False,
        "made_by": [{"name": "Texas Instruments"}],
        "in_package": [{"name": "PDIP-16"}],
        "datasheet": [{"url": "https://example.com/ds.pdf"}],
    }
    row.update(extra_row_fields)
    return [
        {"exact": [], "trig": [], "fts": []},  # Pass 1 (hard): empty.
        {"nearest": [row]},
    ]


def test_ac_sf_17_relaxed_pass_still_applies_manufacturer_hard_constraint() -> None:
    """AC-SF-17 (Gate 3 Architecture MUST-1): Given a nearest-match scenario
    ("1.2V MAX232", hard pass empty) PLUS `--manufacturer "Texas Instruments"`.
    When invoked.
    Then BOTH the hard-pass query and the relaxed-pass query carry
    made_by @filter(allofterms(name, $mfr)) — manufacturer is a hard
    constraint, not relaxed.
    """
    responses = _two_pass_nearest_responses({})
    mock_txn, captured = _make_capturing_txn(responses)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(
            ["search", "1.2V MAX232", "--manufacturer", "Texas Instruments"]
        )

    assert result.exit_code == 0, (
        f"AC-SF-17: relaxed-pass search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert len(captured) == 2, (
        f"AC-SF-17: expected TWO Dgraph queries. Got {len(captured)} calls."
    )
    for i, (dql, _variables) in enumerate(captured):
        assert "made_by" in dql, (
            f"AC-SF-17: pass {i + 1} must still select made_by. Got:\n{dql}"
        )
        mb_idx = dql.index("made_by")
        assert "allofterms(" in dql[mb_idx : mb_idx + 120], (
            f"AC-SF-17: pass {i + 1} must still carry the made_by allofterms "
            f"filter — manufacturer is a hard constraint, not relaxed. "
            f"Got:\n{dql}"
        )


def test_ac_sf_17_relaxed_pass_still_applies_category_hard_constraint() -> None:
    """AC-SF-17 (Gate 3 Architecture MUST-1): Given a nearest-match scenario
    PLUS `--category "RS232 ICs"`.
    When invoked.
    Then BOTH passes carry in_category @filter(allofterms(name, $cat)).
    """
    responses = _two_pass_nearest_responses({})
    mock_txn, captured = _make_capturing_txn(responses)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "1.2V MAX232", "--category", "RS232 ICs"])

    assert result.exit_code == 0, (
        f"AC-SF-17: relaxed-pass search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert len(captured) == 2, (
        f"AC-SF-17: expected TWO Dgraph queries. Got {len(captured)} calls."
    )
    for i, (dql, _variables) in enumerate(captured):
        assert "in_category" in dql, (
            f"AC-SF-17: pass {i + 1} must still select in_category. Got:\n{dql}"
        )
        cat_idx = dql.index("in_category")
        assert "allofterms(" in dql[cat_idx : cat_idx + 120], (
            f"AC-SF-17: pass {i + 1} must still carry the in_category "
            f"allofterms filter — category is a hard constraint, not "
            f"relaxed. Got:\n{dql}"
        )


def test_ac_sf_17_relaxed_pass_still_applies_basic_hard_constraint() -> None:
    """AC-SF-17 (Gate 3 Architecture MUST-1): Given a nearest-match scenario
    PLUS `--basic`.
    When invoked.
    Then BOTH passes carry eq(is_basic, true).
    """
    responses = _two_pass_nearest_responses({})
    mock_txn, captured = _make_capturing_txn(responses)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "1.2V MAX232", "--basic"])

    assert result.exit_code == 0, (
        f"AC-SF-17: relaxed-pass search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert len(captured) == 2, (
        f"AC-SF-17: expected TWO Dgraph queries. Got {len(captured)} calls."
    )
    for i, (dql, _variables) in enumerate(captured):
        assert re.search(r"eq\(\s*is_basic\s*,\s*true\s*\)", dql), (
            f"AC-SF-17: pass {i + 1} must still carry eq(is_basic, true) — "
            f"--basic is a hard constraint, not relaxed. Got:\n{dql}"
        )


def test_ac_sf_17_relaxed_pass_still_applies_max_price_hard_constraint() -> None:
    """AC-SF-17 (Gate 3 Architecture MUST-1): Given a nearest-match scenario
    PLUS `--max-price 0.5`.
    When invoked.
    Then BOTH passes carry le(price_usd, 0.5).
    """
    responses = _two_pass_nearest_responses({})
    mock_txn, captured = _make_capturing_txn(responses)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "1.2V MAX232", "--max-price", "0.5"])

    assert result.exit_code == 0, (
        f"AC-SF-17: relaxed-pass search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert len(captured) == 2, (
        f"AC-SF-17: expected TWO Dgraph queries. Got {len(captured)} calls."
    )
    for i, (dql, _variables) in enumerate(captured):
        assert re.search(r"le\(\s*price_usd\s*,\s*0\.5\s*\)", dql), (
            f"AC-SF-17: pass {i + 1} must still carry le(price_usd, 0.5) — "
            f"--max-price is a hard constraint, not relaxed. Got:\n{dql}"
        )


# ---------------------------------------------------------------------------
# AC-SF-28: validation errors emitted BEFORE any Dgraph client is built
# (stronger than "txn.query not called" — the client/stub must never even be
# constructed, mirroring _validate_limit's cli.py ~L292-309 contract).
# ---------------------------------------------------------------------------

def test_ac_sf_28_in_stock_min_stock_collision_never_builds_dgraph_client() -> None:
    """AC-SF-28: Given the --in-stock + --min-stock collision.
    When invoked.
    Then _build_dgraph_client is NEVER called.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--in-stock", "--min-stock", "5"])

    assert result.exit_code == 1
    mock_build.assert_not_called()


def test_ac_sf_28_basic_extended_collision_never_builds_dgraph_client() -> None:
    """AC-SF-28: Given the --basic + --extended collision.
    When invoked.
    Then _build_dgraph_client is NEVER called.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--basic", "--extended"])

    assert result.exit_code == 1
    mock_build.assert_not_called()


def test_ac_sf_28_package_given_twice_never_builds_dgraph_client() -> None:
    """AC-SF-28: Given the package-given-twice collision.
    When invoked.
    Then _build_dgraph_client is NEVER called.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "SOIC-16 MAX232", "--package", "PDIP-16"])

    assert result.exit_code == 1
    mock_build.assert_not_called()


def test_ac_sf_28_package_charset_error_never_builds_dgraph_client() -> None:
    """AC-SF-28: Given an invalid --package charset value.
    When invoked.
    Then _build_dgraph_client is NEVER called.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--package", "RS232 ICs"])

    assert result.exit_code == 1
    mock_build.assert_not_called()


def test_ac_sf_28_bad_min_stock_value_never_builds_dgraph_client() -> None:
    """AC-SF-28: Given a non-numeric --min-stock value.
    When invoked.
    Then _build_dgraph_client is NEVER called.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--min-stock", "foo"])

    assert result.exit_code == 1
    mock_build.assert_not_called()


def test_ac_sf_28_bad_max_price_value_never_builds_dgraph_client() -> None:
    """AC-SF-28: Given a non-numeric --max-price value.
    When invoked.
    Then _build_dgraph_client is NEVER called.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--max-price", "abc"])

    assert result.exit_code == 1
    mock_build.assert_not_called()


# ===========================================================================
# Gate 3 (Security MUST): manufacturer/category value validation.
#
# A NEW permissive validator (distinct from the package charset regex — mfr/
# category legitimately contain spaces and >20 chars) must reject empty/
# whitespace-only values and enforce a NAMED length cap (DoS defense-in-depth,
# ADR-0007-style bound). The exact cap constant is NOT pinned here (Gate 4
# chooses it) — only that 500 chars is rejected and a normal ~40-char value is
# accepted. Folded into the AC-SF-28 "never builds client" family per Gate 3's
# instruction. PIN (first-pinned by this test suite, Gate 4 matches): fixed
# error substrings "--manufacturer must be" / "--category must be".
# ===========================================================================

def test_ac_sf_1_manufacturer_empty_string_exits_1_no_db_query() -> None:
    """Gate-3 Security MUST: Given `--manufacturer ""` (empty string).
    When invoked.
    Then exit code 1, a fixed path-free "--manufacturer must be" error, NO
    Dgraph query is sent, and _build_dgraph_client is NEVER called.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--manufacturer", ""])

    assert result.exit_code == 1, (
        f"Gate-3: empty --manufacturer must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--manufacturer must be" in result.output, (
        f"Gate-3: expected fixed '--manufacturer must be' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"Gate-3: no path/traceback leak. Got: {result.output!r}"
    )


def test_ac_sf_1_manufacturer_whitespace_only_exits_1_no_db_query() -> None:
    """Gate-3 Security MUST: Given `--manufacturer "   "` (whitespace-only).
    When invoked.
    Then exit code 1, fixed error, no Dgraph query, no client built.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--manufacturer", "   "])

    assert result.exit_code == 1, (
        f"Gate-3: whitespace-only --manufacturer must exit 1. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--manufacturer must be" in result.output, (
        f"Gate-3: expected fixed '--manufacturer must be' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"Gate-3: no path/traceback leak. Got: {result.output!r}"
    )


def test_ac_sf_1_manufacturer_oversized_exits_1_no_db_query() -> None:
    """Gate-3 Security MUST: Given a 500-char --manufacturer value (DoS
    defense-in-depth — the exact cap constant is NOT pinned here, only that
    500 chars is rejected).
    When invoked.
    Then exit code 1, fixed error, no Dgraph query, no client built.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--manufacturer", "A" * 500])

    assert result.exit_code == 1, (
        f"Gate-3: oversized (500-char) --manufacturer must exit 1. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--manufacturer must be" in result.output, (
        f"Gate-3: expected fixed '--manufacturer must be' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"Gate-3: no path/traceback leak. Got: {result.output!r}"
    )


def test_ac_sf_6_category_empty_string_exits_1_no_db_query() -> None:
    """Gate-3 Security MUST: Given `--category ""` (empty string).
    When invoked.
    Then exit code 1, a fixed path-free "--category must be" error, NO
    Dgraph query is sent, and _build_dgraph_client is NEVER called.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--category", ""])

    assert result.exit_code == 1, (
        f"Gate-3: empty --category must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--category must be" in result.output, (
        f"Gate-3: expected fixed '--category must be' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"Gate-3: no path/traceback leak. Got: {result.output!r}"
    )


def test_ac_sf_6_category_whitespace_only_exits_1_no_db_query() -> None:
    """Gate-3 Security MUST: Given `--category "   "` (whitespace-only).
    When invoked.
    Then exit code 1, fixed error, no Dgraph query, no client built.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--category", "   "])

    assert result.exit_code == 1, (
        f"Gate-3: whitespace-only --category must exit 1. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--category must be" in result.output, (
        f"Gate-3: expected fixed '--category must be' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"Gate-3: no path/traceback leak. Got: {result.output!r}"
    )


def test_ac_sf_6_category_oversized_exits_1_no_db_query() -> None:
    """Gate-3 Security MUST: Given a 500-char --category value (DoS
    defense-in-depth).
    When invoked.
    Then exit code 1, fixed error, no Dgraph query, no client built.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--category", "A" * 500])

    assert result.exit_code == 1, (
        f"Gate-3: oversized (500-char) --category must exit 1. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--category must be" in result.output, (
        f"Gate-3: expected fixed '--category must be' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"Gate-3: no path/traceback leak. Got: {result.output!r}"
    )


# ===========================================================================
# AC-SF-19..27, 32, 38: issue #15 PR2 — `partgraph search --sort` / `--json`
#
# New `partgraph search` flags/behavior under test:
#   --sort {relevance,stock,price}   (default: relevance)
#   --json                            (machine-readable envelope; suppresses
#                                       the Rich table / banners / footer)
#
# Neither flag exists yet on the search command. Until implemented, Typer/
# Click rejects them as "No such option" (a usage error, exit code 2) — the
# correct RED state (a per-test runtime failure, never a collection error,
# mirroring the established AC-SF flag-rollout pattern above).
# ===========================================================================

# ---------------------------------------------------------------------------
# Help-copy pins (mirrors the AC-SF help-copy pins above for every new flag).
# ---------------------------------------------------------------------------

def test_cli_search_help_contains_sort_flag() -> None:
    """PIN: `partgraph search --help` must document --sort."""
    result = _invoke(["search", "--help"])
    assert "--sort" in result.output, (
        f"AC-SF: search --help must contain '--sort'. Got:\n{result.output}"
    )


def test_cli_search_help_contains_json_flag() -> None:
    """PIN: `partgraph search --help` must document --json."""
    result = _invoke(["search", "--help"])
    assert "--json" in result.output, (
        f"AC-SF: search --help must contain '--json'. Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# --json fixtures
# ---------------------------------------------------------------------------

def _make_json_search_response() -> dict:
    """Single DQL response with one richly-populated part (AC-SF-24/25)."""
    return {
        "exact": [
            {
                "uid": "0xJS1",
                "mpn": "MAX232CPE",
                "mpn_norm": "MAX232CPE",
                "stock": 250,
                "is_basic": True,
                "price_usd": 0.4123,
                "made_by": [{"name": "Texas Instruments"}],
                "in_package": [{"name": "PDIP-16"}],
                "in_category": [{"name": "RS232 ICs"}],
                "datasheet": [
                    {"url": "https://www.ti.com/lit/ds/symlink/max232.pdf"},
                    {"url": "https://example.com/alt-max232.pdf"},
                ],
                "voltage_max": 5.5,
            }
        ],
        "trig": [],
        "fts": [],
    }


# ---------------------------------------------------------------------------
# AC-SF-24: --json stdout is exactly one JSON object; no Rich table/banners
# ---------------------------------------------------------------------------

def test_ac_sf_24_json_flag_stdout_is_single_json_object_with_envelope_keys() -> None:
    """AC-SF-24: Given a search result with one part.
    When `partgraph search MAX232 --json` is invoked.
    Then stdout parses as EXACTLY ONE JSON object via json.loads(), with keys
    {version, query, nearest_match, count, results}, count == len(results),
    and NO Rich table / banner / "Showing N result(s)." footer text.
    """
    mock_txn = _make_mock_txn([_make_json_search_response()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-24: --json search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    envelope = json.loads(result.output)  # must be exactly one JSON value.
    assert set(envelope) == {"version", "query", "nearest_match", "count", "results"}, (
        f"AC-SF-24: envelope must have exactly the 5 documented keys. "
        f"Got: {sorted(envelope)}"
    )
    assert envelope["version"] == 1
    assert envelope["count"] == len(envelope["results"])

    assert "Showing" not in result.output, (
        f"AC-SF-24: --json output must not contain the Rich footer. Got:\n{result.output}"
    )
    assert "No matches found" not in result.output
    for box_char in ("┃", "│", "┌", "└"):
        assert box_char not in result.output, (
            f"AC-SF-24: --json output must not contain a Rich table "
            f"(box-drawing char {box_char!r} found). Got:\n{result.output}"
        )


def test_ac_sf_24_json_output_has_no_ansi_escape_codes_raw() -> None:
    """AC-SF-24: Given a populated result.
    When `partgraph search MAX232 --json` is invoked.
    Then the RAW (unstripped) captured stdout contains NO ANSI escape
    sequences at all — --json must never go through Rich's colour styling,
    unlike the human table path.
    """
    mock_txn = _make_mock_txn([_make_json_search_response()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        raw_result = RUNNER.invoke(app, ["search", "MAX232", "--json"])

    # Exit-code check FIRST: today, --json is an unrecognized flag, and
    # Click's own "No such option" usage-error panel happens to carry no ANSI
    # codes either — which would make the ANSI-absence assertion below a
    # VACUOUS pass (failing for an unrelated reason) unless exit_code==0 is
    # asserted here first, forcing a genuine RED today.
    assert raw_result.exit_code == 0, (
        f"AC-SF-24: --json must exit 0. Got {raw_result.exit_code}.\n{raw_result.output}"
    )
    assert not _ANSI_RE.search(raw_result.output), (
        f"AC-SF-24: --json output must contain no ANSI escape codes (raw). "
        f"Got: {raw_result.output!r}"
    )


def test_ac_sf_24_json_flag_query_field_matches_raw_query() -> None:
    """AC-SF-24: Given `partgraph search "MAX232" --json`.
    When invoked.
    Then envelope["query"] == "MAX232" (the raw query string).
    """
    mock_txn = _make_mock_txn([_make_json_search_response()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-24: --json search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    envelope = json.loads(result.output)
    assert envelope["query"] == "MAX232", (
        f"AC-SF-24: envelope['query'] must equal the raw query text. "
        f"Got: {envelope['query']!r}"
    )


def test_ac_sf_24_json_flag_nearest_match_false_for_hard_hit() -> None:
    """AC-SF-24: Given a hard (exact-tier) hit.
    When `partgraph search MAX232 --json` is invoked.
    Then envelope["nearest_match"] is False (JSON boolean, not string).
    """
    mock_txn = _make_mock_txn([_make_json_search_response()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-24: --json search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    envelope = json.loads(result.output)
    assert envelope["nearest_match"] is False, (
        f"AC-SF-24: nearest_match must be JSON false for a hard hit. "
        f"Got: {envelope['nearest_match']!r}"
    )


def test_ac_sf_24_json_flag_no_truncate_is_a_no_op() -> None:
    """AC-SF-24: Given `partgraph search MAX232 --json --no-truncate`.
    When invoked.
    Then the output is IDENTICAL to `--json` alone — --no-truncate has no
    effect once --json is active (there is no column-cropping decision to
    make for a machine-readable envelope).
    """
    mock_txn_a = _make_mock_txn([_make_json_search_response()])
    mock_client_a = _make_mock_client(mock_txn_a)
    with _patch_dgraph(mock_client_a):
        result_plain = _invoke(["search", "MAX232", "--json"])

    mock_txn_b = _make_mock_txn([_make_json_search_response()])
    mock_client_b = _make_mock_client(mock_txn_b)
    with _patch_dgraph(mock_client_b):
        result_no_truncate = _invoke(["search", "MAX232", "--json", "--no-truncate"])

    # Exit-code checks FIRST (and separately): today, --json is an unrecognized
    # flag, so BOTH invocations hit the IDENTICAL Click "No such option: --json"
    # usage error regardless of --no-truncate — which would make the output
    # equality assertion below a VACUOUS pass (both sides fail identically,
    # proving nothing about the real no-op contract) unless exit_code==0 is
    # asserted here first, forcing a genuine RED today.
    assert result_plain.exit_code == 0, (
        f"AC-SF-24: --json alone must exit 0. Got {result_plain.exit_code}.\n{result_plain.output}"
    )
    assert result_no_truncate.exit_code == 0, (
        f"AC-SF-24: --json --no-truncate must exit 0. "
        f"Got {result_no_truncate.exit_code}.\n{result_no_truncate.output}"
    )
    assert result_plain.output == result_no_truncate.output, (
        "AC-SF-24: --no-truncate must be a no-op under --json.\n"
        f"--json alone:\n{result_plain.output}\n"
        f"--json --no-truncate:\n{result_no_truncate.output}"
    )


# ---------------------------------------------------------------------------
# AC-SF-25: row shape + null policy + machine-safe match_type
# ---------------------------------------------------------------------------

def test_ac_sf_25_json_row_has_exact_key_set() -> None:
    """AC-SF-25 (UPDATED — hybrid semantic search PR, AC-HY-9: 12-key set incl.
    the additive 'similarity'): Given one richly-populated result row from a
    LEXICAL search.
    When `partgraph search MAX232 --json` is invoked.
    Then each row in results has EXACTLY the 12 keys: mpn, mpn_norm,
    manufacturer, package, category, stock, is_basic, price_usd, match_type,
    similarity, datasheets, params (no more, no less — in particular no 'uid').

    CHANGED FROM PRE-HYBRID (documented, not silent): the pre-hybrid version
    pinned an 11-key set. The hybrid PR adds a 12th key, 'similarity' (JSON
    null on a non-semantic/lexical row — present-but-null per AC-HY-9's
    same-key-set-for-every-row contract; the envelope version stays 1 as it is
    an additive key). Mirrors the rewritten renderer-level AC-SF-25 in
    tests/unit/test_renderer.py.
    """
    mock_txn = _make_mock_txn([_make_json_search_response()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-25: --json search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    envelope = json.loads(result.output)
    assert envelope["results"], "Expected at least one result row."
    row = envelope["results"][0]
    expected_keys = {
        "mpn", "mpn_norm", "manufacturer", "package", "category", "stock",
        "is_basic", "price_usd", "match_type", "similarity", "datasheets", "params",
    }
    assert set(row) == expected_keys, (
        f"AC-SF-25/AC-HY-9: row must have exactly these 12 keys: "
        f"{sorted(expected_keys)}. Got: {sorted(row)}"
    )
    # A lexical (non-semantic) row's similarity is JSON null (present-but-null).
    assert row["similarity"] is None, (
        f"AC-HY-9: a lexical row's 'similarity' must be JSON null. "
        f"Got: {row['similarity']!r}"
    )


def test_ac_sf_25_json_row_values_match_source_data() -> None:
    """AC-SF-25: Given the fixture in _make_json_search_response().
    When `partgraph search MAX232 --json` is invoked.
    Then the row's scalar values match the source DQL data exactly.
    """
    mock_txn = _make_mock_txn([_make_json_search_response()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-25: --json search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    row = json.loads(result.output)["results"][0]
    assert row["mpn"] == "MAX232CPE"
    assert row["mpn_norm"] == "MAX232CPE"
    assert row["manufacturer"] == "Texas Instruments"
    assert row["package"] == "PDIP-16"
    assert row["category"] == "RS232 ICs"
    assert row["stock"] == 250
    assert row["is_basic"] is True
    assert row["price_usd"] == pytest.approx(0.4123)
    assert row["match_type"] == "exact"
    assert row["datasheets"] == [
        "https://www.ti.com/lit/ds/symlink/max232.pdf",
        "https://example.com/alt-max232.pdf",
    ]
    assert row["params"] == {"voltage_max": pytest.approx(5.5)}


def test_ac_sf_25_json_row_null_policy_for_absent_scalars() -> None:
    """AC-SF-25: Given a minimal row with NO manufacturer/package/category/
    stock/is_basic/price_usd (all absent on the source node).
    When `partgraph search MAX232 --json` is invoked.
    Then the 7 scalar fields are present but JSON null; mpn_norm is
    non-null; datasheets == []; params == {}.
    """
    sparse_response = {
        "exact": [{"uid": "0xJS2", "mpn_norm": "SPARSE232"}],
        "trig": [],
        "fts": [],
    }
    mock_txn = _make_mock_txn([sparse_response])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-25: --json search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    row = json.loads(result.output)["results"][0]
    assert row["mpn_norm"] == "SPARSE232", "mpn_norm must always be non-null."
    for scalar in (
        "mpn", "manufacturer", "package", "category", "stock", "is_basic", "price_usd",
    ):
        assert scalar in row and row[scalar] is None, (
            f"AC-SF-25: absent scalar {scalar!r} must be present and JSON "
            f"null. Got: {row.get(scalar, '<MISSING KEY>')!r}"
        )
    assert row["datasheets"] == [], "AC-SF-25: datasheets must be [] when none."
    assert row["params"] == {}, (
        "AC-SF-25: params must be {} when no promoted predicate is present."
    )


def test_ac_sf_25_json_row_match_type_is_machine_safe_not_bracketed_label() -> None:
    """AC-SF-25: Given a result row from the 'trig' tier.
    When `partgraph search MAX232 --json` is invoked.
    Then match_type == "trigram" (machine-safe), never the human "Match"
    label and never containing brackets like "[Semantic]".
    """
    response = {
        "exact": [],
        "trig": [{"uid": "0xJS3", "mpn_norm": "TRIGPART232", "mpn": "TRIGPART232"}],
        "fts": [],
    }
    mock_txn = _make_mock_txn([response])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-25: --json search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    row = json.loads(result.output)["results"][0]
    assert row["match_type"] == "trigram", (
        f"AC-SF-25: trig-tier row must report match_type='trigram'. "
        f"Got: {row['match_type']!r}"
    )
    assert "[" not in row["match_type"] and "]" not in row["match_type"], (
        "AC-SF-25: match_type must never contain brackets (machine-safe, "
        "not the human _MATCH_LABELS)."
    )


def test_ac_sf_25_json_output_never_contains_uid_or_hex_address() -> None:
    """AC-SF-25: Given any populated result.
    When `partgraph search MAX232 --json` is invoked.
    Then the literal string "uid" and any Dgraph uid-shaped value ("0x...")
    appear NOWHERE in the raw stdout text.
    """
    mock_txn = _make_mock_txn([_make_json_search_response()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-25: --json search must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert "uid" not in result.output, (
        f"AC-SF-25: the string 'uid' must never appear in --json output. "
        f"Got:\n{result.output}"
    )
    assert not re.search(r"0x[0-9a-fA-F]+", result.output), (
        f"AC-SF-25: no '0x...' uid-shaped value may appear in --json output. "
        f"Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# AC-SF-26: empty/no-match under --json -> empty envelope, exit 0, never
# "No matches found"
# ---------------------------------------------------------------------------

def test_ac_sf_26_json_flag_empty_results_gives_empty_envelope_exit_0() -> None:
    """AC-SF-26: Given zero results (all blocks empty).
    When `partgraph search MAX232 --json` is invoked.
    Then stdout is EXACTLY {"version":1,"query":"MAX232","nearest_match":false,
    "count":0,"results":[]}, exit code 0, and "No matches found" NEVER
    appears.
    """
    mock_txn = _make_mock_txn([{"exact": [], "trig": [], "fts": []}])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-26: empty --json result must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    envelope = json.loads(result.output)
    assert envelope == {
        "version": 1,
        "query": "MAX232",
        "nearest_match": False,
        "count": 0,
        "results": [],
    }, f"AC-SF-26: empty envelope must match the exact documented shape. Got: {envelope}"
    assert "No matches found" not in result.output, (
        f"AC-SF-26: '--json' must NEVER print the human 'No matches found' "
        f"banner. Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# AC-SF-27: --semantic ... --json — empty block bypasses _NO_EMBEDDINGS_HINT;
# populated block -> match_type == "semantic", nearest_match == False
# ---------------------------------------------------------------------------

def test_ac_sf_27_semantic_json_empty_block_gives_empty_envelope_no_embed_hint() -> None:
    """AC-SF-27: Given a mocked encoder and a mocked client returning an
    EMPTY semantic block.
    When `partgraph search --semantic "rs232 transceiver" --json` is invoked.
    Then exit code is 0, stdout is the empty envelope (count 0, results []),
    and the human _NO_EMBEDDINGS_HINT text ("run `partgraph embed` first")
    is NEVER printed — the cli.py short-circuit that prints it under the
    non-JSON path must be bypassed entirely under --json.
    """
    empty_resp = {"exact": [], "trig": [], "fts": [], "semantic": []}
    mock_txn = _make_mock_txn([empty_resp])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-27: empty --semantic --json must exit 0. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    envelope = json.loads(result.output)
    assert envelope["count"] == 0 and envelope["results"] == [], (
        f"AC-SF-27: empty semantic --json must give an empty envelope. Got: {envelope}"
    )
    assert "embed" not in result.output.lower(), (
        f"AC-SF-27: the '_NO_EMBEDDINGS_HINT' embed-run hint must NEVER be "
        f"printed under --json. Got:\n{result.output}"
    )


def test_ac_sf_27_semantic_json_populated_rows_have_semantic_match_type() -> None:
    """AC-SF-27: Given a populated semantic result.
    When `partgraph search --semantic "rs232 transceiver" --json` is invoked.
    Then every row's match_type == "semantic" and envelope["nearest_match"]
    is False.
    """
    mock_txn = _make_mock_txn([_make_semantic_response_with_max232()])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver", "--json"])

    assert result.exit_code == 0, (
        f"AC-SF-27: populated --semantic --json must exit 0. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    envelope = json.loads(result.output)
    assert envelope["nearest_match"] is False, (
        f"AC-SF-27: semantic hit must never set nearest_match. "
        f"Got: {envelope['nearest_match']!r}"
    )
    assert envelope["results"], "Expected at least one semantic result row."
    for row in envelope["results"]:
        assert row["match_type"] == "semantic", (
            f"AC-SF-27: every semantic row must have match_type='semantic'. "
            f"Got: {row['match_type']!r}"
        )


# ---------------------------------------------------------------------------
# AC-SF-38: --json --sort price -> one valid envelope, ordered per AC-SF-21
# ---------------------------------------------------------------------------

def test_ac_sf_38_json_with_sort_price_orders_results_ascending_price_missing_last() -> None:
    """AC-SF-38: Given three parts: price_usd=0.50, price_usd=0.10, and a
    part with price_usd entirely absent.
    When `partgraph search MAX232 --json --sort price` is invoked.
    Then envelope["results"] is ordered ascending by price_usd with the
    missing-price row LAST (mirrors AC-SF-21's rank_results contract, now
    proven end-to-end through the CLI + JSON envelope).
    """
    response = {
        "exact": [
            {"uid": "0xM1", "mpn_norm": "HIGH", "price_usd": 0.50},
            {"uid": "0xM2", "mpn_norm": "LOW", "price_usd": 0.10},
            {"uid": "0xM3", "mpn_norm": "NOPRICE"},
        ],
        "trig": [],
        "fts": [],
    }
    mock_txn = _make_mock_txn([response])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json", "--sort", "price"])

    assert result.exit_code == 0, (
        f"AC-SF-38: --json --sort price must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    envelope = json.loads(result.output)
    mpn_norms = [row["mpn_norm"] for row in envelope["results"]]
    assert mpn_norms == ["LOW", "HIGH", "NOPRICE"], (
        f"AC-SF-38: --sort price must order ascending with missing price "
        f"last. Got: {mpn_norms}"
    )


# ===========================================================================
# AC-SF-40 (Gate 3 security FAIL-blocking, flagged by all three reviewers):
# invalid --sort value -> exit 1 (NEVER Click's usage-error exit code 2),
# fixed error text, no Dgraph client ever built.
#
# PIN: --sort MUST be implemented as a plain `str` option validated by OUR
# code (a `_validate_sort_flag`, mirroring `_validate_min_stock_flag` /
# `_validate_max_price_flag` / `_validate_package_flag`), NEVER as a Typer
# `Enum`/`Literal`/`click.Choice` — either of those would make Click itself
# reject a bad value with exit code 2 and Click's own generic usage-error
# text, breaking both the exit-1 AND the fixed-message contract pinned here.
# Mirrors the AC-SF-28 "never builds client" pattern exactly.
# ===========================================================================

def test_ac_sf_40_sort_bogus_value_exits_1_not_2_no_db_query() -> None:
    """AC-SF-40: Given `--sort bogus` (not one of relevance/stock/price).
    When invoked.
    Then exit code is 1 (NEVER Click's usage-error exit code 2), the fixed
    message '--sort must be one of: relevance, stock, price.' is printed,
    _build_dgraph_client is NEVER called, and no path/traceback leaks.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--sort", "bogus"])

    assert result.exit_code == 1, (
        f"AC-SF-40: bad --sort must exit 1 (never Click's exit-2). "
        f"Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--sort must be one of: relevance, stock, price." in result.output, (
        f"AC-SF-40: expected the fixed '--sort must be one of: relevance, "
        f"stock, price.' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"AC-SF-40: no path/traceback leak. Got: {result.output!r}"
    )


def test_ac_sf_40_sort_empty_string_exits_1_not_2_no_db_query() -> None:
    """AC-SF-40: Given `--sort ""` (empty string).
    When invoked.
    Then exit code 1, fixed error, no Dgraph client built, no path/traceback
    leak.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--sort", ""])

    assert result.exit_code == 1, (
        f"AC-SF-40: empty --sort must exit 1 (never Click's exit-2). "
        f"Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--sort must be one of: relevance, stock, price." in result.output, (
        f"AC-SF-40: expected the fixed '--sort must be one of: relevance, "
        f"stock, price.' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"AC-SF-40: no path/traceback leak. Got: {result.output!r}"
    )


def test_ac_sf_40_sort_uppercase_relevance_exits_1_not_2_no_db_query() -> None:
    """AC-SF-40: Given `--sort Relevance` (uppercase — case-sensitive
    mismatch of an otherwise-valid value; --sort is never case-folded).
    When invoked.
    Then exit code 1, fixed error, no Dgraph client built, no path/traceback
    leak.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--sort", "Relevance"])

    assert result.exit_code == 1, (
        f"AC-SF-40: uppercase --sort ('Relevance') must exit 1 (never "
        f"Click's exit-2). Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--sort must be one of: relevance, stock, price." in result.output, (
        f"AC-SF-40: expected the fixed '--sort must be one of: relevance, "
        f"stock, price.' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"AC-SF-40: no path/traceback leak. Got: {result.output!r}"
    )


def test_ac_sf_40_sort_numeric_string_exits_1_not_2_no_db_query() -> None:
    """AC-SF-40: Given `--sort 1` (a numeric-looking string, not a valid
    sort key).
    When invoked.
    Then exit code 1, fixed error, no Dgraph client built, no path/traceback
    leak.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--sort", "1"])

    assert result.exit_code == 1, (
        f"AC-SF-40: numeric-string --sort ('1') must exit 1 (never Click's "
        f"exit-2). Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    assert "--sort must be one of: relevance, stock, price." in result.output, (
        f"AC-SF-40: expected the fixed '--sort must be one of: relevance, "
        f"stock, price.' error text. Got:\n{result.output}"
    )
    assert "/home/" not in result.output and "Traceback" not in result.output, (
        f"AC-SF-40: no path/traceback leak. Got: {result.output!r}"
    )


# ===========================================================================
# Gate 3 (Security MUST-2): --json x error paths.
#
# A machine consumer parsing stdout must NEVER receive a half-JSON blob or a
# raw Python traceback on an error path — every error (DB failure, bad
# structured filter, empty query) must produce NO JSON envelope on stdout,
# with the error handled the SAME way (fixed message, exit 1) as the
# non-JSON path.
# ===========================================================================

def test_gate3_json_db_query_exception_exit_1_no_envelope_no_traceback() -> None:
    """Gate 3 (Security MUST-2): Given a mock txn.query that raises
    RuntimeError (DB down) — mirrors the existing _DB_QUERY_ERROR tests.
    When `partgraph search MAX232 --json` is invoked.
    Then exit code is 1, stdout is NOT a valid JSON envelope (no stray '{'),
    no traceback, no path leak, and the fixed 'partgraph db up' hint is still
    present (same B1/E4 contract as the non-JSON path).
    """
    mock_txn = MagicMock()
    mock_txn.query.side_effect = RuntimeError("connection refused")
    mock_txn.discard.return_value = None
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--json"])

    assert result.exit_code == 1, (
        f"Gate3: --json with a DB exception must exit 1. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert "{" not in result.output, (
        f"Gate3: --json error output must NOT contain a stray JSON envelope "
        f"opening brace. Got:\n{result.output}"
    )
    with pytest.raises(ValueError):
        json.loads(result.output)
    assert "Traceback" not in result.output, (
        f"Gate3: no raw traceback may leak into --json error output. "
        f"Got:\n{result.output!r}"
    )
    assert "/home/" not in result.output, (
        f"Gate3: no filesystem path may leak into --json error output. "
        f"Got:\n{result.output!r}"
    )
    assert "connection refused" not in result.output, (
        f"Gate3/B1: raw exception text must not leak. Got: {result.output!r}"
    )


def test_gate3_json_invalid_package_exit_1_no_db_client_no_envelope() -> None:
    """Gate 3 (Security MUST-2): Given `--json` combined with an invalid
    --package value ("bad value" — contains a space, fails the package
    charset).
    When invoked.
    Then exit code 1, _build_dgraph_client is NEVER called, and stdout is
    NOT a valid JSON envelope.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--json", "--package", "bad value"])

    assert result.exit_code == 1, (
        f"Gate3: --json + invalid --package must exit 1. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    with pytest.raises(ValueError):
        json.loads(result.output)


def test_gate3_json_invalid_min_stock_exit_1_no_db_client_no_envelope() -> None:
    """Gate 3 (Security MUST-2): Given `--json` combined with an invalid
    --min-stock value (-1, negative).
    When invoked.
    Then exit code 1, _build_dgraph_client is NEVER called, and stdout is
    NOT a valid JSON envelope.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--json", "--min-stock", "-1"])

    assert result.exit_code == 1, (
        f"Gate3: --json + invalid --min-stock must exit 1. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    with pytest.raises(ValueError):
        json.loads(result.output)


def test_gate3_json_empty_query_exit_1_no_envelope() -> None:
    """Gate 3 (Security MUST-2): Given `--json` combined with an empty query
    "".
    When invoked.
    Then exit code 1, the fixed 'empty' error text is present, and stdout is
    NOT a valid JSON envelope.
    """
    mock_client = _make_mock_client()

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "", "--json"])

    assert result.exit_code == 1, (
        f"Gate3: --json + empty query must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert "empty" in result.output.lower(), (
        f"Gate3: expected the fixed empty-query error text. Got:\n{result.output}"
    )
    with pytest.raises(ValueError):
        json.loads(result.output)


# ===========================================================================
# Gate 3 (UI/UX MUST-2/3): --sort / --json help-text content.
# ===========================================================================

def test_gate3_help_sort_mentions_all_three_values() -> None:
    """Gate 3 (UI/UX MUST-2): Given `partgraph search --help`.
    When invoked.
    Then the output documents --sort AND mentions all three valid values:
    relevance, stock, price.
    """
    result = _invoke(["search", "--help"])
    assert "--sort" in result.output, (
        f"Gate3: search --help must document --sort. Got:\n{result.output}"
    )
    for value in ("relevance", "stock", "price"):
        assert value in result.output, (
            f"Gate3: search --help must mention the --sort value {value!r}. "
            f"Got:\n{result.output}"
        )


def test_gate3_help_sort_mentions_default_relevance() -> None:
    """Gate 3 (UI/UX MUST-3, 'ideally'): Given `partgraph search --help`.
    When invoked.
    Then the output documents that the DEFAULT --sort value is 'relevance'
    (Typer's own "[default: ...]" annotation, mirroring how --limit already
    shows "[default: 20]" in this repo's help output today).
    """
    result = _invoke(["search", "--help"])
    assert "default" in result.output.lower() and "relevance" in result.output, (
        f"Gate3: search --help should document 'relevance' as the --sort "
        f"default. Got:\n{result.output}"
    )


def test_gate3_help_json_mentions_json_machine_readable() -> None:
    """Gate 3 (UI/UX MUST-2): Given `partgraph search --help`.
    When invoked.
    Then the output documents --json AND the substring "JSON" appears
    (machine-readable output contract).
    """
    result = _invoke(["search", "--help"])
    assert "--json" in result.output, (
        f"Gate3: search --help must document --json. Got:\n{result.output}"
    )
    assert "JSON" in result.output, (
        f"Gate3: search --help must mention 'JSON' (machine-readable "
        f"output). Got:\n{result.output}"
    )


# ===========================================================================
# Gate 3 (ADR-0016 Option B, OPTIONAL guard): the human table stays
# UNCHANGED — price/category surface ONLY via --json, never as a new column
# in the Rich table.
# ===========================================================================

def test_gate3_human_table_never_shows_price_value_option_b() -> None:
    """Gate 3 / ADR-0016 Option B (OPTIONAL — likely already implied by the
    existing render tests; added as an explicit regression guard): Given a
    search result whose underlying part carries price_usd=1.2345.
    When `partgraph search MAX232` (the human, NON --json table) is invoked.
    Then the price value does NOT appear anywhere in the rendered output —
    ADR-0016 decided price/category surface ONLY via --json; the human table
    stays UNCHANGED (no new price column).

    NOTE: this PASSES TODAY already (pass-by-design regression guard, not a
    RED test) — the current renderer's Rich table never reads price_usd at
    all (confirmed: partgraph.query.renderer._PARAM_DISPLAY has no price_usd
    entry). Flagged honestly as such.
    """
    response = {
        "exact": [
            {
                "uid": "0xG3P1",
                "mpn": "MAX232CPE",
                "mpn_norm": "MAX232CPE",
                "stock": 250,
                "is_basic": True,
                "price_usd": 1.2345,
                "made_by": [{"name": "Texas Instruments"}],
                "in_package": [{"name": "PDIP-16"}],
                "datasheet": [{"url": "https://example.com/ds.pdf"}],
            }
        ],
        "trig": [],
        "fts": [],
    }
    mock_txn = _make_mock_txn([response])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232"])

    assert "1.2345" not in result.output and "1.23" not in result.output, (
        f"Gate3/ADR-0016 Option B: the human table must NOT show a price "
        f"value. Got:\n{result.output}"
    )


# ===========================================================================
# AC-HY: hybrid semantic search robustness (Gate-1 ratified contract)
#
# cli.py's --semantic path (_run_semantic_search):
#   - computes candidate_k = min(max(limit * 20, 200), 1500) and passes it
#     (not the raw --limit) as build_semantic_dql's k argument (AC-HY-1);
#   - threads query_vector= and result_limit= into rank_results (see
#     tests/unit/test_ranker.py AC-HY-6..12);
#   - on an EMPTY human-path result, issues ONE probe
#     `{ probe(func: has(embedding), first: 1) { uid } }` on the same
#     client: probe>=1 rows -> a "starvation" message (loosen filters/raise
#     --limit; NEVER "partgraph embed" — AC-HY-13); probe==0 rows -> the
#     existing embed-run hint (AC-HY-14, and the AC-CE-2 rewrite above);
#     the probe raising falls back to the embed-run hint (AC-HY-14);
#   - a NON-EMPTY result NEVER issues the probe (AC-HY-15); --json NEVER
#     issues the probe and NEVER prints either hint, even when empty
#     (AC-HY-15, preserving AC-SF-27's empty-envelope contract);
#   - --help documents that --sort's relevance == cosine similarity for a
#     semantic search, and that --limit is internally oversampled and still
#     capped at 200 results (AC-HY-17).
# ===========================================================================

# ---------------------------------------------------------------------------
# AC-HY-1: candidate_k = min(max(limit * 20, 200), 1500), not the raw limit.
# ---------------------------------------------------------------------------

def test_ac_hy_1_semantic_candidate_k_is_oversampled_from_limit() -> None:
    """AC-HY-1: Given --semantic and several --limit values (5, 20, 50, 200,
    99999), with the actual Dgraph query captured.
    When `partgraph search --semantic "rs232 transceiver" --limit L` is
    invoked for each L.
    Then the k argument baked into similar_to(embedding, k, ...) is the
    OVERSAMPLED candidate_k = min(max(L * 20, 200), 1500), NOT the raw
    --limit value:
      L=5     -> k=200    (floor)
      L=20    -> k=400
      L=50    -> k=1000
      L=200   -> k=1500   (cap)
      L=99999 -> k=1500   (cap)
    """
    cases = {5: 200, 20: 400, 50: 1000, 200: 1500, 99999: 1500}
    for limit_value, expected_k in cases.items():
        mock_txn, captured = _make_capturing_txn(_make_semantic_response_with_max232())
        mock_client = _make_mock_client(mock_txn)

        with _patch_dgraph(mock_client), _patch_get_encoder():
            result = _invoke(
                ["search", "--semantic", "rs232 transceiver", "--limit", str(limit_value)]
            )

        assert result.exit_code == 0, (
            f"AC-HY-1: semantic search with --limit {limit_value} must exit "
            f"0. Got {result.exit_code}.\n{result.output}"
        )
        assert captured, f"AC-HY-1: expected a Dgraph query for --limit {limit_value}."
        dql, _variables = captured[0]
        k_matches = re.findall(r"similar_to\([^,]+,\s*(\d+)", dql)
        assert k_matches, f"AC-HY-1: expected similar_to(embedding, k, ...) in DQL:\n{dql}"
        assert int(k_matches[0]) == expected_k, (
            f"AC-HY-1: --limit {limit_value} must oversample to candidate_k="
            f"{expected_k} (min(max(limit*20,200),1500)). "
            f"Got k={k_matches[0]} in DQL:\n{dql}"
        )


# ---------------------------------------------------------------------------
# AC-HY-13: probe finds >=1 embedded part -> starvation message, NEVER
# "partgraph embed".
# ---------------------------------------------------------------------------

def test_ac_hy_13_probe_finds_embeddings_prints_starvation_message_not_embed_hint() -> None:
    """AC-HY-13: Given mocked encoder and mocked client where the semantic
    search itself returns ZERO rows, but the follow-up has(embedding) probe
    DOES find at least one embedded part (the index is populated; the empty
    result is caused by an over-narrow filter/limit combination, not a
    missing embed run).
    When `partgraph search --semantic "rs232 transceiver" --category
    "NoSuchCategory"` is invoked (human/non-JSON path).
    Then:
    - Exit code is 0.
    - Output advises loosening filters AND raising --limit (a "starvation"
      message, distinct from the embed-run hint).
    - Output NEVER contains the substring "partgraph embed" anywhere.
    """
    empty_resp = {"exact": [], "trig": [], "fts": [], "semantic": []}
    probe_found = {"probe": [{"uid": "0xPROBE1"}]}
    mock_txn = _make_mock_txn([empty_resp, probe_found])
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(
            ["search", "--semantic", "rs232 transceiver", "--category", "NoSuchCategory"]
        )

    assert result.exit_code == 0, (
        f"AC-HY-13: empty semantic result with a populated index must "
        f"still exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert "partgraph embed" not in result.output, (
        f"AC-HY-13: a populated embedding index (probe found >=1) must "
        f"NEVER advise re-running 'partgraph embed'. Got:\n{result.output}"
    )
    lowered = result.output.lower()
    assert "loosen" in lowered and "filter" in lowered, (
        f"AC-HY-13: expected the starvation message to advise loosening "
        f"filters. Got:\n{result.output}"
    )
    assert "limit" in lowered, (
        f"AC-HY-13: expected the starvation message to mention raising "
        f"--limit. Got:\n{result.output}"
    )
    # Security F5 (Gate-3 MUST-fix): the starvation hint is PATH-FREE — no
    # filesystem path (e.g. a leaked internal "/home/..." path) may ever appear.
    assert "/home/" not in result.output, (
        f"Security F5: the starvation hint must be path-free (no '/home/' "
        f"leak). Got:\n{result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-HY-14: probe DQL shape (has(embedding), first: 1); probe raising falls
# back to the embed hint.
# ---------------------------------------------------------------------------

def test_ac_hy_14_probe_dql_has_embedding_first_1_when_result_empty() -> None:
    """AC-HY-14: Given an empty semantic search result (triggering the
    probe) and a spy capturing every DQL sent to Dgraph.
    When `partgraph search --semantic "rs232 transceiver"` is invoked.
    Then a SECOND query is sent whose DQL text contains 'has(embedding)' and
    'first: 1' (the probe's exact contract:
    `{ probe(func: has(embedding), first: 1) { uid } }`).
    """
    captured: list[tuple[str, dict]] = []
    responses = [
        {"exact": [], "trig": [], "fts": [], "semantic": []},
        {"probe": []},
    ]

    def _spy_query(dql: str, variables: dict | None = None, *args, **kwargs):
        captured.append((dql, variables or {}))
        idx = min(len(captured) - 1, len(responses) - 1)
        resp = MagicMock()
        resp.json = json.dumps(responses[idx]).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _spy_query
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver"])

    assert result.exit_code == 0, (
        f"AC-HY-14: must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert len(captured) == 2, (
        f"AC-HY-14: expected exactly 2 Dgraph queries (search + probe) for "
        f"an empty result. Got {len(captured)}: {[c[0] for c in captured]}"
    )
    probe_dql, _probe_vars = captured[1]
    assert "has(embedding)" in probe_dql, (
        f"AC-HY-14: probe DQL must use has(embedding). Got:\n{probe_dql}"
    )
    assert re.search(r"first\s*:\s*1\b", probe_dql), (
        f"AC-HY-14: probe DQL must request first: 1. Got:\n{probe_dql}"
    )


def test_ac_hy_14_probe_raises_exception_falls_back_to_embed_hint() -> None:
    """AC-HY-14: Given an empty semantic search result, where the FOLLOW-UP
    probe query itself raises (e.g. a transient Dgraph error on the second
    round-trip).
    When `partgraph search --semantic "rs232 transceiver"` is invoked.
    Then the CLI falls back to the embed-run hint (never crashes, never
    leaks the raw exception) and still exits 0.
    """
    call_counter = [0]

    def _flaky_query(dql: str, variables: dict | None = None, *args, **kwargs):
        call_counter[0] += 1
        if call_counter[0] == 1:
            resp = MagicMock()
            resp.json = json.dumps(
                {"exact": [], "trig": [], "fts": [], "semantic": []}
            ).encode()
            return resp
        raise RuntimeError("transient probe failure")

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _flaky_query
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver"])

    assert result.exit_code == 0, (
        f"AC-HY-14: a probe failure must still exit 0 (fallback to the "
        f"embed hint, never crash). Got {result.exit_code}.\n{result.output}"
    )
    assert "embed" in result.output.lower(), (
        f"AC-HY-14: probe failure must fall back to the embed-run hint. "
        f"Got:\n{result.output}"
    )
    assert "transient probe failure" not in result.output, (
        f"AC-HY-14: the raw probe exception must never leak. "
        f"Got:\n{result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-HY-15: a non-empty result never issues the probe; --json never issues
# the probe and never prints either hint, even when empty.
# ---------------------------------------------------------------------------

def test_ac_hy_15_non_empty_semantic_result_never_issues_probe() -> None:
    """AC-HY-15: Given a semantic search that returns a NON-EMPTY result.
    When `partgraph search --semantic "rs232 transceiver"` is invoked.
    Then exactly ONE Dgraph query is sent (the search itself) — the
    has(embedding) probe must NEVER be issued when there is already at
    least one row to show.
    """
    mock_txn, captured = _make_capturing_txn(_make_semantic_response_with_max232())
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver"])

    assert result.exit_code == 0, (
        f"AC-HY-15: must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert len(captured) == 1, (
        f"AC-HY-15: a non-empty semantic result must issue EXACTLY ONE "
        f"Dgraph query (no probe). Got {len(captured)}: {[c[0] for c in captured]}"
    )
    assert "probe(" not in captured[0][0], (
        f"AC-HY-15: the single query sent must not itself be the probe. "
        f"Got:\n{captured[0][0]}"
    )


def test_ac_hy_15_json_empty_semantic_result_no_probe_no_hint_preserves_ac_sf_27() -> None:
    """AC-HY-15: Given an empty semantic result under --json.
    When `partgraph search --semantic "rs232 transceiver" --json` is
    invoked.
    Then:
    - Exactly ONE Dgraph query is sent (the search itself) — the probe must
      NEVER be issued under --json, regardless of emptiness (AC-SF-27's
      contract: --json never prints the human embed hint, and now also
      never pays the extra probe round-trip).
    - stdout is still the empty envelope (count 0, results []), exit 0.
    - No embed-hint / starvation text appears anywhere in stdout.
    """
    mock_txn, captured = _make_capturing_txn(
        {"exact": [], "trig": [], "fts": [], "semantic": []}
    )
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver", "--json"])

    assert result.exit_code == 0, (
        f"AC-HY-15: empty --semantic --json must exit 0. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert len(captured) == 1, (
        f"AC-HY-15: --json must NEVER issue the has(embedding) probe (no "
        f"probe round-trip under the machine-readable path). "
        f"Got {len(captured)} queries: {[c[0] for c in captured]}"
    )
    envelope = json.loads(result.output)
    assert envelope["count"] == 0 and envelope["results"] == [], (
        f"AC-HY-15: empty semantic --json must give an empty envelope. "
        f"Got: {envelope}"
    )
    assert "embed" not in result.output.lower(), (
        f"AC-HY-15: no embed-run hint may appear under --json. "
        f"Got:\n{result.output}"
    )
    assert "loosen" not in result.output.lower(), (
        f"AC-HY-15: no starvation message may appear under --json either. "
        f"Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# AC-HY-17: help text — --sort notes semantic relevance=cosine; --limit
# notes internal oversampling, capped at 200.
# ---------------------------------------------------------------------------

def test_ac_hy_17_help_sort_mentions_semantic_relevance_is_cosine() -> None:
    """AC-HY-17: Given `partgraph search --help`.
    When invoked.
    Then the help text notes that, for a semantic search, 'relevance'
    ordering is by cosine (embedding) similarity.
    """
    result = _invoke(["search", "--help"])
    assert "--sort" in result.output
    lowered = result.output.lower()
    assert "cosine" in lowered, (
        f"AC-HY-17: search --help must mention 'cosine' (semantic relevance "
        f"= cosine similarity). Got:\n{result.output}"
    )


def test_ac_hy_17_help_limit_mentions_oversampling_and_200_cap() -> None:
    """AC-HY-17: Given `partgraph search --help`.
    When invoked.
    Then the help text for --limit notes the internal candidate
    oversampling and that results stay capped at 200.
    """
    result = _invoke(["search", "--help"])
    assert "--limit" in result.output
    lowered = result.output.lower()
    assert "oversampl" in lowered, (
        f"AC-HY-17: search --help must mention internal oversampling for "
        f"--limit. Got:\n{result.output}"
    )
    assert "200" in result.output, (
        f"AC-HY-17: search --help must mention the 200 result cap. "
        f"Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# AC-HY-4 (architecture MUST-fix): a filterless --semantic --limit 5 where the
# server-side has(datasheet) @filter prunes the candidate pool still returns
# EXACTLY 5 datasheet-backed rows (top-5 by cosine), refilled from the
# oversampled 200-candidate pool — never a silent underfill.
# ---------------------------------------------------------------------------

def test_ac_hy_4_filterless_semantic_limit_5_refills_to_exactly_5_datasheet_backed_rows() -> None:
    """AC-HY-4: Given a filterless semantic search with --limit 5, where the
    server-side has(datasheet) @filter prunes the candidate pool (only
    datasheet-backed parts come back) but the CLI oversamples the pool
    (candidate_k = 200 for --limit 5) so more than 5 datasheet-backed
    candidates remain.
    When `partgraph search --semantic "rs232 transceiver" --limit 5 --json` is
    invoked (mocked encoder + mocked client returning 8 datasheet-backed
    semantic rows with distinct, strictly-decreasing cosine similarities).
    Then:
    - The DQL asks Dgraph for the OVERSAMPLED candidate_k=200 (min(max(5*20,
      200),1500)) — the mechanism that lets the top-5 refill from a pruned pool.
    - EXACTLY 5 rows are returned (no silent underfill below --limit just
      because has(datasheet) pruned some candidates).
    - The 5 survivors are the TOP 5 by cosine (R0..R4), chosen AFTER the
      client-side cosine re-rank + truncation.
    """
    query_vector = [1.0] + [0.0] * 383  # 384-dim, aligned with axis 0.

    def _enc(texts):
        return [query_vector for _ in texts]

    # 8 datasheet-backed semantic rows; embedding [1.0, scale]+... gives
    # cosine = 1/sqrt(1+scale**2), strictly decreasing as scale (idx) grows.
    semantic_rows = [
        {
            "uid": f"0xR{idx}",
            "mpn": f"R{idx}",
            "mpn_norm": f"R{idx}",
            "stock": 10,
            "is_basic": False,
            "made_by": [{"name": "Texas Instruments"}],
            "in_package": [{"name": "SOIC-16"}],
            "datasheet": [{"url": f"https://example.com/r{idx}.pdf"}],
            "embedding": [1.0, float(idx)] + [0.0] * 382,
        }
        for idx in range(8)
    ]
    response = {"exact": [], "trig": [], "fts": [], "semantic": semantic_rows}
    mock_txn, captured = _make_capturing_txn(response)
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder(_enc):
        result = _invoke(
            ["search", "--semantic", "rs232 transceiver", "--limit", "5", "--json"]
        )

    assert result.exit_code == 0, (
        f"AC-HY-4: filterless --semantic --limit 5 must exit 0. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "AC-HY-4: expected a semantic DQL query."
    dql = captured[0][0]
    k_matches = re.findall(r"similar_to\([^,]+,\s*(\d+)", dql)
    assert k_matches and int(k_matches[0]) == 200, (
        f"AC-HY-4: --limit 5 must oversample the candidate pool to k=200 (the "
        f"refill source). Got k={k_matches[0] if k_matches else '<none>'} "
        f"in:\n{dql}"
    )
    # Exactly one query (non-empty result -> no probe).
    assert len(captured) == 1, (
        f"AC-HY-4: a non-empty result must issue exactly one query (no probe). "
        f"Got {len(captured)}."
    )
    envelope = json.loads(result.output)
    assert envelope["count"] == 5 and len(envelope["results"]) == 5, (
        f"AC-HY-4: exactly 5 datasheet-backed rows must survive (no silent "
        f"underfill). Got count={envelope['count']}, "
        f"{len(envelope['results'])} rows."
    )
    mpns = [row["mpn"] for row in envelope["results"]]
    assert mpns == ["R0", "R1", "R2", "R3", "R4"], (
        f"AC-HY-4: the 5 survivors must be the TOP 5 by cosine (R0..R4), "
        f"refilled from the oversampled candidate pool. Got: {mpns}"
    )


# ---------------------------------------------------------------------------
# Security F4 (Gate-3 MUST-fix): _build_dgraph_client is called exactly ONCE
# across the search+probe round-trip — the probe reuses the search's client.
# ---------------------------------------------------------------------------

def test_security_f4_build_dgraph_client_called_exactly_once_across_search_and_probe() -> None:
    """Security F4: Given an EMPTY human-path semantic result that triggers the
    has(embedding) probe (the probe finds an embedded part).
    When `partgraph search --semantic "rs232 transceiver"` is invoked.
    Then _build_dgraph_client is called EXACTLY ONCE across the whole
    search+probe round-trip — the probe REUSES the search's client and must
    never open a second Dgraph connection.
    """
    import partgraph.cli as cli_mod

    empty_resp = {"exact": [], "trig": [], "fts": [], "semantic": []}
    probe_found = {"probe": [{"uid": "0xPROBE1"}]}
    mock_txn = _make_mock_txn([empty_resp, probe_found])
    mock_client = _make_mock_client(mock_txn)

    build_spy = MagicMock(return_value=(mock_client, MagicMock()))
    with patch.object(cli_mod, "_build_dgraph_client", build_spy), _patch_get_encoder():
        result = _invoke(["search", "--semantic", "rs232 transceiver"])

    assert result.exit_code == 0, (
        f"Security F4: empty semantic + probe must exit 0. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert build_spy.call_count == 1, (
        f"Security F4: _build_dgraph_client must be called EXACTLY ONCE across "
        f"the search+probe round-trip (the probe reuses the search client). "
        f"Got {build_spy.call_count} call(s)."
    )


# ===========================================================================
# AC-LM: `search --limit` non-positive rejection (issue: the CLI's five
# --limit commands disagreed — four (ingest jlcparts, embed, refresh-links,
# refresh) route through cli.py's `_validate_limit` and reject a non-positive
# value with exit 1 and the fixed "--limit must be a positive integer."
# message; `search` alone declared `--limit` as a Typer `int`, so Click
# coerced it before any repo code saw it and `_validate_limit` never ran.
# `search --limit 0`/`--limit -5` therefore fell straight through to
# dql_builder's `first = max(1, min(int(limit), MAX_RESULT_LIMIT))` and
# silently became `--limit 1` — exit 0, a full result set, no error.
#
# ADR-0024 (breaking-change record; not written by this test suite) must
# record: `search --limit 0` / `search --limit <negative>` changes from
# "exit 0, silently clamped to 1" to "exit 1, rejected" — anyone scripting
# `search --limit 0` today for its old (accidental) behaviour will start
# seeing a failure.
#
# Explicitly OUT of scope (the tests below pin the boundary so it cannot
# silently move):
#   - The upper cap (`--limit 5000` -> still clamped to MAX_RESULT_LIMIT=200,
#     never an error). search's own --help text says results "stay capped at
#     200 regardless of --limit" — that is a documented feature, not a bug.
#   - `--limit abc` (non-integer text). CONFIRMED by direct invocation
#     (mocked _build_dgraph_client + _autostart_database, PARTGRAPH_AUTOSTART
#     unset -> the module-level conftest autouse fixture forces "0"): Click's
#     own `int` coercion already rejects this BEFORE the command body (and
#     therefore before _validate_limit could ever run) with its own exit
#     code 2 and "Invalid value for '--limit': 'abc' is not a valid integer."
#     message — an already-acceptable error. Only the sign is ours to fix.
# ===========================================================================

@pytest.mark.parametrize("bad_limit", ["0", "-1", "-5", "-999"])
def test_ac_lm_1_search_limit_non_positive_exits_1_reuses_validate_limit_message(
    bad_limit: str,
) -> None:
    """AC-LM-1: Given `partgraph search MAX232 --limit <n>` where n <= 0
    (0, -1, -5, -999).
    When invoked.

    OLD (pre-fix) behaviour: exit 0. `--limit` was typed `int` in the Typer
    option, so Click coerced the string to an int before any validation ran;
    the value then reached dql_builder's `max(1, min(int(limit),
    MAX_RESULT_LIMIT))` and was silently rewritten to 1 — a full query still
    ran and results (or "No matches found") were printed.

    NEW (post-fix, ADR-0024) behaviour: exit code is 1, and the output
    contains the EXACT existing "--limit must be a positive integer."
    message — the same fixed, path-free string `_validate_limit` already
    emits for `ingest jlcparts` / `embed` / `refresh-links` / `refresh`
    (the message `_validate_limit` itself prints). No Dgraph client is ever built (parity with
    `_connect_dgraph`'s own contract: "a bad --limit must be reported
    without starting anything").

    NOTE on what this test does NOT prove: an identical, byte-for-byte
    duplicate literal (`if limit <= 0: _err_console.print(...); raise
    typer.Exit(code=1)`) copy-pasted straight into `search` would satisfy
    every assertion below just as well as real delegation to
    `_validate_limit` would — this test only inspects rendered output text,
    so it cannot tell reuse from copy-paste. See
    test_ac_lm_8_search_limit_rejection_delegates_to_validate_limit_not_a_duplicate,
    which spies on `_validate_limit` itself and is the one that can.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--limit", bad_limit])

    assert result.exit_code == 1, (
        f"AC-LM-1: --limit {bad_limit!r} must exit 1 (was exit 0, silently "
        f"clamped to 1, before this fix). Got {result.exit_code}.\n{result.output}"
    )
    assert "--limit must be a positive integer." in result.output, (
        f"AC-LM-1: must reuse the EXACT _validate_limit message (parity with "
        f"ingest jlcparts/embed/refresh-links/refresh). Got:\n{result.output!r}"
    )
    assert not captured, (
        f"AC-LM-1: no Dgraph query may be sent for a rejected --limit. "
        f"Got {len(captured)} captured call(s)."
    )
    assert "/home/" not in result.output, (
        f"AC-LM-1/Security-baseline: no filesystem path leak. Got: {result.output!r}"
    )
    assert "Traceback" not in result.output, (
        f"AC-LM-1/Security-baseline: no raw traceback. Got: {result.output!r}"
    )


def test_ac_lm_2_search_limit_one_boundary_still_succeeds() -> None:
    """AC-LM-2 (edge case): Given `partgraph search MAX232 --limit 1` — the
    smallest value _validate_limit accepts as positive.
    When invoked.
    Then exit code is 0 (UNCHANGED by this fix: 1 was never rejected, before
    or after), and the captured DQL's `first:` clause is 1 for every block —
    the boundary sits exactly at "1 is accepted, 0 is not", matching
    `_validate_limit`'s own `value <= 0` check exactly, with no
    off-by-one drift introduced by threading validation into `search`.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--limit", "1"])

    assert result.exit_code == 0, (
        f"AC-LM-2: --limit 1 must exit 0 (the smallest legal positive value). "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "AC-LM-2: expected at least one Dgraph query to be sent."
    dql, _variables = captured[0]
    first_values = re.findall(r"first\s*:\s*(\d+)", dql)
    assert first_values, f"AC-LM-2: expected at least one 'first: N' clause. Got:\n{dql}"
    for raw_val in first_values:
        assert int(raw_val) == 1, (
            f"AC-LM-2: --limit 1 must produce 'first: 1' in every block. "
            f"Got first: {raw_val} in:\n{dql}"
        )


def test_ac_lm_3_search_limit_5000_stays_silently_clamped_not_rejected() -> None:
    """AC-LM-3 (scope precision — the upper cap is UNCHANGED by this fix):
    Given `partgraph search MAX232 --limit 5000` (well above
    MAX_RESULT_LIMIT=200, but a positive integer).
    When invoked.
    Then exit code is 0 (NOT an error, both before and after this fix — the
    task is explicit that a documented cap must never become a rejection),
    and the captured DQL's `first:` clause stays <= 200 in every block
    (dql_builder's own `min(int(limit), MAX_RESULT_LIMIT)` clamp, unchanged).
    search's own --help text says "results stay capped at 200 regardless of
    --limit" — that promise must keep holding after ADR-0024 lands.
    """
    mock_txn, captured = _make_capturing_txn()
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client):
        result = _invoke(["search", "MAX232", "--limit", "5000"])

    assert result.exit_code == 0, (
        f"AC-LM-3: --limit 5000 must stay a silent clamp, NOT an error "
        f"(the upper cap is deliberate and documented). "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert captured, "AC-LM-3: expected at least one Dgraph query to be sent."
    dql, _variables = captured[0]
    first_values = re.findall(r"first\s*:\s*(\d+)", dql)
    assert first_values, f"AC-LM-3: expected at least one 'first: N' clause. Got:\n{dql}"
    for raw_val in first_values:
        assert int(raw_val) <= 200, (
            f"AC-LM-3: PIN MAX_RESULT_LIMIT=200 — effective cap in query is "
            f"{raw_val}, must stay <= 200. Query text:\n{dql}"
        )


def test_ac_lm_4_search_limit_non_integer_text_keeps_clicks_own_error_out_of_scope() -> None:
    """AC-LM-4 (scope boundary — CONFIRMED, not assumed, by direct
    invocation before writing this test: mocked _build_dgraph_client +
    _autostart_database, PARTGRAPH_AUTOSTART left at the conftest-forced
    "0"): Given `partgraph search MAX232 --limit abc` (non-integer text).
    When invoked.
    Then exit code is 2 (Click's OWN usage-error exit code — NOT 1, and NOT
    _validate_limit's exit code), the output contains Click's native
    "Invalid value for '--limit'" phrasing, and the output does NOT contain
    the reused "--limit must be a positive integer." message. `--limit`
    stays a Typer `int` option for non-integer text; Click's own coercion
    already produces an acceptable, clear error before any repo code runs,
    so only the sign/zero case (AC-LM-1) is this fix's job. This pin exists
    so a later refactor that reroutes ALL of --limit's validation through
    _validate_limit (changing the Typer type to `str`, as the other four
    commands do) cannot silently change this exit code or message without
    a test noticing.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--limit", "abc"])

    assert result.exit_code == 2, (
        f"AC-LM-4: --limit abc must stay Click's own usage-error exit code 2 "
        f"(out of this fix's scope). Got {result.exit_code}.\n{result.output}"
    )
    assert "Invalid value for '--limit'" in result.output, (
        f"AC-LM-4: expected Click's own native error text. Got:\n{result.output!r}"
    )
    assert "--limit must be a positive integer." not in result.output, (
        f"AC-LM-4: must NOT be rerouted through _validate_limit's message — "
        f"that would change an already-acceptable, out-of-scope error. "
        f"Got:\n{result.output!r}"
    )
    mock_build.assert_not_called()


def test_ac_lm_5_json_output_limit_zero_exits_1_no_envelope() -> None:
    """AC-LM-5 (contract test — API_AND_CONTRACT_RULES): Given `--json`
    combined with `--limit 0`.
    When invoked.

    OLD (pre-fix) behaviour: exit 0, a full JSON envelope (`{"count": ...,
    "results": [...]}`) printed for a single silently-clamped-to-1 result
    set.

    NEW (post-fix) behaviour: exit code is 1, `_build_dgraph_client` is
    NEVER called, and stdout is NOT a valid JSON value — matching this
    file's existing Gate-3 contract ("Errors still exit non-zero and print
    no JSON") already pinned for --package/--min-stock/empty-query in
    test_gate3_json_invalid_package_exit_1_no_db_client_no_envelope and
    test_gate3_json_invalid_min_stock_exit_1_no_db_client_no_envelope.
    """
    import partgraph.cli as cli_mod

    with patch.object(cli_mod, "_build_dgraph_client") as mock_build:
        result = _invoke(["search", "MAX232", "--json", "--limit", "0"])

    assert result.exit_code == 1, (
        f"AC-LM-5: --json + --limit 0 must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    mock_build.assert_not_called()
    with pytest.raises(ValueError):
        json.loads(result.output)


def test_ac_lm_6_semantic_search_shares_the_same_limit_validation() -> None:
    """AC-LM-6 (integration coverage — the two branches of `search` share ONE
    `--limit` Typer option, so validation must run BEFORE the `if semantic is
    not None:` split, exactly like every other AC-SF flag validator already
    does): Given `partgraph search --semantic "rs232 transceiver" --limit 0`.
    When invoked.

    OLD (pre-fix) behaviour: exit 0. The semantic path never validated
    --limit either; it flowed into `_run_semantic_search`'s own
    `candidate_k = min(max(limit * 20, 200), 1500)` oversampling formula,
    where `0 * 20 = 0` and `max(0, 200) = 200` — a full semantic search still
    ran on a 200-candidate pool.

    NEW (post-fix) behaviour: exit code is 1, the output contains the exact
    "--limit must be a positive integer." message, the embedding encoder is
    NEVER invoked, and no Dgraph query is ever sent — proving the fix does
    not just patch the lexical `search` path while leaving `--semantic`
    silently broken.
    """
    import partgraph.cli as cli_mod

    encoder_called = [False]

    def _counting_get_encoder():
        def _enc(texts):
            encoder_called[0] = True
            return [_FAKE_VECTOR for _ in texts]
        return _enc

    mock_txn, captured = _make_capturing_txn({"exact": [], "trig": [], "fts": [], "semantic": []})
    mock_client = _make_mock_client(mock_txn)

    with _patch_dgraph(mock_client), \
         patch.object(cli_mod, "get_encoder", _counting_get_encoder, create=True):
        result = _invoke(["search", "--semantic", "rs232 transceiver", "--limit", "0"])

    assert result.exit_code == 1, (
        f"AC-LM-6: --semantic ... --limit 0 must exit 1 (was exit 0, a full "
        f"semantic search on a 200-candidate pool, before this fix). "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert "--limit must be a positive integer." in result.output, (
        f"AC-LM-6: must reuse the exact _validate_limit message on the "
        f"semantic path too. Got:\n{result.output!r}"
    )
    assert not encoder_called[0], (
        "AC-LM-6: the encoder must NOT be invoked for a rejected --limit "
        "(it was invoked before this fix)."
    )
    assert not captured, (
        f"AC-LM-6: no Dgraph query may be sent for a rejected --limit. "
        f"Got {len(captured)} captured call(s)."
    )


# ---------------------------------------------------------------------------
# AC-LM-8 — closes a proxy gap: AC-LM-1..6 assert only rendered output text
# (message string, exit code), never that `_validate_limit` was actually
# CALLED. A hand-copied duplicate literal —
#
#     if limit <= 0:
#         _err_console.print("[red]Error:[/red] --limit must be a positive integer.")
#         raise typer.Exit(code=1)
#
# — placed directly in `search`, satisfies every AC-LM-1..6 assertion just
# as well as genuine delegation to `_validate_limit(str(limit))` does,
# because both paths render byte-identical text and the same exit code. Text
# assertions cannot distinguish "search reuses the shared validator" from
# "search re-implements a copy of it". This test can: it wraps
# `partgraph.cli._validate_limit` with a call-tracking spy and asserts the
# spy was actually invoked, with the user's --limit value coerced to the
# string `_validate_limit`'s own `str | None` signature expects.
#
# DEMONSTRATED (mutation-testing style, against two standalone SCRATCH
# copies of src/partgraph/cli.py loaded outside the repo — the tracked
# src/partgraph/cli.py itself was never edited by this check, deliberately,
# because another agent had live uncommitted work in progress on that exact
# file at the time; see the Test Engineer's session report for the scratch
# file paths and the exact `pytest` runs):
#   - Real delegation (`_validate_limit(str(limit))` in `search`, before the
#     --semantic branch split): AC-LM-1..6-style text assertions pass AND
#     this test's spy assertion passes (`_validate_limit` was called).
#   - Hand-copied duplicate literal (same `if limit <= 0: ...` block,
#     inlined instead of calling `_validate_limit`): AC-LM-1..6-style text
#     assertions STILL pass (byte-identical rendered text) but THIS test's
#     spy assertion fails — `_validate_limit` was never called.
# ---------------------------------------------------------------------------

def test_ac_lm_8_search_limit_rejection_delegates_to_validate_limit_not_a_duplicate() -> None:
    """AC-LM-8: Given `partgraph search MAX232 --limit 0`, with
    `partgraph.cli._validate_limit` wrapped by a call-tracking spy (`wraps=`
    the real function, so its own behaviour — the exit-1 side effect — still
    runs unchanged; only the call itself is observed).
    When invoked.
    Then:
    - `_validate_limit` is called at least once (proves DELEGATION, not a
      hand-copied duplicate literal that happens to emit the same text —
      see the block comment above this test, and AC-LM-1's docstring, which
      explicitly does NOT claim more than its own (text-only) assertions
      prove).
    - It is called with a STRING "0" (`_validate_limit`'s declared
      `str | None` signature; `search`'s Typer option stays `int`-typed per
      AC-LM-4, so the call site must coerce with `str(limit)` before
      delegating — passing the raw int would crash inside
      `_validate_limit` on `limit.strip()`).
    - exit code is still 1 and the shared message still appears (unchanged
      from AC-LM-1; this test adds a NEW assertion, it does not replace the
      existing one).
    """
    import partgraph.cli as cli_mod

    with patch.object(
        cli_mod, "_validate_limit", wraps=cli_mod._validate_limit
    ) as spy_validate_limit:
        result = _invoke(["search", "MAX232", "--limit", "0"])

    assert result.exit_code == 1, (
        f"AC-LM-8: --limit 0 must exit 1. Got {result.exit_code}.\n{result.output}"
    )
    assert "--limit must be a positive integer." in result.output, (
        f"AC-LM-8: expected the shared _validate_limit message. Got:\n{result.output!r}"
    )
    assert spy_validate_limit.called, (
        "AC-LM-8: _validate_limit must be CALLED for search's --limit too — "
        "a hand-copied duplicate literal that renders the same text would "
        "make every AC-LM-1..6 text assertion pass while never calling the "
        "shared validator. This is the assertion that tells them apart."
    )
    call_args, call_kwargs = spy_validate_limit.call_args
    called_value = call_args[0] if call_args else call_kwargs.get("limit")
    assert isinstance(called_value, str), (
        f"AC-LM-8: _validate_limit's own signature is `str | None` — search "
        f"must call it with the coerced STRING form (e.g. str(limit)), not "
        f"a raw int (that would crash inside _validate_limit on "
        f"limit.strip()). Got {called_value!r} ({type(called_value).__name__})."
    )
    assert int(called_value) == 0, (
        f"AC-LM-8: _validate_limit must be called with the user's actual "
        f"--limit value (0), not some other value. Got {called_value!r}."
    )
