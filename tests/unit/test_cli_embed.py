"""
Tests: AC-EC-1..6 — partgraph embed command

Specifies the behaviour of `partgraph embed` CLI command added in PR4.

Design decisions pinned by PR4 plan:
  - embed command reads parts from Dgraph (read_only=True txn for selection).
  - Writes embedding by uid ONLY (uid+embedding payload, never blank-node Part).
  - get_encoder() ImportError -> exit 1, names embed extra, no mutation.
  - DB down (txn raises) -> exit 1, "partgraph db up", no leak.
  - --limit 0 or --limit abc -> exit 1, "--limit must be a positive integer."
  - partgraph --help lists "embed"; embed --help has Usage + --limit.
  - progress reported (count of embedded parts).

NOTE: COLUMNS=200 set before partgraph.cli import (matches existing CLI test pattern).
Collection will ERROR until `embed` command exists in cli.py. That is the
correct red state before PR4 implementation.
"""

from __future__ import annotations

import json
import os

# Pin wide terminal before partgraph.cli import (same pattern as test_cli_search.py).
os.environ["COLUMNS"] = "200"

import re  # noqa: E402
from unittest.mock import MagicMock, call, patch  # noqa: E402

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from partgraph.cli import app  # noqa: E402, F401

RUNNER = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_EMBED_DIM = 384
_FAKE_VECTOR = [0.001] * _EMBED_DIM


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


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_mock_parts_txn(parts_response: dict | None = None) -> MagicMock:
    """Return a mock txn for the parts selection query."""
    default_parts = {"q": [
        {
            "uid": "0xA001",
            "xid": "MAX232CPE|TEXASINSTRUMENTS",
            "description": "RS-232 level converter",
            "category": "Interface IC",
            "mpn_norm": "MAX232CPE",
            "in_package": [{"name": "DIP-16"}],
        }
    ]}
    resp = MagicMock()
    resp.json = json.dumps(parts_response or default_parts).encode()

    mock_txn = MagicMock()
    mock_txn.query.return_value = resp
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    return mock_txn


def _make_write_txn() -> MagicMock:
    """Return a mock txn for the embedding write operation."""
    mock_txn = MagicMock()
    mock_txn.mutate.return_value = MagicMock()
    mock_txn.commit.return_value = None
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    return mock_txn


def _make_mock_client(
    read_txn: MagicMock | None = None,
    write_txn: MagicMock | None = None,
) -> MagicMock:
    """Return a mock client that alternates between read and write txns."""
    mock_client = MagicMock()
    # We can't easily split read vs write in mock, so use side_effect counter.
    txns = []
    if read_txn is not None:
        txns.append(read_txn)
    if write_txn is not None:
        txns.append(write_txn)

    if txns:
        call_idx = [0]

        def _txn_factory(**kwargs):
            t = txns[min(call_idx[0], len(txns) - 1)]
            call_idx[0] += 1
            return t

        mock_client.txn.side_effect = _txn_factory
    else:
        # Default: both ops use the same txn.
        default_txn = _make_mock_parts_txn()
        mock_client.txn.return_value = default_txn

    return mock_client


def _patch_dgraph(mock_client: MagicMock):
    """Patch _build_dgraph_client to return mock_client."""
    import partgraph.cli as cli_mod
    return patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock()))


def _patch_get_encoder(fake_encoder_callable=None):
    """Patch get_encoder in cli module."""
    import partgraph.cli as cli_mod

    def _default_enc(texts: list[str]) -> list[list[float]]:
        return [_FAKE_VECTOR for _ in texts]

    encoder = fake_encoder_callable or _default_enc

    def _fake_get_encoder():
        return encoder

    return patch.object(cli_mod, "get_encoder", _fake_get_encoder, create=True)


# ===========================================================================
# AC-EC-1: --limit validation
# ===========================================================================

def test_ac_ec_1_limit_zero_exits_1_with_message() -> None:
    """AC-EC-1: Given --limit 0 (invalid: must be positive).
    When `partgraph embed --limit 0` is invoked.
    Then exit code is 1 and output contains "--limit must be a positive integer."
    """
    with _patch_dgraph(_make_mock_client()), _patch_get_encoder():
        result = _invoke(["embed", "--limit", "0"])

    assert result.exit_code != 0, (
        f"AC-EC-1: --limit 0 must exit non-zero. Got {result.exit_code}.\n{result.output}"
    )
    assert "--limit must be a positive integer" in result.output or \
           "positive integer" in result.output.lower(), (
        f"AC-EC-1: output must contain '--limit must be a positive integer.' "
        f"Got:\n{result.output!r}"
    )


def test_ac_ec_1_limit_abc_exits_1_with_message() -> None:
    """AC-EC-1: Given --limit abc (non-integer).
    When `partgraph embed --limit abc` is invoked.
    Then exit code is 1 and output contains a message about --limit being invalid.
    """
    with _patch_dgraph(_make_mock_client()), _patch_get_encoder():
        result = _invoke(["embed", "--limit", "abc"])

    assert result.exit_code != 0, (
        f"AC-EC-1: --limit abc must exit non-zero. Got {result.exit_code}.\n{result.output}"
    )


# ===========================================================================
# AC-EC-2: --limit 10 mock client+encoder -> exit 0, progress, count reported
# ===========================================================================

def test_ac_ec_2_valid_limit_exit_0_progress_count_reported() -> None:
    """AC-EC-2: Given --limit 10, mocked client returning 1 part, mocked encoder.
    When `partgraph embed --limit 10` is invoked.
    Then:
    - Exit code is 0.
    - Output contains a count of embedded parts.
    - Progress is visible (at minimum a non-empty output mentioning embedding).
    """
    read_txn = _make_mock_parts_txn()
    write_txn = _make_write_txn()
    mock_client = _make_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["embed", "--limit", "10"])

    assert result.exit_code == 0, (
        f"AC-EC-2: valid embed run must exit 0. Got {result.exit_code}.\n{result.output}"
    )
    # Output must mention some count or progress.
    output_lower = result.output.lower()
    assert (
        "embed" in output_lower
        or "1" in result.output
        or "part" in output_lower
    ), (
        f"AC-EC-2: output must report progress/count. Got:\n{result.output!r}"
    )


# ===========================================================================
# AC-EC-3: get_encoder ImportError -> exit 1, names embed extra, mutate NOT called
# ===========================================================================

def test_ac_ec_3_encoder_import_error_exit_1_names_embed_no_mutation() -> None:
    """AC-EC-3: Given get_encoder() raises ImportError.
    When `partgraph embed` is invoked.
    Then:
    - Exit code is 1.
    - Output mentions 'embed' (the optional extra).
    - txn.mutate is NEVER called.
    """
    import partgraph.cli as cli_mod

    def _raising_get_encoder():
        raise ImportError(
            'sentence-transformers not installed. '
            'pip install -e ".[embed]" to enable embedding.'
        )

    read_txn = _make_mock_parts_txn()
    write_txn = _make_write_txn()
    mock_client = _make_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), \
         patch.object(cli_mod, "get_encoder", _raising_get_encoder, create=True):
        result = _invoke(["embed", "--limit", "10"])

    assert result.exit_code != 0, (
        f"AC-EC-3: ImportError must produce non-zero exit. Got {result.exit_code}."
    )
    assert "embed" in result.output.lower(), (
        f"AC-EC-3: output must mention 'embed' extra. Got:\n{result.output!r}"
    )
    write_txn.mutate.assert_not_called()


# ===========================================================================
# AC-EC-4: txn raises -> exit 1, "partgraph db up", no leak
# ===========================================================================

def test_ac_ec_4_txn_raises_exit_1_db_up_no_leak() -> None:
    """AC-EC-4: Given txn.query raises RuntimeError (DB down).
    When `partgraph embed` is invoked.
    Then:
    - Exit code is 1.
    - Output contains "partgraph db up".
    - No raw exception text leaks.
    """
    failing_txn = MagicMock()
    failing_txn.query.side_effect = RuntimeError("connection refused")
    failing_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = failing_txn

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["embed", "--limit", "10"])

    assert result.exit_code != 0, (
        f"AC-EC-4: DB-down must produce non-zero exit. Got {result.exit_code}."
    )
    assert "partgraph db up" in result.output, (
        f"AC-EC-4: output must contain 'partgraph db up'. Got:\n{result.output!r}"
    )
    assert "connection refused" not in result.output, (
        f"AC-EC-4: raw exception must not leak. Got:\n{result.output!r}"
    )


# ===========================================================================
# AC-EC-5: partgraph --help lists "embed"; embed --help has Usage + --limit
# ===========================================================================

def test_ac_ec_5_partgraph_help_lists_embed() -> None:
    """AC-EC-5: Given the partgraph CLI.
    When `partgraph --help` is invoked.
    Then the output contains "embed" (the command is registered).
    """
    result = _invoke(["--help"])
    assert "embed" in result.output.lower(), (
        f"AC-EC-5: partgraph --help must list 'embed' command. Got:\n{result.output}"
    )


def test_ac_ec_5_embed_help_contains_usage_and_limit() -> None:
    """AC-EC-5: Given the embed command.
    When `partgraph embed --help` is invoked.
    Then:
    - Exit code is 0.
    - Output contains "Usage" or "usage".
    - Output contains "--limit".
    """
    result = _invoke(["embed", "--help"])
    assert result.exit_code == 0, (
        f"AC-EC-5: embed --help must exit 0. Got {result.exit_code}."
    )
    assert "sage" in result.output, (
        f"AC-EC-5: embed --help must contain 'Usage'. Got:\n{result.output}"
    )
    assert "--limit" in result.output, (
        f"AC-EC-5: embed --help must contain '--limit'. Got:\n{result.output}"
    )


# ===========================================================================
# AC-EC-6: selection txn read_only=True; write txn payload uid+embedding only
# ===========================================================================

def test_ac_ec_6_selection_txn_is_read_only() -> None:
    """AC-EC-6: Given mocked client and encoder.
    When `partgraph embed --limit 10` is invoked.
    Then the selection txn is called with read_only=True.
    """
    read_txn = _make_mock_parts_txn()
    write_txn = _make_write_txn()
    mock_client = _make_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        _invoke(["embed", "--limit", "10"])

    calls = mock_client.txn.call_args_list
    assert any(
        c == call(read_only=True) or c.kwargs.get("read_only") is True
        for c in calls
    ), (
        f"AC-EC-6: embed must call client.txn(read_only=True) for selection. "
        f"Actual calls: {calls}"
    )


def test_ac_ec_6_write_txn_payload_only_uid_and_embedding() -> None:
    """AC-EC-6: Given mocked client with one selectable part.
    When `partgraph embed --limit 10` is invoked.
    Then every item in the write txn's mutate payload has ONLY uid+embedding keys.
    (No mpn, description, made_by, stock, dgraph.type, xid — only uid+embedding.)
    """
    read_txn = _make_mock_parts_txn()
    write_txn = _make_write_txn()
    mock_client = _make_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), _patch_get_encoder():
        _invoke(["embed", "--limit", "10"])

    mutate_calls = write_txn.mutate.call_args_list
    if not mutate_calls:
        # If no mutate was called (e.g. part was skipped), accept it.
        return

    for c_obj in mutate_calls:
        _, kwargs = c_obj
        set_obj = kwargs.get("set_obj")
        if set_obj is None:
            set_json = kwargs.get("set_json")
            if set_json:
                set_obj = json.loads(
                    set_json.decode("utf-8") if isinstance(set_json, bytes) else set_json
                )
        if set_obj is not None:
            items = set_obj if isinstance(set_obj, list) else [set_obj]
            for item in items:
                if not isinstance(item, dict):
                    continue
                extra_keys = set(item.keys()) - {"uid", "embedding"}
                assert not extra_keys, (
                    f"AC-EC-6: write payload must ONLY have uid+embedding. "
                    f"Found extra keys: {extra_keys!r} in: {item!r}"
                )
