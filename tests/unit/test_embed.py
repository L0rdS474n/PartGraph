"""
Tests: AC-ET / AC-EG / AC-EW / AC-IM — partgraph.embed

Specifies the behaviour of:
  - build_embed_text(part) -> str
  - generate_embeddings(texts, encoder, batch_size, expected_dim=384) -> list[list[float]]
  - embed_write(parts_iter, client, *, encoder, controller, sleep, progress)
  - get_encoder() + lazy-import contract

Design decisions pinned by PR4 plan:
  - Embed source of truth: GRAPH — write embedding by uid only.
  - embed_write payload keys MUST be exactly {"uid", "embedding"} subset.
  - xid-absent parts are skipped silently.
  - sentence-transformers is an OPTIONAL extra [embed]; must NOT be imported at
    module level. Unit tests MOCK the encoder; they must pass even when
    sentence_transformers is not installed.
  - get_encoder() lazy-imports; absent package -> ImportError naming
    "sentence-transformers" and 'pip install -e ".[embed]"'.
  - generate_embeddings: wrong output width -> ValueError naming 384.
  - 384-dimensional guard on all embedding paths.

NOTE: Collection will ERROR on import of partgraph.embed because that module
does not exist yet. That is the correct red state before PR4 implementation.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# --- Module under test (will be red until implementation exists) ---
from partgraph.embed import (  # noqa: F401
    build_embed_text,
    embed_write,
    generate_embeddings,
    get_encoder,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EMBED_DIM = 384


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_part(  # noqa: PLR0913
    *,
    uid: str = "0xABC",
    xid: str = "TESTPART|TESTMFR",
    description: str | None = "USB to RS-232 level converter",
    category: str | None = "Interface IC",
    package: str | None = "DIP-16",
    tags: list[str] | None = None,
) -> SimpleNamespace:
    """Return a minimal namespace that looks like a Part row (uid + text fields)."""
    return SimpleNamespace(
        uid=uid,
        xid=xid,
        description=description,
        category=category,
        package=package,
        tags=tags or ["rs232", "level-shifter"],
    )


def _fake_encoder(texts: list[str]) -> list[list[float]]:
    """Deterministic fake encoder returning (N, 384) all-zeros vectors."""
    return [[0.0] * _EMBED_DIM for _ in texts]


def _fake_encoder_wrong_dim(texts: list[str]) -> list[list[float]]:
    """Fake encoder returning (N, 4) vectors — wrong dimension."""
    return [[0.0] * 4 for _ in texts]


def _build_mock_txn(
    uid_response: dict | None = None,
    raise_on_query: Exception | None = None,
) -> MagicMock:
    """Build a mock Dgraph txn.

    uid_response: dict returned as resp.json bytes for xid->uid lookup.
    """
    mock_txn = MagicMock()
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)
    mock_txn.discard.return_value = None
    mock_txn.commit.return_value = None

    if raise_on_query is not None:
        mock_txn.query.side_effect = raise_on_query
    else:
        resp = MagicMock()
        resp.json = json.dumps(uid_response or {"q": []}).encode()
        mock_txn.query.return_value = resp

    mock_txn.mutate.return_value = MagicMock()
    return mock_txn


def _build_mock_client(txn: MagicMock | None = None) -> MagicMock:
    mock_client = MagicMock()
    t = txn if txn is not None else _build_mock_txn()
    mock_client.txn.return_value = t
    return mock_client


# ===========================================================================
# AC-ET: build_embed_text
# ===========================================================================

def test_ac_et_1_all_fields_present_deterministic_tags_sorted() -> None:
    """AC-ET-1: Given a part with description, category, package and two tags.
    When build_embed_text(part) is called.
    Then the result is a non-empty string containing all four fields, and the
    tags appear in sorted order (deterministic embedding input).
    """
    part = _make_fake_part(
        description="USB to RS-232 level converter",
        category="Interface IC",
        package="DIP-16",
        tags=["rs232", "level-shifter"],  # "level-shifter" < "rs232" alphabetically
    )
    text = build_embed_text(part)

    assert isinstance(text, str), f"build_embed_text must return str; got {type(text)}"
    assert "USB to RS-232 level converter" in text, (
        f"AC-ET-1: description must appear in embed text. Got: {text!r}"
    )
    assert "Interface IC" in text, (
        f"AC-ET-1: category must appear in embed text. Got: {text!r}"
    )
    assert "DIP-16" in text, (
        f"AC-ET-1: package must appear in embed text. Got: {text!r}"
    )
    # Both tags must appear.
    assert "rs232" in text, f"AC-ET-1: tag 'rs232' must appear in embed text. Got: {text!r}"
    assert "level-shifter" in text, (
        f"AC-ET-1: tag 'level-shifter' must appear in embed text. Got: {text!r}"
    )
    # Tags must appear in sorted order.
    idx_ls = text.index("level-shifter")
    idx_rs = text.index("rs232")
    assert idx_ls < idx_rs, (
        f"AC-ET-1: tags must be sorted; 'level-shifter' must precede 'rs232'. "
        f"Got: {text!r}"
    )


def test_ac_et_2_description_none_only_package_no_none_substring() -> None:
    """AC-ET-2: Given a part with description=None, category=None, package="SOT-23",
    and no tags.
    When build_embed_text(part) is called.
    Then:
    - The result is non-empty (at minimum contains the package name).
    - The string "None" does not appear anywhere (no Python None coercion).
    - No stray double-space or leading/trailing delimiter artefacts.
    """
    part = _make_fake_part(
        description=None,
        category=None,
        package="SOT-23",
        tags=[],
    )
    text = build_embed_text(part)

    assert text, "AC-ET-2: embed text must be non-empty when package is present."
    assert "None" not in text, (
        f"AC-ET-2: 'None' string must not appear in embed text. Got: {text!r}"
    )
    assert "SOT-23" in text, (
        f"AC-ET-2: package 'SOT-23' must appear in embed text. Got: {text!r}"
    )
    # No stray delimiter runs: double spaces, double colons, trailing separators.
    assert "  " not in text, (
        f"AC-ET-2: no double-space artefact allowed. Got: {text!r}"
    )


def test_ac_et_3_all_empty_returns_empty_string() -> None:
    """AC-ET-3: Given a part where ALL text fields are None/empty.
    When build_embed_text(part) is called.
    Then the result is the empty string "".
    """
    part = _make_fake_part(
        description=None,
        category=None,
        package=None,
        tags=[],
    )
    text = build_embed_text(part)

    assert text == "", (
        f"AC-ET-3: all-empty part must produce empty string. Got: {text!r}"
    )


def test_ac_et_4_determinism_byte_identical_on_repeated_calls() -> None:
    """AC-ET-4: Given the same part data.
    When build_embed_text is called twice.
    Then both results are byte-identical (deterministic — no random/timestamp element).
    """
    part = _make_fake_part(
        description="Capacitor 100nF",
        category="Passive",
        package="0402",
        tags=["decoupling", "bypass"],
    )
    text1 = build_embed_text(part)
    text2 = build_embed_text(part)

    assert text1 == text2, (
        f"AC-ET-4: build_embed_text must be deterministic. "
        f"Got different results: {text1!r} vs {text2!r}"
    )


# ===========================================================================
# AC-EG: generate_embeddings
# ===========================================================================

def test_ac_eg_1_fake_encoder_returns_n_times_384_no_sentence_transformers() -> None:
    """AC-EG-1: Given a fake encoder that returns (N, 384) vectors and a list of 3 texts.
    When generate_embeddings(texts, encoder=fake, batch_size=10) is called.
    Then:
    - Returns a list of 3 vectors, each of length 384.
    - sentence_transformers is NOT in sys.modules (lazy import contract).
    """
    texts = ["rs232 transceiver", "capacitor 100nF", "resistor 10k"]
    result = generate_embeddings(texts, encoder=_fake_encoder, batch_size=10)

    assert isinstance(result, list), f"generate_embeddings must return list; got {type(result)}"
    assert len(result) == 3, (
        f"AC-EG-1: expected 3 vectors for 3 texts; got {len(result)}"
    )
    for i, vec in enumerate(result):
        assert len(vec) == _EMBED_DIM, (
            f"AC-EG-1: vector {i} must be length {_EMBED_DIM}; got {len(vec)}"
        )

    # Lazy import contract: sentence_transformers must NOT be imported just by calling this.
    assert "sentence_transformers" not in sys.modules, (
        "AC-EG-1: generate_embeddings must not import sentence_transformers. "
        "The fake encoder was used — no real model needed."
    )


def test_ac_eg_2_wrong_encoder_width_raises_value_error_naming_384() -> None:
    """AC-EG-2: Given a fake encoder that returns (N, 4) vectors (wrong dim).
    When generate_embeddings(texts, encoder=wrong_encoder, batch_size=10) is called.
    Then a ValueError is raised whose message names 384 (the required dimension).
    """
    texts = ["some text"]
    with pytest.raises(ValueError, match="384"):
        generate_embeddings(texts, encoder=_fake_encoder_wrong_dim, batch_size=10)


def test_ac_eg_3_batching_order_preserved() -> None:
    """AC-EG-3: Given 5 texts and batch_size=2.
    When generate_embeddings is called.
    Then:
    - The encoder is called at most ceil(5/2)=3 times (batching).
    - The output order matches the input order.
    """
    call_log: list[list[str]] = []

    def _tracking_encoder(texts_batch: list[str]) -> list[list[float]]:
        call_log.append(list(texts_batch))
        return [[float(i)] * _EMBED_DIM for i in range(len(texts_batch))]

    texts = ["a", "b", "c", "d", "e"]
    result = generate_embeddings(texts, encoder=_tracking_encoder, batch_size=2)

    assert len(result) == 5, f"AC-EG-3: expected 5 vectors; got {len(result)}"
    assert len(call_log) <= 3, (
        f"AC-EG-3: batch_size=2, 5 texts -> at most 3 encoder calls; got {len(call_log)}"
    )
    # Verify order is preserved: the 5 vectors map to 5 positions.
    # Check that the combined batch inputs cover all 5 texts in order.
    combined_inputs = [t for batch in call_log for t in batch]
    assert combined_inputs == texts, (
        f"AC-EG-3: batch input order must preserve original text order. "
        f"Got combined: {combined_inputs!r}"
    )


def test_ac_eg_4_empty_texts_returns_empty_no_encoder_call() -> None:
    """AC-EG-4: Given an empty text list.
    When generate_embeddings([], encoder=..., batch_size=10) is called.
    Then the result is [] and the encoder is never called.
    """
    call_count = [0]

    def _counting_encoder(texts_batch: list[str]) -> list[list[float]]:
        call_count[0] += 1
        return [[0.0] * _EMBED_DIM for _ in texts_batch]

    result = generate_embeddings([], encoder=_counting_encoder, batch_size=10)

    assert result == [], (
        f"AC-EG-4: empty input must return []. Got: {result!r}"
    )
    assert call_count[0] == 0, (
        f"AC-EG-4: encoder must not be called for empty input; called {call_count[0]} time(s)."
    )


# ===========================================================================
# AC-EW: embed_write
# ===========================================================================

def _make_parts_with_uids(n: int) -> list[SimpleNamespace]:
    """Return n parts with pre-assigned xids and uids."""
    return [
        SimpleNamespace(
            uid=f"0x{100 + i:04x}",
            xid=f"PART{i:04d}|TESTMFR",
            description=f"Part {i}",
            category="IC",
            package="DIP-8",
            tags=[],
        )
        for i in range(n)
    ]


def _build_uid_lookup_response(parts: list[SimpleNamespace]) -> dict:
    """Build a fake Dgraph xid->uid lookup response."""
    return {
        "q": [
            {"uid": p.uid, "xid": p.xid}
            for p in parts
        ]
    }


def test_ac_ew_1_payload_keys_only_uid_and_embedding() -> None:
    """AC-EW-1: Given parts with full data.
    When embed_write is called with a fake encoder.
    Then every mutate payload object has ONLY keys from {"uid", "embedding"}
    — no mpn, description, made_by, stock, dgraph.type, xid or any other field.
    """
    parts = _make_parts_with_uids(2)
    uid_resp = _build_uid_lookup_response(parts)
    mock_txn = _build_mock_txn(uid_response=uid_resp)
    mock_client = _build_mock_client(mock_txn)

    embed_write(
        iter(parts),
        mock_client,
        encoder=_fake_encoder,
        sleep=MagicMock(),
    )

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "embed_write must call mutate() at least once."

    for call_obj in mutate_calls:
        _, kwargs = call_obj
        set_obj = kwargs.get("set_obj")
        if set_obj is None:
            set_json = kwargs.get("set_json")
            if set_json:
                set_obj = json.loads(
                    set_json.decode("utf-8") if isinstance(set_json, bytes) else set_json
                )

        items = set_obj if isinstance(set_obj, list) else [set_obj]
        for item in items:
            if not isinstance(item, dict):
                continue
            extra_keys = set(item.keys()) - {"uid", "embedding"}
            assert not extra_keys, (
                f"AC-EW-1: embed payload must ONLY have 'uid' and 'embedding'. "
                f"Found extra keys: {extra_keys!r} in item: {item!r}"
            )


def test_ac_ew_2_upsert_resolves_existing_uid_by_xid_absent_skipped() -> None:
    """AC-EW-2: Given parts where one has a known xid->uid mapping and one has no
    xid attribute (xid_absent).
    When embed_write is called.
    Then:
    - The known xid part is written using its resolved uid.
    - The xid-absent part is silently skipped — no mutation for it.
    """
    known_part = SimpleNamespace(
        uid="0x1234",
        xid="KNOWN|TESTMFR",
        description="Known part",
        category="IC",
        package="DIP-8",
        tags=[],
    )
    # xid-absent: no xid attribute at all.
    no_xid_part = SimpleNamespace(
        uid=None,
        description="No xid",
        category="IC",
        package="DIP-8",
        tags=[],
    )

    # Build lookup response that returns uid only for the known xid.
    uid_resp = {"q": [{"uid": "0x1234", "xid": "KNOWN|TESTMFR"}]}
    mock_txn = _build_mock_txn(uid_response=uid_resp)
    mock_client = _build_mock_client(mock_txn)

    embed_write(
        iter([known_part, no_xid_part]),
        mock_client,
        encoder=_fake_encoder,
        sleep=MagicMock(),
    )

    # The call to mutate must carry the known uid.
    mutate_calls = mock_txn.mutate.call_args_list
    written_uids: list[str] = []
    for call_obj in mutate_calls:
        _, kwargs = call_obj
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
                if isinstance(item, dict) and "uid" in item:
                    written_uids.append(item["uid"])

    assert "0x1234" in written_uids, (
        f"AC-EW-2: known part's uid '0x1234' must appear in mutate payload. "
        f"Written uids: {written_uids!r}"
    )
    # The no-xid part should not produce any write (it is skipped).
    assert "None" not in written_uids, (
        "AC-EW-2: xid-absent part must be skipped; None uid must not appear in payload."
    )


def test_ac_ew_3_extra_fields_on_part_still_only_uid_and_embedding_written() -> None:
    """AC-EW-3: Given a part with many extra fields (mpn, stock, etc.).
    When embed_write is called.
    Then the mutation payload still only contains uid+embedding.
    (Regression guard: extra fields from the graph read must not pollute writes.)
    """
    rich_part = SimpleNamespace(
        uid="0xBEEF",
        xid="RICH|TESTMFR",
        description="Rich part",
        category="IC",
        package="QFN-32",
        tags=["spi"],
        mpn="RICHPART1",
        mpn_norm="RICHPART1",
        stock=500,
        is_basic=False,
        made_by=[{"name": "RichMfr"}],
        dgraph_type="Part",
    )

    uid_resp = {"q": [{"uid": "0xBEEF", "xid": "RICH|TESTMFR"}]}
    mock_txn = _build_mock_txn(uid_response=uid_resp)
    mock_client = _build_mock_client(mock_txn)

    embed_write(
        iter([rich_part]),
        mock_client,
        encoder=_fake_encoder,
        sleep=MagicMock(),
    )

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "embed_write must call mutate for a part with a resolved uid."

    for call_obj in mutate_calls:
        _, kwargs = call_obj
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
                if isinstance(item, dict):
                    extra_keys = set(item.keys()) - {"uid", "embedding"}
                    assert not extra_keys, (
                        f"AC-EW-3: payload must ONLY have uid+embedding regardless of rich "
                        f"part data. Extra keys: {extra_keys!r}"
                    )


def test_ac_ew_4_empty_embed_text_skipped_no_mutation_for_it() -> None:
    """AC-EW-4: Given a part that produces empty embed text (all fields None/empty).
    When embed_write is called.
    Then that part is skipped — no mutation entry for it.
    (Build embed text returns "" -> skip, count as skipped.)
    """
    empty_part = SimpleNamespace(
        uid="0xDEAD",
        xid="EMPTY|TESTMFR",
        description=None,
        category=None,
        package=None,
        tags=[],
    )
    good_part = SimpleNamespace(
        uid="0xGOOD",
        xid="GOOD|TESTMFR",
        description="Good part",
        category="IC",
        package="DIP-8",
        tags=[],
    )

    uid_resp = {
        "q": [
            {"uid": "0xDEAD", "xid": "EMPTY|TESTMFR"},
            {"uid": "0xGOOD", "xid": "GOOD|TESTMFR"},
        ]
    }
    mock_txn = _build_mock_txn(uid_response=uid_resp)
    mock_client = _build_mock_client(mock_txn)

    embed_write(
        iter([empty_part, good_part]),
        mock_client,
        encoder=_fake_encoder,
        sleep=MagicMock(),
    )

    # Collect all written uids.
    written_uids: set[str] = set()
    for call_obj in mock_txn.mutate.call_args_list:
        _, kwargs = call_obj
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
                if isinstance(item, dict) and "uid" in item:
                    written_uids.add(item["uid"])

    assert "0xDEAD" not in written_uids, (
        f"AC-EW-4: empty-embed-text part must be skipped. "
        f"uid '0xDEAD' must not appear in mutate payload. Written: {written_uids!r}"
    )
    assert "0xGOOD" in written_uids, (
        f"AC-EW-4: good part must still be written. "
        f"uid '0xGOOD' must appear in mutate payload. Written: {written_uids!r}"
    )


# ===========================================================================
# AC-IM: lazy import
# ===========================================================================

def test_ac_im_1_get_encoder_raises_import_error_when_sentence_transformers_absent() -> None:
    """AC-IM-1: Given sentence_transformers is NOT installed (patched to raise ImportError).
    When get_encoder() is called.
    Then:
    - ImportError is raised.
    - The error message names "sentence-transformers" (the package name).
    - The error message contains the install hint including "[embed]".
    - No filesystem path leaks into the error message.
    """
    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else None

    def _blocking_import(name: str, *args, **kwargs):
        if name.startswith("sentence_transformers") or name == "sentence_transformers":
            raise ImportError("No module named 'sentence_transformers'")
        return original_import(name, *args, **kwargs) if original_import else __import__(name, *args, **kwargs)

    # Use patch to make sentence_transformers unavailable at import/call time.
    with patch.dict(sys.modules, {"sentence_transformers": None}), \
         pytest.raises(ImportError) as exc_info:
        get_encoder()

    msg = str(exc_info.value)
    assert "sentence-transformers" in msg, (
        f"AC-IM-1: ImportError must name 'sentence-transformers'. Got: {msg!r}"
    )
    assert "[embed]" in msg or "embed" in msg, (
        f"AC-IM-1: ImportError must mention the [embed] extra. Got: {msg!r}"
    )
    # No path leak — message must not contain "/" or "site-packages" or "home".
    assert "/home/" not in msg, (
        f"AC-IM-1: error message must not contain path leak '/home/'. Got: {msg!r}"
    )
    assert "site-packages" not in msg, (
        f"AC-IM-1: error message must not leak 'site-packages'. Got: {msg!r}"
    )


def test_ac_im_2_import_embed_module_does_not_import_sentence_transformers() -> None:
    """AC-IM-2: Given partgraph.embed is imported.
    When the module import completes.
    Then sentence_transformers is NOT present in sys.modules.
    (Top-level import of partgraph.embed must be cheap — no model loading.)
    """
    # Remove sentence_transformers from sys.modules if it somehow crept in.
    sys.modules.pop("sentence_transformers", None)

    # Re-importing the module must not trigger sentence_transformers loading.
    import importlib
    import partgraph.embed as embed_mod
    importlib.reload(embed_mod)

    assert "sentence_transformers" not in sys.modules, (
        "AC-IM-2: importing partgraph.embed must NOT import sentence_transformers. "
        "Use lazy import inside get_encoder() only."
    )
