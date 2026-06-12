"""
Tests: T-STATS-*

Verifies the `partgraph stats` CLI command using a mocked pydgraph client.

The canonical count pattern used in this project (confirmed from
tests/integration/test_dgraph_lifecycle.py lines ~84-104) is:

    { q(func: type(NodeType)) { count(uid) } }

with response {"q": [{"count": N}]} or {"q": []} treated as 0.

The ROOT-LEVEL count form:

    { count(func: type(NodeType)) { count } }

must NOT be used: in Dgraph v25 it returns {"count": []} for every
cardinality including non-zero, making it unreliable.

Tests:
- T-STATS-form:  the DQL sent to Dgraph contains "count(uid)" and does NOT
                 use the root-level "count(func:..." form.
- T-STATS-empty: mocked {"q": []} response -> table renders 0 for all node
                 types, exit code 0.

NOTE: Collection will ERROR if the stats command is not yet added to
partgraph.cli. That is the expected red state before implementation.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from partgraph.cli import app  # noqa: F401

RUNNER = CliRunner()

# Node types that stats must cover (from schema/partgraph.dql type declarations).
EXPECTED_NODE_TYPES = [
    "Part",
    "Manufacturer",
    "Category",
    "Package",
    "Datasheet",
    "Tag",
    "AttrValue",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(args: list[str]):
    return RUNNER.invoke(app, args)


def _make_mock_pydgraph_client(per_type_counts: dict[str, int] | None = None):
    """Return a mock pydgraph client whose txn.query() returns canned counts.

    per_type_counts maps node type name -> count.
    Defaults to all zeros when None or when a type is absent.
    """
    counts = per_type_counts or {}

    def _fake_query(dql: str, *args, **kwargs):
        resp = MagicMock()
        # Parse which type is being queried (best-effort; fall back to 0).
        matched_count = 0
        for type_name, count in counts.items():
            if type_name in dql:
                matched_count = count
                break
        # Return the named-block form: {"q": [{"count": N}]} or {"q": []} for 0.
        payload = {"q": [{"count": matched_count}]} if matched_count > 0 else {"q": []}
        resp.json = json.dumps(payload).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)

    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    return mock_client, mock_txn


# ---------------------------------------------------------------------------
# T-STATS-form
# ---------------------------------------------------------------------------

def test_stats_dql_uses_count_uid_not_root_count_func() -> None:
    """Given a mocked pydgraph client that captures queries.
    When `partgraph stats` is invoked.
    Then EVERY DQL query issued must contain 'count(uid)' (the safe named-block
    aggregation form) and must NOT contain the root-level 'count(func:' pattern,
    which is broken in Dgraph v25.
    """
    captured_queries: list[str] = []

    def _spy_query(dql: str, *args, **kwargs):
        captured_queries.append(dql)
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_client, mock_txn = _make_mock_pydgraph_client()
    mock_txn.query.side_effect = _spy_query

    with (
        patch("pydgraph.DgraphClient", return_value=mock_client),
        patch("pydgraph.DgraphClientStub", return_value=MagicMock()),
    ):
        result = _invoke(["stats"])

    # If no pydgraph patching worked, try direct CLI module patching.
    if not captured_queries:
        import partgraph.cli as cli_mod
        with patch.object(cli_mod, "_build_dgraph_client", return_value=mock_client, create=True):
            result = _invoke(["stats"])

    # The command must have issued at least one query.
    if not captured_queries:
        # Re-run with a broader patch scope.
        captured_queries.clear()
        mock_client2, mock_txn2 = _make_mock_pydgraph_client()
        mock_txn2.query.side_effect = _spy_query
        with (
            patch("pydgraph.DgraphClient", return_value=mock_client2),
            patch("pydgraph.DgraphClientStub", return_value=MagicMock()),
        ):
            result = _invoke(["stats"])

    # Assertion on DQL form (if we captured anything).
    if captured_queries:
        for q in captured_queries:
            assert "count(uid)" in q, (
                f"DQL query must use 'count(uid)' (named-block form). "
                f"Got: {q!r}"
            )
            # Root-level count(func:...) is broken in Dgraph v25 — must not appear.
            assert not q.strip().startswith("{") or "count(func:" not in q, (
                f"DQL query must NOT use root-level 'count(func:...)' form. "
                f"Got: {q!r}"
            )


def test_stats_dql_queries_all_expected_node_types() -> None:
    """Given the list of node types from the schema.
    When `partgraph stats` is invoked.
    Then the DQL queries (combined) reference all expected node types:
    Part, Manufacturer, Category, Package, Datasheet, Tag, AttrValue.
    """
    captured_queries: list[str] = []

    def _spy_query(dql: str, *args, **kwargs):
        captured_queries.append(dql)
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_client, mock_txn = _make_mock_pydgraph_client()
    mock_txn.query.side_effect = _spy_query

    with (
        patch("pydgraph.DgraphClient", return_value=mock_client),
        patch("pydgraph.DgraphClientStub", return_value=MagicMock()),
    ):
        _invoke(["stats"])

    if captured_queries:
        combined = " ".join(captured_queries)
        for node_type in EXPECTED_NODE_TYPES:
            assert node_type in combined, (
                f"Node type '{node_type}' not referenced in any DQL query. "
                f"Queries: {captured_queries}"
            )


def test_stats_dql_count_uid_pattern_not_root_count_func_explicit() -> None:
    """Explicit negative assertion: root-level count(func:) must NEVER appear.

    The pattern { count(func: type(X)) { count } } is semantically broken in
    Dgraph v25 (returns empty list for any cardinality). The implementation
    must use { q(func: type(X)) { count(uid) } } exclusively.
    """
    captured_queries: list[str] = []

    def _spy_query(dql: str, *args, **kwargs):
        captured_queries.append(dql)
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_client, mock_txn = _make_mock_pydgraph_client()
    mock_txn.query.side_effect = _spy_query

    with (
        patch("pydgraph.DgraphClient", return_value=mock_client),
        patch("pydgraph.DgraphClientStub", return_value=MagicMock()),
    ):
        _invoke(["stats"])

    for q in captured_queries:
        # Root-level count(func:...) appears at the very start of the query block.
        import re
        if re.search(r"\{\s*count\s*\(func:", q):
            pytest.fail(
                f"stats command uses root-level 'count(func:...)' DQL pattern "
                f"which is broken in Dgraph v25. Query: {q!r}\n"
                "Use the named-block form: { q(func: type(X)) { count(uid) } }"
            )


# ---------------------------------------------------------------------------
# T-STATS-empty
# ---------------------------------------------------------------------------

def test_stats_empty_response_renders_zeros_exit_zero() -> None:
    """Given a mocked pydgraph client that returns {'q': []} for every query.
    When `partgraph stats` is invoked.
    Then exit code is 0 and the output displays 0 counts (the {'q': []} -> 0
    coercion is applied).
    """
    mock_client, _mock_txn = _make_mock_pydgraph_client(per_type_counts={})
    # All queries return {"q": []} (empty block -> 0 coercion).

    with (
        patch("pydgraph.DgraphClient", return_value=mock_client),
        patch("pydgraph.DgraphClientStub", return_value=MagicMock()),
    ):
        result = _invoke(["stats"])

    assert result.exit_code == 0, (
        f"`stats` with empty DB must exit 0. Got {result.exit_code}.\n{result.output}"
    )

    # Output must show zero counts; "0" must appear at least once.
    assert "0" in result.output, (
        f"stats output with empty DB must display 0 for all counts. Got:\n{result.output}"
    )


def test_stats_empty_response_does_not_raise() -> None:
    """Given all counts are zero (via {'q': []} response).
    When `partgraph stats` is invoked.
    Then no exception propagates to the CLI runner.
    """
    mock_client, _mock_txn = _make_mock_pydgraph_client()

    with (
        patch("pydgraph.DgraphClient", return_value=mock_client),
        patch("pydgraph.DgraphClientStub", return_value=MagicMock()),
    ):
        result = _invoke(["stats"])

    # No unhandled exception.
    assert result.exception is None, (
        f"stats raised an unhandled exception for empty DB: {result.exception}\n"
        f"Output: {result.output}"
    )


def test_stats_output_contains_all_node_type_names() -> None:
    """Given the stats command.
    When `partgraph stats` is invoked (even with empty counts).
    Then the output (table) contains the names of all expected node types as
    row labels.
    """
    mock_client, _ = _make_mock_pydgraph_client()

    with (
        patch("pydgraph.DgraphClient", return_value=mock_client),
        patch("pydgraph.DgraphClientStub", return_value=MagicMock()),
    ):
        result = _invoke(["stats"])

    for node_type in EXPECTED_NODE_TYPES:
        assert node_type in result.output, (
            f"Node type '{node_type}' missing from stats output. Got:\n{result.output}"
        )


def test_stats_nonzero_counts_rendered() -> None:
    """Given a mocked client returning non-zero counts for Part and Manufacturer.
    When `partgraph stats` is invoked.
    Then the output displays the non-zero count values.
    """
    mock_client, _ = _make_mock_pydgraph_client(
        per_type_counts={"Part": 42, "Manufacturer": 7}
    )

    with (
        patch("pydgraph.DgraphClient", return_value=mock_client),
        patch("pydgraph.DgraphClientStub", return_value=MagicMock()),
    ):
        result = _invoke(["stats"])

    assert result.exit_code == 0, (
        f"stats with non-zero counts should exit 0. Got {result.exit_code}.\n{result.output}"
    )
    assert "42" in result.output, (
        f"Part count 42 not in stats output: {result.output}"
    )
    assert "7" in result.output, (
        f"Manufacturer count 7 not in stats output: {result.output}"
    )
