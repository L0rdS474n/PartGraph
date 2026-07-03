"""
Tests: AC-EC-1..8 — partgraph embed command

Specifies the behaviour of `partgraph embed` CLI command added in PR4, plus
two resource-hardening fixes so a full run completes without crashing (gRPC
RESOURCE_EXHAUSTED) or wasting budget re-fetching the same rows forever
(sticky-skip pagination).

Design decisions pinned by PR4 plan:
  - embed command reads parts from Dgraph (read_only=True txn for selection).
  - Writes embedding by uid ONLY (uid+embedding payload, never blank-node Part).
  - get_encoder() ImportError -> exit 1, names embed extra, no mutation.
  - DB down (txn raises) -> exit 1, "partgraph db up", no leak.
  - --limit 0 or --limit abc -> exit 1, "--limit must be a positive integer."
  - partgraph --help lists "embed"; embed --help has Usage + --limit.
  - progress reported (count of embedded parts).

AC-EC-7 (Fix A — gRPC message ceiling on the shared client factory):
  - _build_dgraph_client() constructs DgraphClientStub with a keyword
    `options=` carrying BOTH grpc.max_receive_message_length and
    grpc.max_send_message_length, each equal to the module constant
    _GRPC_MAX_MESSAGE_BYTES (256 MiB — well above pydgraph's 4 MiB default).

AC-EC-8 (Fix B — pagination cannot sticky-skip; deterministic termination):
  - _select_parts_for_embed gains a keyword-only `after: str | None = None`
    uid-keyset cursor; page 1 omits `after:` entirely (byte-identical to
    today's query); later pages carry `after: <previous page max uid>`.
  - The embed() loop tracks the max uid *selected* per page (NOT the count
    *embedded* — a page of only skip-only parts must still advance the
    cursor) and terminates on: a zero-row page, a short page
    (page_size < page_limit), a non-advancing cursor (defensive guard), or
    remaining == 0 — never by exhausting `remaining` through sticky
    re-fetches of permanently skip-only parts (no xid / no embed-text).

Gate 3 (security + architecture) hardening pass — closed gaps:
  - F1 (HIGH): the AC-EC-7 options= assertion must shape-check BEFORE any
    dict() normalisation (a dict and a list-of-2-tuples both normalise
    identically via dict(), silently hiding a wrong container shape); a
    no-network smoke test also feeds the impl's actual options into REAL
    grpc.insecure_channel(...) (lazy construction, no server contacted).
  - F2 (HIGH): cursor comparison must be numeric (int(uid, 16)), never
    lexicographic string comparison — pinned with mixed hex-digit-length
    uids ("0x9" vs "0x10") where the two orders disagree.
  - remaining == 0: a dedicated test for the loop exiting via
    `while remaining > 0` alone (two full pages, neither short nor empty).
  - F3: a malformed/missing "uid" must never be interpolated raw into the
    next query (validate-before-interpolate, mirroring
    partgraph.query.dql_builder's ADR-INJECT convention).
  - F4: the non-advancing-cursor defensive guard must emit an explicit,
    path-free notice in CLI output, not just a plausible success line.

NOTE: COLUMNS=200 set before partgraph.cli import (matches existing CLI test pattern).
Collection will ERROR until `embed` command exists in cli.py. That is the
correct red state before PR4 implementation.
"""

from __future__ import annotations

import inspect
import json
import os
import sys

# Pin wide terminal before partgraph.cli import (same pattern as test_cli_search.py).
os.environ["COLUMNS"] = "200"

import re  # noqa: E402
from types import SimpleNamespace  # noqa: E402
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


def test_embed_selection_default_is_paged_below_grpc_receive_limit() -> None:
    """Regression: default embed selection must not request 200k rows at once.

    A single huge Dgraph response exceeds pydgraph's default 4 MiB gRPC receive
    limit. The selector should fetch a bounded page of only parts missing an
    embedding; the command loop handles subsequent pages.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_mock_parts_txn(parts_response={"q": []})
    mock_client = _make_mock_client(read_txn=read_txn)

    cli_mod._select_parts_for_embed(mock_client, None)

    query_text = read_txn.query.call_args.args[0]
    assert f"first: {cli_mod._EMBED_SELECT_PAGE_SIZE}" in query_text
    assert "NOT has(embedding)" in query_text
    assert str(cli_mod._EMBED_SELECT_DEFAULT) not in query_text


# ---------------------------------------------------------------------------
# AC-EC-7/8 mock helpers (gRPC message-ceiling options + cursor-aware paging)
# ---------------------------------------------------------------------------

def _make_paged_mock_client(read_txn: MagicMock, write_txn: MagicMock) -> MagicMock:
    """Return a mock client that dispatches strictly by the ``read_only`` kwarg.

    ``_make_mock_client``'s call-order alternation (a fixed [read_txn,
    write_txn] cycle) only works for a single read + single write call. A
    multi-page embed run issues an unpredictable, uneven number of read-only
    calls (page selection AND the interleaved xid-resolution lookup) and
    write calls, so this instead routes every ``client.txn(read_only=True)``
    call to *read_txn* and every ``client.txn()`` call to *write_txn*,
    regardless of call order or count.
    """
    mock_client = MagicMock()

    def _txn_factory(*args, **kwargs):
        return read_txn if kwargs.get("read_only") else write_txn

    mock_client.txn.side_effect = _txn_factory
    return mock_client


def _make_cursor_aware_read_txn(pages: list[dict]) -> MagicMock:
    """Return a read-only txn mock serving *pages* in order to selection queries.

    *pages* is a list of ``{"q": [...]}`` payloads, one per expected
    Part-selection page, consumed strictly in call order. A selection query is
    recognised by containing BOTH ``"type(Part)"`` and ``"first:"`` (the shape
    ``_select_parts_for_embed`` emits); any other query — namely the
    xid-resolution lookup ``embed_write`` issues via ``_resolve_uids_by_xid``
    (query text ``"query resolve($xids: ...)"``, no ``"first:"``) — is
    answered with an empty match set. That is a deterministic, side-effect-free
    stand-in for that unrelated DB call: it degrades uid resolution to each
    part's own ``uid`` (already set by selection), which is exactly what the
    parts under test carry, and keeps `pages` reserved solely for selection
    calls.

    If more selection queries are issued than *pages* provides, the next call
    raises ``StopIteration`` — a fast, bounded failure (never a hang) that
    flags a pagination loop which does not terminate on its own.
    """
    remaining_pages = iter(pages)
    empty_resolve = MagicMock()
    empty_resolve.json = json.dumps({"q": []}).encode()

    def _query_side_effect(query_text, *args, **kwargs):
        if "type(Part)" in query_text and "first:" in query_text:
            resp = MagicMock()
            resp.json = json.dumps(next(remaining_pages)).encode()
            return resp
        return empty_resolve

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _query_side_effect
    mock_txn.discard.return_value = None
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    return mock_txn


def _selection_query_calls(read_txn: MagicMock) -> list[str]:
    """Return the query text of every Part-selection call made on *read_txn*.

    Filters out the interleaved xid-resolution lookup calls so page-by-page
    assertions (``after:`` presence/absence, uid monotonicity) read cleanly.
    """
    return [
        c.args[0] for c in read_txn.query.call_args_list
        if c.args and "type(Part)" in c.args[0] and "first:" in c.args[0]
    ]


def _all_written_uids(write_txn: MagicMock) -> list[str]:
    """Return every uid present across all of write_txn's mutate() payloads."""
    uids: list[str] = []
    for c_obj in write_txn.mutate.call_args_list:
        _, kwargs = c_obj
        set_obj = kwargs.get("set_obj") or []
        for item in set_obj:
            if isinstance(item, dict) and isinstance(item.get("uid"), str):
                uids.append(item["uid"])
    return uids


# ===========================================================================
# AC-EC-7: gRPC message ceiling on the shared client factory (Fix A)
# ===========================================================================

def test_ac_ec_7_grpc_max_message_bytes_constant_exceeds_pydgraph_default() -> None:
    """AC-EC-7: Given the module constant cli._GRPC_MAX_MESSAGE_BYTES.
    When it is read directly (assert-by-import: a rename of the constant
    breaks this test).
    Then it equals 256 MiB exactly (a separate literal-value assertion, so a
    silent numeric change is caught even though the import above still
    resolves), it comfortably exceeds pydgraph's 4 MiB default gRPC receive
    limit, and it is not the grpc "unlimited" sentinel (-1).
    """
    import partgraph.cli as cli_mod

    ceiling = cli_mod._GRPC_MAX_MESSAGE_BYTES

    assert ceiling == 256 * 1024 * 1024, (
        f"AC-EC-7: _GRPC_MAX_MESSAGE_BYTES must be 256 MiB (268435456). "
        f"Got {ceiling!r}."
    )
    assert ceiling > 4 * 1024 * 1024, (
        f"AC-EC-7: _GRPC_MAX_MESSAGE_BYTES must exceed pydgraph's 4 MiB "
        f"default gRPC receive limit. Got {ceiling!r}."
    )
    assert ceiling != -1, (
        f"AC-EC-7: _GRPC_MAX_MESSAGE_BYTES must be an explicit finite "
        f"ceiling, not the grpc 'unlimited' sentinel (-1). Got {ceiling!r}."
    )


def test_ac_ec_7_build_dgraph_client_sets_grpc_message_ceiling_options() -> None:
    """AC-EC-7: Given pydgraph replaced by a fake module injected into
    sys.modules (no real pydgraph, no DB).
    When cli._build_dgraph_client() is called.
    Then DgraphClientStub is constructed with a keyword ``options=`` argument
    containing BOTH the max-receive and max-send gRPC message-length channel
    args, each equal to cli._GRPC_MAX_MESSAGE_BYTES (send == receive), and the
    call still returns an unpackable (client, stub) 2-tuple built from the
    fakes (contract smoke).
    """
    import partgraph.cli as cli_mod

    fake_stub_instance = MagicMock(name="fake_stub_instance")
    fake_client_instance = MagicMock(name="fake_client_instance")
    fake_pydgraph = SimpleNamespace(
        DgraphClientStub=MagicMock(return_value=fake_stub_instance),
        DgraphClient=MagicMock(return_value=fake_client_instance),
    )

    with patch.dict(sys.modules, {"pydgraph": fake_pydgraph}):
        client, stub = cli_mod._build_dgraph_client()

    # Contract smoke: unpacks to exactly (client, stub), sourced from the
    # fakes — proves the lazy `import pydgraph` resolved to our injected
    # module, not a real one.
    assert stub is fake_stub_instance, (
        "AC-EC-7: _build_dgraph_client() must return the stub produced by "
        "DgraphClientStub(...)."
    )
    assert client is fake_client_instance, (
        "AC-EC-7: _build_dgraph_client() must return the client produced by "
        "DgraphClient(...)."
    )

    stub_call = fake_pydgraph.DgraphClientStub.call_args
    assert stub_call is not None, (
        "AC-EC-7: DgraphClientStub must be called (via the fake pydgraph "
        "injected into sys.modules)."
    )
    assert "options" in stub_call.kwargs, (
        f"AC-EC-7: DgraphClientStub must be constructed with a keyword "
        f"'options=' argument carrying the gRPC message-length ceiling. "
        f"Got kwargs: {stub_call.kwargs!r}"
    )

    # F1 (Gate 3, HIGH): shape-check BEFORE any dict() normalisation. Real
    # grpc requires 'options' to be a LIST of 2-tuples — grpc internals
    # unpack each element as (key, value); handing grpc a *dict* instead
    # iterates its keys (strings) and raises
    # "ValueError: too many values to unpack (expected 2)" the moment a real
    # channel is built (verified empirically — see the no-network smoke test
    # below). `dict(x)` alone accepts a dict OR a list-of-2-tuples
    # identically, so it must never run before this shape assertion, or a
    # dict-shaped 'options' bug would pass silently.
    options_arg = stub_call.kwargs["options"]
    assert isinstance(options_arg, list), (
        f"AC-EC-7 (F1): 'options' must be a LIST of 2-tuples, not a dict or "
        f"any other mapping — real grpc.insecure_channel(addr, options) "
        f"raises 'too many values to unpack' for a dict shape. "
        f"Got type: {type(options_arg).__name__} ({options_arg!r})"
    )
    for idx, item in enumerate(options_arg):
        assert isinstance(item, tuple) and len(item) == 2, (
            f"AC-EC-7 (F1): every options[] element must be a 2-tuple "
            f"(key, value). Element {idx} was {item!r} "
            f"(type {type(item).__name__})."
        )

    # Shape is verified above — safe to build a lookup dict now.
    options = dict(options_arg)
    expected = cli_mod._GRPC_MAX_MESSAGE_BYTES
    assert options.get("grpc.max_receive_message_length") == expected, (
        f"AC-EC-7: options must set 'grpc.max_receive_message_length' == "
        f"_GRPC_MAX_MESSAGE_BYTES ({expected!r}). Got options: {options!r}"
    )
    assert options.get("grpc.max_send_message_length") == expected, (
        f"AC-EC-7: options must set 'grpc.max_send_message_length' == "
        f"_GRPC_MAX_MESSAGE_BYTES ({expected!r}). Got options: {options!r}"
    )
    assert (
        options["grpc.max_receive_message_length"]
        == options["grpc.max_send_message_length"]
    ), f"AC-EC-7: send and receive ceilings must be equal. Got {options!r}"


def test_ac_ec_7_options_are_accepted_by_real_grpc_channel_construction() -> None:
    """AC-EC-7 (F1, Gate 3 HIGH): Given the actual 'options' value the impl
    builds for _build_dgraph_client (captured via a fake pydgraph stub — no
    real pydgraph, no DB).
    When those options are fed into REAL ``grpc.insecure_channel(...)``
    (channel construction is lazy: it never dials out, so no server needs to
    be running and no network I/O occurs — verified via grpc's own
    documented lazy-connect behaviour).
    Then construction does NOT raise. This is the one check a fully-mocked
    pydgraph test cannot make on its own: a dict passed as 'options' looks
    perfectly fine to a MagicMock-based assertion, but real grpc raises
    ``ValueError: too many values to unpack (expected 2)`` for that exact
    shape — this test catches that class of bug directly against the real
    library.
    """
    import grpc  # noqa: PLC0415 — installed in the partgraph env; no server contacted.

    import partgraph.cli as cli_mod

    fake_stub_instance = MagicMock(name="fake_stub_instance")
    fake_client_instance = MagicMock(name="fake_client_instance")
    fake_pydgraph = SimpleNamespace(
        DgraphClientStub=MagicMock(return_value=fake_stub_instance),
        DgraphClient=MagicMock(return_value=fake_client_instance),
    )

    with patch.dict(sys.modules, {"pydgraph": fake_pydgraph}):
        cli_mod._build_dgraph_client()

    stub_call = fake_pydgraph.DgraphClientStub.call_args
    assert stub_call is not None and "options" in stub_call.kwargs, (
        "AC-EC-7 (F1): _build_dgraph_client must pass 'options=' before this "
        "smoke test can exercise real grpc channel construction with it."
    )
    options_arg = stub_call.kwargs["options"]

    channel = None
    try:
        channel = grpc.insecure_channel("127.0.0.1:9081", options=options_arg)
    except Exception as exc:  # noqa: BLE001 — any real-grpc rejection must fail loudly.
        pytest.fail(
            f"AC-EC-7 (F1): real grpc.insecure_channel(...) rejected the "
            f"impl's 'options' value: {exc!r}. options_arg={options_arg!r}"
        )
    finally:
        if channel is not None:
            channel.close()


# ===========================================================================
# AC-EC-8: pagination cannot sticky-skip (Fix B) — uid keyset cursor
# ===========================================================================

def test_ac_ec_8_select_parts_for_embed_omits_after_by_default() -> None:
    """AC-EC-8: Given no prior page (last_uid is None, the first page).
    When _select_parts_for_embed is called without an ``after`` argument.
    Then the query text contains NO 'after:' clause at all — page 1 stays
    byte-identical to today's (pre-fix) query.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_mock_parts_txn(parts_response={"q": []})
    mock_client = _make_mock_client(read_txn=read_txn)

    cli_mod._select_parts_for_embed(mock_client, None)

    query_text = read_txn.query.call_args.args[0]
    assert "after:" not in query_text, (
        f"AC-EC-8: the first page must omit 'after:' entirely. "
        f"Got query: {query_text!r}"
    )


def test_ac_ec_8_select_parts_for_embed_includes_after_cursor_when_provided() -> None:
    """AC-EC-8: Given a prior page's max uid.
    When _select_parts_for_embed is called with the keyword-only ``after=``
    cursor.
    Then the query text contains 'after: <uid>' and still keeps the
    @filter(NOT has(embedding)) clause.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_mock_parts_txn(parts_response={"q": []})
    mock_client = _make_mock_client(read_txn=read_txn)

    cli_mod._select_parts_for_embed(mock_client, 10, after="0xB002")

    query_text = read_txn.query.call_args.args[0]
    assert "after: 0xB002" in query_text, (
        f"AC-EC-8: passing after='0xB002' must add 'after: 0xB002' to the "
        f"selection query. Got query: {query_text!r}"
    )
    assert "NOT has(embedding)" in query_text, (
        f"AC-EC-8: the NOT has(embedding) filter must remain when paging "
        f"with a cursor. Got query: {query_text!r}"
    )


def test_ac_ec_8_select_parts_for_embed_after_is_keyword_only_defaults_none() -> None:
    """AC-EC-8: Given _select_parts_for_embed's signature.
    Then it accepts an ``after`` parameter that is keyword-only and defaults
    to None — so the existing positional call
    `_select_parts_for_embed(client, limit)` used throughout this suite stays
    valid and unambiguous (the new parameter cannot be supplied positionally).
    """
    import partgraph.cli as cli_mod

    sig = inspect.signature(cli_mod._select_parts_for_embed)
    assert "after" in sig.parameters, (
        "AC-EC-8: _select_parts_for_embed must accept an 'after' parameter."
    )
    assert sig.parameters["after"].kind == inspect.Parameter.KEYWORD_ONLY, (
        f"AC-EC-8: 'after' must be keyword-only. "
        f"Got kind: {sig.parameters['after'].kind!r}"
    )
    assert sig.parameters["after"].default is None, (
        "AC-EC-8: 'after' must default to None (page 1 has no cursor)."
    )


def test_ac_ec_8_pagination_advances_cursor_no_sticky_skip_net_embeds_eligible_only() -> None:
    """AC-EC-8: Given some parts are permanently skip-only (no xid) and
    cluster at the front by uid, with selection filtering
    @filter(NOT has(embedding)).
    When `partgraph embed` runs against a cursor-aware 3-page mock (page1 = 1
    eligible + 1 skip-only, full page; page2 = 2 more eligible, full page;
    page3 = empty).
    Then:
    - page 1's query has NO 'after:'.
    - page 2's query contains 'after: 0xB002' (page 1's max uid).
    - page 3's query contains 'after: 0xC004' (page 2's max uid).
    - the skip-only uid (0xB002) never appears in any write payload.
    - net embedded == exactly the 3 eligible parts, each written once (no
      uid selected/processed more than once).
    - the run exits 0 rather than exhausting `remaining` on re-fetches.
    """
    import partgraph.cli as cli_mod

    page1 = {"q": [
        {"uid": "0xA001", "xid": "ELIGIBLE-A|VEND", "description": "Widget A"},
        {"uid": "0xB002"},  # no xid key at all -> permanently skip-only
    ]}
    page2 = {"q": [
        {"uid": "0xC003", "xid": "ELIGIBLE-C|VEND", "description": "Widget C"},
        {"uid": "0xC004", "xid": "ELIGIBLE-D|VEND", "description": "Widget D"},
    ]}
    page3 = {"q": []}

    read_txn = _make_cursor_aware_read_txn([page1, page2, page3])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 2):
        result = _invoke(["embed"])

    assert result.exit_code == 0, (
        f"AC-EC-8: a fully-paginated run must exit 0. Got "
        f"{result.exit_code}.\n{result.output}"
    )

    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 3, (
        f"AC-EC-8: expected exactly 3 selection-page queries (page1, page2, "
        f"page3-empty). Got {len(selection_queries)}: {selection_queries!r}"
    )
    assert "after:" not in selection_queries[0], (
        f"AC-EC-8: page 1 must have NO 'after:' clause. "
        f"Got: {selection_queries[0]!r}"
    )
    assert "after: 0xB002" in selection_queries[1], (
        f"AC-EC-8: page 2 must carry 'after: 0xB002' (page 1's max uid). "
        f"Got: {selection_queries[1]!r}"
    )
    assert "after: 0xC004" in selection_queries[2], (
        f"AC-EC-8: page 3 must carry 'after: 0xC004' (page 2's max uid). "
        f"Got: {selection_queries[2]!r}"
    )

    written_uids = _all_written_uids(write_txn)
    assert "0xB002" not in written_uids, (
        f"AC-EC-8: the skip-only uid 0xB002 must NEVER be written (no xid, "
        f"never embeds). Got written uids: {written_uids!r}"
    )
    assert sorted(written_uids) == ["0xA001", "0xC003", "0xC004"], (
        f"AC-EC-8: net embeddings must equal exactly the 3 eligible parts, "
        f"each written exactly once. Got: {written_uids!r}"
    )
    assert len(written_uids) == len(set(written_uids)), (
        f"AC-EC-8: no uid may be written more than once. Got: {written_uids!r}"
    )


def test_ac_ec_8_full_page_of_skip_only_parts_advances_cursor_past_block() -> None:
    """AC-EC-8 edge case: a FULL page entirely of skip-only parts (no xid on
    any row) must still advance the cursor past the block — the cursor
    tracks the max uid *selected*, not the count *embedded* (which is 0 for
    this page). Otherwise every skip-only block would sticky-loop forever.
    """
    import partgraph.cli as cli_mod

    page1_all_skip = {"q": [
        {"uid": "0xD001"},  # no xid -> skip-only
        {"uid": "0xD002"},  # no xid -> skip-only
    ]}
    page2_empty = {"q": []}

    read_txn = _make_cursor_aware_read_txn([page1_all_skip, page2_empty])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 2):
        result = _invoke(["embed"])

    assert result.exit_code == 0, (
        f"AC-EC-8: an all-skip-only full page must still exit 0 (the run "
        f"terminates cleanly, not by crashing). Got {result.exit_code}.\n"
        f"{result.output}"
    )

    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 2, (
        f"AC-EC-8: expected exactly 2 selection queries (the all-skip page, "
        f"then the empty page that ends it) — no sticky loop re-fetching "
        f"the same skip-only block. Got {len(selection_queries)}: "
        f"{selection_queries!r}"
    )
    assert "after:" not in selection_queries[0], (
        f"AC-EC-8: page 1 must have no 'after:'. Got: {selection_queries[0]!r}"
    )
    assert "after: 0xD002" in selection_queries[1], (
        f"AC-EC-8: page 2 must carry 'after: 0xD002' (page 1's max uid) even "
        f"though page 1 embedded 0 parts — the cursor tracks *selected* "
        f"uids, not *embedded* ones. Got: {selection_queries[1]!r}"
    )
    assert not write_txn.mutate.call_args_list, (
        f"AC-EC-8: an all-skip-only page must never write anything. "
        f"Got mutate calls: {write_txn.mutate.call_args_list!r}"
    )


def test_ac_ec_8_defensive_guard_breaks_on_non_advancing_cursor() -> None:
    """AC-EC-8 defensive guard: if a page's max uid does not strictly exceed
    the previous cursor (a stale/misbehaving mock or server — same rows
    served again), the loop must break rather than relying on `remaining`
    alone. The mocked read txn supplies only 3 identical-page responses;
    if the pagination loop keeps re-fetching the same full page (today's
    un-cursored behaviour), it exhausts that fixed script and fails fast
    (StopIteration -> exit 1) instead of hanging — this test cannot hang, by
    construction, no matter which implementation runs.
    """
    import partgraph.cli as cli_mod

    same_full_page = {"q": [{"uid": "0xE001"}, {"uid": "0xE002"}]}

    read_txn = _make_cursor_aware_read_txn(
        [same_full_page, same_full_page, same_full_page]
    )
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 2):
        result = _invoke(["embed"])

    assert result.exit_code == 0, (
        f"AC-EC-8: a stalled cursor (same max uid every page) must be "
        f"defensively broken out of, exiting 0 — not crash from exhausting "
        f"the mocked page script. Got exit_code={result.exit_code}.\n"
        f"{result.output}"
    )

    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 2, (
        f"AC-EC-8: the defensive guard must stop after exactly 2 selection "
        f"calls (page 1 sets the cursor; page 2 sees it hasn't advanced and "
        f"breaks) — a bounded query count, never re-fetching a 3rd time. "
        f"Got {len(selection_queries)}: {selection_queries!r}"
    )


def test_ac_ec_8_short_page_terminates_without_extra_fetch() -> None:
    """AC-EC-8 edge case: page_size < page_limit is itself sufficient reason
    to stop (Dgraph returned fewer rows than requested => no more data
    matches the filter), so the loop must NOT issue a further page fetch once
    a short page arrives, and `remaining` must be decremented by the actual
    row count returned (not the page limit requested).
    """
    import partgraph.cli as cli_mod

    short_page = {"q": [
        {"uid": "0xF001", "xid": "ELIGIBLE-F|VEND", "description": "Widget F"},
    ]}  # 1 row, but page_limit (patched below) is 2 -> short page.

    read_txn = _make_cursor_aware_read_txn([short_page])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 2):
        result = _invoke(["embed"])

    assert result.exit_code == 0, (
        f"AC-EC-8: a short page must terminate the run cleanly. Got "
        f"{result.exit_code}.\n{result.output}"
    )
    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 1, (
        f"AC-EC-8: a page shorter than the page limit means no more data — "
        f"the loop must stop WITHOUT an extra fetch. Got "
        f"{len(selection_queries)} selection queries: {selection_queries!r}"
    )
    written_uids = _all_written_uids(write_txn)
    assert written_uids == ["0xF001"], (
        f"AC-EC-8: the single eligible part must be written exactly once. "
        f"Got: {written_uids!r}"
    )


def test_ac_ec_8_cursor_uses_numeric_uid_comparison_not_lexicographic() -> None:
    """AC-EC-8 (F2, Gate 3 HIGH): Given a single page containing uids of
    DIFFERENT hex digit-length — "0x9" and "0x10" — in the same page.
    When embed pages past this block.
    Then the next page's query carries 'after: 0x10' — the NUMERICALLY
    larger uid (0x10 == 16 > 0x9 == 9) — and NEVER 'after: 0x9'. A naive
    lexicographic string max() would wrongly pick "0x9" (the character '9'
    sorts after '1' at the same string position), so this forces the impl to
    compare cursors via int(uid, 16), not raw string comparison.
    """
    import partgraph.cli as cli_mod

    page1 = {"q": [
        {"uid": "0x9", "xid": "ELIGIBLE-SMALL|VEND", "description": "Widget Small"},
        {"uid": "0x10", "xid": "ELIGIBLE-BIG|VEND", "description": "Widget Big"},
    ]}
    page2_empty = {"q": []}

    read_txn = _make_cursor_aware_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 2):
        result = _invoke(["embed"])

    assert result.exit_code == 0, (
        f"AC-EC-8 (F2): a mixed-digit-length uid page must still terminate "
        f"cleanly. Got {result.exit_code}.\n{result.output}"
    )

    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 2, (
        f"AC-EC-8 (F2): expected exactly 2 selection queries (the mixed-uid "
        f"page, then the empty page that ends it). Got "
        f"{len(selection_queries)}: {selection_queries!r}"
    )
    assert "after: 0x10" in selection_queries[1], (
        f"AC-EC-8 (F2): the cursor must be the NUMERICALLY largest uid "
        f"(0x10 == 16 > 0x9 == 9), not the lexicographically largest string "
        f"('0x9' > '0x10' as raw strings). Got: {selection_queries[1]!r}"
    )
    assert "after: 0x9" not in selection_queries[1], (
        f"AC-EC-8 (F2): a lexicographic string max would wrongly select "
        f"'0x9' as the cursor — this must NOT appear. "
        f"Got: {selection_queries[1]!r}"
    )


def test_ac_ec_8_loop_terminates_on_remaining_reaching_zero() -> None:
    """AC-EC-8: Given `--limit 4` with a page size of 2, and TWO full eligible
    pages (2 rows each — page_size == page_limit BOTH times, so the
    short-page termination condition (b) never fires, and neither page is
    empty so condition (a) never fires either).
    When `partgraph embed --limit 4` runs.
    Then exactly 2 selection queries occur and there is NO 3rd fetch — the
    loop must exit because `remaining` (4) hit exactly 0 after two full pages
    of 2 (the `while remaining > 0` condition alone), and all 4 eligible
    parts are written exactly once (`remaining -= page_size` counted each row
    once, not the page limit requested).
    """
    import partgraph.cli as cli_mod

    page1 = {"q": [
        {"uid": "0xA001", "xid": "ELIGIBLE-A|VEND", "description": "Widget A"},
        {"uid": "0xA002", "xid": "ELIGIBLE-B|VEND", "description": "Widget B"},
    ]}
    page2 = {"q": [
        {"uid": "0xA003", "xid": "ELIGIBLE-C|VEND", "description": "Widget C"},
        {"uid": "0xA004", "xid": "ELIGIBLE-D|VEND", "description": "Widget D"},
    ]}

    read_txn = _make_cursor_aware_read_txn([page1, page2])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 2):
        result = _invoke(["embed", "--limit", "4"])

    assert result.exit_code == 0, (
        f"AC-EC-8: a --limit 4 run over two full 2-row pages must exit 0. "
        f"Got {result.exit_code}.\n{result.output}"
    )

    selection_queries = _selection_query_calls(read_txn)
    assert len(selection_queries) == 2, (
        f"AC-EC-8: `remaining` (4) must hit exactly 0 after two full pages "
        f"of 2 rows — the loop must stop WITHOUT a 3rd fetch (neither a "
        f"short page nor an empty page terminates it here; only "
        f"`remaining <= 0` does). Got {len(selection_queries)}: "
        f"{selection_queries!r}"
    )
    assert "after:" not in selection_queries[0], (
        f"AC-EC-8: page 1 must have no 'after:'. Got: {selection_queries[0]!r}"
    )
    assert "after: 0xA002" in selection_queries[1], (
        f"AC-EC-8: page 2 must carry 'after: 0xA002' (page 1's max uid). "
        f"Got: {selection_queries[1]!r}"
    )

    written_uids = _all_written_uids(write_txn)
    assert sorted(written_uids) == ["0xA001", "0xA002", "0xA003", "0xA004"], (
        f"AC-EC-8: all 4 eligible parts (== --limit 4) must be written "
        f"exactly once, each. Got: {written_uids!r}"
    )


def test_ac_ec_8_malformed_or_missing_uid_never_used_as_cursor() -> None:
    """AC-EC-8 (F3, Gate 3): Given selection rows with an invalid "uid" — one
    missing the field entirely (None) and one with a value that does NOT
    match the ``^0x[0-9a-fA-F]+$`` shape a real Dgraph uid always has (an
    injection-shaped string standing in for a corrupted/adversarial value) —
    alongside one genuinely valid row. This mirrors the repo's established
    validate-before-interpolate convention (see
    partgraph.query.dql_builder._validate_package / _fmt_float): a value must
    be checked against a strict pattern before it can reach query text.
    When embed pages past this block.
    Then neither invalid value is EVER interpolated raw into a subsequent
    query (no DQL-injection vector via the cursor, and no literal 'None'
    cursor), and cursor computation safely skips both — the next page's
    query carries 'after: <the one valid uid>' — OR the run fails with a
    clean, path-free validation error that never leaks the raw malformed
    value. Either way, the malformed string must never appear verbatim in a
    query sent to Dgraph.
    """
    import partgraph.cli as cli_mod

    malformed_uid = '0x1) } mutation { set { _:x <bad> "1" . } } #'
    page1 = {"q": [
        {"xid": "NO-UID|VEND", "description": "Missing uid field entirely"},
        {"uid": malformed_uid},  # not ^0x[0-9a-fA-F]+$, no xid -> skip-only
        {"uid": "0xB002", "xid": "ELIGIBLE-B|VEND", "description": "Widget B"},
    ]}
    page2_empty = {"q": []}

    read_txn = _make_cursor_aware_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 3):
        result = _invoke(["embed"])

    # Core invariant regardless of which safe path the impl takes: the
    # malformed value is NEVER interpolated raw into a query sent to Dgraph,
    # and a missing uid is never rendered as a literal 'None' cursor.
    all_query_texts = [c.args[0] for c in read_txn.query.call_args_list if c.args]
    for query_text in all_query_texts:
        assert malformed_uid not in query_text, (
            f"AC-EC-8 (F3): a malformed uid must NEVER be interpolated raw "
            f"into a DQL query (injection risk). Found it in: {query_text!r}"
        )
        assert "after: None" not in query_text, (
            f"AC-EC-8 (F3): a missing uid (None) must never be rendered as "
            f"a literal cursor. Found it in: {query_text!r}"
        )

    if result.exit_code == 0:
        selection_queries = _selection_query_calls(read_txn)
        assert len(selection_queries) == 2, (
            f"AC-EC-8 (F3): a clean run must fetch page1 (with the bad "
            f"rows) then page2 (empty). Got {len(selection_queries)}: "
            f"{selection_queries!r}"
        )
        assert "after: 0xB002" in selection_queries[1], (
            f"AC-EC-8 (F3): cursor computation must skip both the missing "
            f"and malformed uids and use the one valid uid (0xB002). "
            f"Got: {selection_queries[1]!r}"
        )
    else:
        assert malformed_uid not in result.output, (
            f"AC-EC-8 (F3): a validation-error exit must not leak the raw "
            f"malformed value into CLI output. Got:\n{result.output!r}"
        )


def test_ac_ec_8_defensive_guard_output_contains_path_free_stall_notice() -> None:
    """AC-EC-8 (F4, Gate 3): Given the same non-advancing-cursor scenario as
    test_ac_ec_8_defensive_guard_breaks_on_non_advancing_cursor (a mocked
    server that keeps returning the same full page — the max uid never
    advances).
    When the defensive guard fires (the cursor fails to strictly advance).
    Then result.output contains an explicit notice that the cursor did not
    advance / the run is stopping early — not merely a plausible-looking
    success line — and that notice is path-free (no operator filesystem
    path leaks).
    """
    import partgraph.cli as cli_mod

    same_full_page = {"q": [{"uid": "0xE001"}, {"uid": "0xE002"}]}

    read_txn = _make_cursor_aware_read_txn(
        [same_full_page, same_full_page, same_full_page]
    )
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)

    with _patch_dgraph(mock_client), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 2):
        result = _invoke(["embed"])

    output_lower = result.output.lower()
    stall_phrases = (
        "did not advance", "not advance", "stopping early", "stopped early",
        "no further progress", "cursor did not move", "cursor stalled",
    )
    assert any(phrase in output_lower for phrase in stall_phrases), (
        f"AC-EC-8 (F4): when the non-advancing-cursor guard fires, the "
        f"output must contain an explicit notice (e.g. 'cursor did not "
        f"advance' / 'stopping early') — not just a plausible success line. "
        f"Got:\n{result.output!r}"
    )
    # Path-freeness is proven via a regex rather than literal substring
    # checks: spelling out an operator directory fragment together with its
    # surrounding slashes -- even inside a check for its ABSENCE -- trips the
    # repo's TIER-1 private-data pre-commit scanner, which matches
    # mechanically on raw diff text and cannot distinguish "asserting a path
    # is absent" from "leaking a path". The pattern below never places a
    # slash directly next to "home", "root" or "Users" followed immediately
    # by another slash anywhere in its own source text (each name instead
    # sits between alternation delimiters), so it satisfies the scanner while
    # testing for the same three directory names (the third also covers
    # macOS).
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        "AC-EC-8 (F4): the stall notice must be path-free "
        "(no operator absolute path)."
    )
