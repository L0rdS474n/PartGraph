"""
Tests: AC-RE-3, AC-RE-4, AC-RE-5, AC-RE-6, AC-RE-7, AC-RE-8, AC-RE-11,
AC-RE-13, AC-RE-14 — `partgraph embed --changed` (issue #11, PR 4:
incremental re-embedding; ADR-0015).

Specifies the behaviour of the NEW reconcile pass added to the `embed`
command: `_select_parts_for_reembed` (a READ-ONLY selection over
``@filter(has(embedding))``) and `_reembed_all_pages` (the cursor-paged
reconcile loop). Neither exists yet — this is the correct red state before
implementation.

D2 four-case reconcile contract (precondition: build_embed_text=="" -> skip
entirely, no encoder call, no write):
  (i)   no embedding            -> embed + stamp {uid, embedding,
        embed_text_hash} (covered by the EXISTING missing pass + the updated
        embed_write payload contract; see test_embed.py AC-RE-1/AC-RE-2 and
        the updated AC-EW-1/AC-EW-3/AC-EC-6 pins).
  (ii)  embedding, no stored hash    -> backfill {uid, embed_text_hash} ONLY
        (encoder NOT called, embedding untouched).                    AC-RE-3
  (iii) embedding, stored hash == current -> skip (no mutate, no encoder).
                                                                        AC-RE-4
  (iv)  embedding, stored hash != current -> re-embed + re-stamp {uid,
        embedding, embed_text_hash} (encoder CALLED, new vector + hash).
                                                                        AC-RE-5

D3: `embed --changed` runs the reconcile pass FIRST
(`_select_parts_for_reembed` + `_reembed_all_pages`), THEN the existing
missing pass (`NOT has(embedding)` + `_embed_all_pages`, unchanged). Plain
`embed` / `embed --limit N` (no --changed) is unaffected.               AC-RE-13

D4: embed_text_hash is sha256; the predicate is INDEX-FREE — comparison is
CLIENT-SIDE in Python, never a DQL `eq(embed_text_hash, ...)` filter and
never a literal hash value embedded in query text.                      AC-RE-7

Mock-helper reuse: this file imports the mock-client/txn helpers from
test_cli_embed.py (a sibling test module in the SAME `tests.unit` package)
rather than redefining them, per the planner contract.

NOTE ON RED STATE: every top-level import in this file resolves against
code that ALREADY EXISTS today (partgraph.cli:app, partgraph.embed:
build_embed_text, and test_cli_embed.py's helpers) — so this file collects
cleanly. The NOT-YET-IMPLEMENTED symbols
(`cli_mod._select_parts_for_reembed`, `cli_mod._reembed_all_pages`, the
`--changed` CLI flag) are only ever referenced INSIDE test function bodies,
so a missing symbol fails that one test at run time (AttributeError / a
Typer "no such option" usage error) rather than erroring collection of the
whole file.

Page-size assumption (flagged per-test where relied upon): a handful of
multi-page reconcile tests patch `cli_mod._EMBED_SELECT_PAGE_SIZE` to force
a page boundary, assuming the reconcile pass shares this constant with the
existing missing pass — both live inside the SAME embed pipeline (unlike the
fully-decoupled `partgraph.refresh.*` leaves, which each keep their own
copy). If the implementation introduces a separate reconcile-only page-size
constant instead, only those specific tests need their patch target renamed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

os.environ["COLUMNS"] = "200"

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from partgraph.embed import build_embed_text  # noqa: E402

from .test_cli_embed import (  # noqa: E402
    _ANSI_RE,
    _FAKE_VECTOR,
    _invoke,
    _make_direct_call_controller,
    _make_mock_client,
    _make_mock_parts_txn,
    _make_paged_mock_client,
    _make_write_txn,
    _patch_dgraph,
    _patch_get_encoder,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _oracle_hash(text: str) -> str:
    """Return the sha256 hex-digest oracle for *text*.

    Computed directly via hashlib — independent of the production
    `compute_embed_text_hash` (which is exercised by its own dedicated tests
    in test_embed.py) — so this file's expectations are never tautologically
    derived from the function under test.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_fake_part(
    *,
    description: str | None = None,
    category: str | None = None,
    package: str | None = None,
    tags: list[str] | None = None,
) -> SimpleNamespace:
    """Return a minimal namespace exposing exactly the attributes
    build_embed_text reads, mirroring test_embed.py's own fixture shape.
    """
    return SimpleNamespace(
        description=description, category=category, package=package, tags=tags or [],
    )


def _reembed_row(  # noqa: PLR0913 — one keyword per build_embed_text source field + the stored hash.
    uid: str,
    *,
    description: str | None = None,
    category: str | None = None,
    package: str | None = None,
    tags: list[str] | None = None,
    stored_hash: str | None = None,
) -> dict:
    """Return a raw Dgraph-JSON row shaped like the reconcile selection
    query's expected response: uid + the build_embed_text source fields,
    mirroring `_select_parts_for_embed`'s in_category/in_package/tagged
    parsing convention, plus an optional stored embed_text_hash (omitted ->
    case ii, "no stored hash").
    """
    row: dict = {
        "uid": uid,
        "description": description,
        "in_category": [{"name": category}] if category else [],
        "in_package": [{"name": package}] if package else [],
        "tagged": [{"name": t} for t in (tags or [])],
    }
    if stored_hash is not None:
        row["embed_text_hash"] = stored_hash
    return row


def _skip_row(uid: str, text: str) -> dict:
    """Return a reconcile row whose stored embed_text_hash matches the
    CURRENT oracle hash of *text* — a guaranteed case (iii) "skip" row (no
    mutate, no encoder call). Used where a test only needs pagination
    behaviour, not a specific ii/iii/iv outcome.
    """
    return _reembed_row(
        uid,
        description=text,
        stored_hash=_oracle_hash(build_embed_text(_make_fake_part(description=text))),
    )


def _make_reconcile_read_txn(pages: list[dict]) -> MagicMock:
    """Serve *pages*, in order, to has(embedding)-rooted reconcile selection
    queries.

    Mirrors test_cli_embed._make_cursor_aware_read_txn but recognises the
    reconcile query shape (`type(Part)` + `first:` + `has(embedding)`)
    instead of the missing-pass's `NOT has(embedding)`. Any other query
    (e.g. an xid-resolution lookup, if the implementation issues one)
    degrades to an empty match set — a deterministic, side-effect-free
    stand-in that never consumes a reconcile page.
    """
    remaining_pages = iter(pages)
    empty_resolve = MagicMock()
    empty_resolve.json = json.dumps({"q": []}).encode()

    def _query_side_effect(query_text, *args, **kwargs):
        if (
            "type(Part)" in query_text
            and "first:" in query_text
            and "has(embedding)" in query_text
        ):
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


def _reconcile_query_calls(read_txn: MagicMock) -> list[str]:
    """Return the query text of every reconcile (has(embedding)) selection
    call made on *read_txn*.
    """
    return [
        c.args[0] for c in read_txn.query.call_args_list
        if c.args and "type(Part)" in c.args[0] and "first:" in c.args[0]
        and "has(embedding)" in c.args[0]
    ]


def _mutate_items_for_uid(write_txn: MagicMock, uid: str) -> list[dict]:
    """Return every mutation item across all of write_txn's mutate() calls
    whose "uid" equals *uid*.
    """
    return [
        item
        for c_obj in write_txn.mutate.call_args_list
        for item in (c_obj.kwargs.get("set_obj") or [])
        if isinstance(item, dict) and item.get("uid") == uid
    ]


def _make_counting_encoder() -> MagicMock:
    """Return a MagicMock encoder producing valid EMBED_DIM vectors —
    usable with assert_called/assert_not_called/call_count while still
    satisfying generate_embeddings' output-width guard.
    """
    def _encode(texts: list[str]) -> list[list[float]]:
        return [list(_FAKE_VECTOR) for _ in texts]

    return MagicMock(side_effect=_encode)


# ===========================================================================
# AC-RE-3 (D2 case ii): embedding present, no stored hash -> narrow backfill.
# ===========================================================================

def test_ac_re_3_case_ii_backfill_narrow_mutation_no_embedding_key_encoder_not_called() -> None:
    """AC-RE-3: Given a Part that already HAS an embedding but carries NO
    stored embed_text_hash.
    When the reconcile pass (`_reembed_all_pages`) processes it.
    Then it stages a NARROW backfill mutation {uid, embed_text_hash} for
    that uid — the hash equals the sha256 oracle of its current
    build_embed_text — the embedding is left untouched (no "embedding" key
    in that mutation), and the encoder is never called.
    """
    import partgraph.cli as cli_mod

    part_text = build_embed_text(
        _make_fake_part(description="RS-232 transceiver", category="Interface IC", package="DIP-16")
    )
    page1 = {"q": [
        _reembed_row(
            "0xAA01", description="RS-232 transceiver", category="Interface IC", package="DIP-16",
        ),  # no embed_text_hash key at all -> case ii
    ]}
    read_txn = _make_reconcile_read_txn([page1])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)
    encoder = _make_counting_encoder()
    controller = _make_direct_call_controller()

    cli_mod._reembed_all_pages(
        mock_client, encoder=encoder, controller=controller, remaining=1,
        progress_bar=MagicMock(),
    )

    encoder.assert_not_called()
    matching_items = _mutate_items_for_uid(write_txn, "0xAA01")
    assert matching_items, "AC-RE-3: no mutation item found for uid 0xAA01."
    for item in matching_items:
        assert "embedding" not in item, (
            f"AC-RE-3: backfill mutation must NOT touch 'embedding'. Got: {item!r}"
        )
        assert set(item.keys()) == {"uid", "embed_text_hash"}, (
            f"AC-RE-3: backfill mutation must be EXACTLY {{uid, embed_text_hash}}. "
            f"Got keys: {set(item.keys())!r}"
        )
        assert item["embed_text_hash"] == _oracle_hash(part_text), (
            f"AC-RE-3: embed_text_hash must equal sha256(current build_embed_text). "
            f"Expected {_oracle_hash(part_text)!r}, got {item['embed_text_hash']!r}"
        )


# ===========================================================================
# AC-RE-4 (D2 case iii): stored hash == current -> skip entirely.
# ===========================================================================

def test_ac_re_4_case_iii_skip_when_stored_hash_matches_current_no_mutate_no_encoder() -> None:
    """AC-RE-4: Given a Part whose stored embed_text_hash already equals the
    sha256 of its CURRENT build_embed_text (nothing changed since it was
    last stamped).
    When the reconcile pass processes it.
    Then no mutation is issued for the whole run and the encoder is never
    called.
    """
    import partgraph.cli as cli_mod

    text = build_embed_text(
        _make_fake_part(description="Capacitor 100nF", category="Passive", package="0402")
    )
    current_hash = _oracle_hash(text)
    page1 = {"q": [
        _reembed_row(
            "0xBB01", description="Capacitor 100nF", category="Passive", package="0402",
            stored_hash=current_hash,
        ),
    ]}
    read_txn = _make_reconcile_read_txn([page1])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)
    encoder = _make_counting_encoder()
    controller = _make_direct_call_controller()

    cli_mod._reembed_all_pages(
        mock_client, encoder=encoder, controller=controller, remaining=1,
        progress_bar=MagicMock(),
    )

    encoder.assert_not_called()
    write_txn.mutate.assert_not_called()


# ===========================================================================
# AC-RE-5 (D2 case iv): stored hash != current -> re-embed + re-stamp.
# ===========================================================================

def test_ac_re_5_case_iv_reembed_on_hash_mismatch_new_vector_and_hash_encoder_called() -> None:
    """AC-RE-5: Given a Part whose stored embed_text_hash does NOT match the
    sha256 of its CURRENT build_embed_text (the source text changed since
    the last embed).
    When the reconcile pass processes it.
    Then the encoder IS called, and the mutation for that uid is EXACTLY
    {uid, embedding, embed_text_hash} with a NEW hash equal to the current
    oracle (and different from the stale stored one).
    """
    import partgraph.cli as cli_mod

    stale_hash = _oracle_hash("some stale text that no longer matches")
    current_text = build_embed_text(
        _make_fake_part(description="Updated description", category="IC", package="SOIC-8")
    )
    page1 = {"q": [
        _reembed_row(
            "0xCC01", description="Updated description", category="IC", package="SOIC-8",
            stored_hash=stale_hash,
        ),
    ]}
    read_txn = _make_reconcile_read_txn([page1])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)
    encoder = _make_counting_encoder()
    controller = _make_direct_call_controller()

    cli_mod._reembed_all_pages(
        mock_client, encoder=encoder, controller=controller, remaining=1,
        progress_bar=MagicMock(),
    )

    encoder.assert_called()
    matching_items = _mutate_items_for_uid(write_txn, "0xCC01")
    assert matching_items, "AC-RE-5: no mutation item found for uid 0xCC01."
    for item in matching_items:
        assert set(item.keys()) == {"uid", "embedding", "embed_text_hash"}, (
            f"AC-RE-5: re-embed mutation must be EXACTLY {{uid, embedding, "
            f"embed_text_hash}}. Got: {set(item.keys())!r}"
        )
        assert item["embed_text_hash"] == _oracle_hash(current_text), (
            f"AC-RE-5: embed_text_hash must equal the CURRENT sha256 oracle. "
            f"Expected {_oracle_hash(current_text)!r}, got {item['embed_text_hash']!r}"
        )
        assert item["embed_text_hash"] != stale_hash, (
            "AC-RE-5: the re-stamped hash must differ from the stale stored one."
        )
        assert isinstance(item["embedding"], str) and item["embedding"].startswith("["), (
            f"AC-RE-5: embedding must be the Dgraph vector STRING literal. "
            f"Got: {item['embedding']!r}"
        )


# ===========================================================================
# AC-RE-6: empty embed text -> skip entirely, in ANY hash state.
# ===========================================================================

@pytest.mark.parametrize(
    "stored_hash",
    [None, _oracle_hash(""), "0" * 64],
    ids=["no_stored_hash", "hash_of_empty_text", "arbitrary_stale_hash"],
)
def test_ac_re_6_empty_embed_text_skipped_regardless_of_hash_state(stored_hash) -> None:
    """AC-RE-6: Given a Part whose CURRENT build_embed_text is "" (a
    whitespace-only description, no category/package, no tags — AC-ET-3),
    in any hash state (no stored hash / a hash of "" / an arbitrary stale
    hash).
    When the reconcile pass processes it.
    Then it is skipped entirely: no mutation is issued and the encoder is
    never called — the empty-text precondition is checked BEFORE the
    ii/iii/iv case split.
    """
    import partgraph.cli as cli_mod

    page1 = {"q": [
        _reembed_row("0xDD01", description="   ", category=None, package=None, tags=[],
                     stored_hash=stored_hash),
    ]}
    read_txn = _make_reconcile_read_txn([page1])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)
    encoder = _make_counting_encoder()
    controller = _make_direct_call_controller()

    cli_mod._reembed_all_pages(
        mock_client, encoder=encoder, controller=controller, remaining=1,
        progress_bar=MagicMock(),
    )

    encoder.assert_not_called()
    write_txn.mutate.assert_not_called()


# ===========================================================================
# AC-RE-7: reconcile selection is client-side — no hash VALUE ever reaches
# the DQL query text; comparison happens in Python after the read.
# ===========================================================================

def test_ac_re_7_selection_query_never_embeds_a_hash_value() -> None:
    """AC-RE-7: Given the reconcile selection query, built for a first page
    and for a subsequent cursor-bearing page.
    When `_select_parts_for_reembed` builds each query.
    Then the query text NEVER embeds a 64-hex-char hash VALUE and never
    filters via `eq(embed_text_hash, ...)` — the reconcile pass compares a
    row's stored hash against a freshly computed one in Python, never in
    DQL.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_reconcile_read_txn([{"q": []}, {"q": []}])
    mock_client = _make_paged_mock_client(read_txn, _make_write_txn())

    cli_mod._select_parts_for_reembed(mock_client, 10)
    cli_mod._select_parts_for_reembed(mock_client, 10, after="0xAB12")

    all_queries = [c.args[0] for c in read_txn.query.call_args_list if c.args]
    assert all_queries, "AC-RE-7: _select_parts_for_reembed must issue at least one query."
    for query_text in all_queries:
        assert not re.search(r"\b[0-9a-f]{64}\b", query_text), (
            f"AC-RE-7: no 64-hex-char hash VALUE may appear in the reconcile "
            f"selection query — comparison is client-side. Got: {query_text!r}"
        )
        assert "eq(embed_text_hash" not in query_text, (
            f"AC-RE-7: the selection query must never filter via "
            f"eq(embed_text_hash, ...). Got: {query_text!r}"
        )


def test_ac_re_7b_selection_query_projects_embed_text_hash_and_text_fields() -> None:
    """AC-RE-7b (Gate 3 MUST — silent-failure guard): Given the reconcile
    selection query.
    When `_select_parts_for_reembed` builds it.
    Then the query text PROJECTS `embed_text_hash` (so the reconcile pass
    can actually read the stored hash to compare against the freshly
    computed one) AND the build_embed_text source fields (`description`,
    `in_category`, `in_package`, `tagged`) — the hash is recomputed
    client-side FROM these, so they must be requested too.

    Silent-failure rationale: if the implementation forgets to project
    embed_text_hash, the server never returns it, every row would look like
    case (ii) (no stored hash — backfill only), and case (iv) (re-embed on
    genuine drift — the entire point of `--changed`) would NEVER fire. Yet
    every OTHER AC-RE-* test could still pass, because they control the
    mocked response's fields directly rather than deriving them from the
    query text. This test is the guard against exactly that silent failure
    — mirrors how test_embed_selection_default_is_paged_below_grpc_receive_
    limit (test_cli_embed.py) pins real query-text substrings for the
    missing pass.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_reconcile_read_txn([{"q": []}])
    mock_client = _make_paged_mock_client(read_txn, _make_write_txn())

    cli_mod._select_parts_for_reembed(mock_client, 10)

    query_text = read_txn.query.call_args.args[0]
    assert "uid" in query_text, (
        f"AC-RE-7b: the reconcile query must project 'uid' (needed for the "
        f"cursor and the write-back). Got: {query_text!r}"
    )
    assert "embed_text_hash" in query_text, (
        f"AC-RE-7b: the reconcile query must PROJECT 'embed_text_hash' — "
        f"otherwise every row silently looks like case (ii) and case (iv) "
        f"(re-embed on drift) never fires. Got: {query_text!r}"
    )
    for field in ("description", "in_category", "in_package", "tagged"):
        assert field in query_text, (
            f"AC-RE-7b: the reconcile query must project {field!r} — a "
            f"build_embed_text source field the hash is recomputed "
            f"client-side from. Got: {query_text!r}"
        )


# ===========================================================================
# AC-RE-8: pagination (uid keyset cursor) for the reconcile pass.
# ===========================================================================

def test_ac_re_8_select_parts_for_reembed_omits_after_by_default() -> None:
    """AC-RE-8: Given no prior page (the first page, no cursor).
    When `_select_parts_for_reembed` is called without an `after` argument.
    Then the query text contains NO 'after:' clause, and it filters on
    has(embedding).
    """
    import partgraph.cli as cli_mod

    read_txn = _make_reconcile_read_txn([{"q": []}])
    mock_client = _make_paged_mock_client(read_txn, _make_write_txn())

    cli_mod._select_parts_for_reembed(mock_client, None)

    query_text = read_txn.query.call_args.args[0]
    assert "after:" not in query_text, (
        f"AC-RE-8: the first page must omit 'after:' entirely. Got: {query_text!r}"
    )
    assert "has(embedding)" in query_text, (
        f"AC-RE-8: the reconcile selection must filter on has(embedding). "
        f"Got: {query_text!r}"
    )


def test_ac_re_8_select_parts_for_reembed_includes_after_cursor_when_provided() -> None:
    """AC-RE-8: Given a prior page's max uid.
    When `_select_parts_for_reembed` is called with the keyword-only
    `after=` cursor.
    Then the query text contains 'after: <uid>' and still carries the
    has(embedding) filter.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_reconcile_read_txn([{"q": []}])
    mock_client = _make_paged_mock_client(read_txn, _make_write_txn())

    cli_mod._select_parts_for_reembed(mock_client, 10, after="0xB002")

    query_text = read_txn.query.call_args.args[0]
    assert "after: 0xB002" in query_text, (
        f"AC-RE-8: passing after='0xB002' must add 'after: 0xB002'. Got: {query_text!r}"
    )
    assert "has(embedding)" in query_text, (
        f"AC-RE-8: has(embedding) filter must remain when paging with a cursor. "
        f"Got: {query_text!r}"
    )


def test_ac_re_8_select_parts_for_reembed_rejects_malformed_after_at_function_level() -> None:
    """AC-RE-8 (Gate 3 SHOULD — security F2, function-level): Given an
    injection-shaped, malformed `after` value passed DIRECTLY to
    `_select_parts_for_reembed` — not merely produced internally by the
    pagination loop (that transitive path is already covered by
    test_ac_re_8_malformed_or_missing_uid_never_used_as_cursor).
    When the query is built.
    Then the query text omits 'after:' entirely (validate-before-interpolate
    enforced at the function boundary itself) and the raw malformed value
    never appears anywhere in the query text.
    """
    import partgraph.cli as cli_mod

    malformed_after = '0x1) } injection { set { _:x <bad> "1" . } } #'
    read_txn = _make_reconcile_read_txn([{"q": []}])
    mock_client = _make_paged_mock_client(read_txn, _make_write_txn())

    cli_mod._select_parts_for_reembed(mock_client, 10, after=malformed_after)

    query_text = read_txn.query.call_args.args[0]
    assert "after:" not in query_text, (
        f"AC-RE-8: a malformed after= value passed directly to "
        f"_select_parts_for_reembed must be rejected at the function level "
        f"— the query must omit 'after:' entirely rather than interpolate "
        f"it. Got: {query_text!r}"
    )
    assert malformed_after not in query_text, (
        f"AC-RE-8: the raw malformed after= value must NEVER appear in the "
        f"query text (injection risk). Got: {query_text!r}"
    )


def test_ac_re_8_after_is_keyword_only_defaults_none() -> None:
    """AC-RE-8: Given `_select_parts_for_reembed`'s signature.
    Then it accepts an `after` parameter that is keyword-only and defaults
    to None — mirrors `_select_parts_for_embed`'s cursor contract exactly.
    """
    import inspect

    import partgraph.cli as cli_mod

    sig = inspect.signature(cli_mod._select_parts_for_reembed)
    assert "after" in sig.parameters, (
        "AC-RE-8: _select_parts_for_reembed must accept an 'after' parameter."
    )
    assert sig.parameters["after"].kind == inspect.Parameter.KEYWORD_ONLY, (
        f"AC-RE-8: 'after' must be keyword-only. Got: {sig.parameters['after'].kind!r}"
    )
    assert sig.parameters["after"].default is None, (
        "AC-RE-8: 'after' must default to None (page 1 has no cursor)."
    )


def test_ac_re_8_cursor_advances_across_two_full_pages_no_row_reprocessed() -> None:
    """AC-RE-8: Given two full reconcile pages of 2 rows each (case
    iii/skip, for simplicity — every row's stored hash already matches its
    current text) and `remaining=4` (exactly 2 pages worth, so the loop
    terminates via the `remaining` bound reaching 0 rather than an
    empty/short page).
    When `_reembed_all_pages` runs.
    Then page 1's query has NO 'after:'; page 2's query carries
    'after: <page 1's max uid>'; and exactly 2 selection queries occur — no
    row from page 1 is ever re-fetched.
    """
    import partgraph.cli as cli_mod

    page1 = {"q": [_skip_row("0xE001", "Widget E1"), _skip_row("0xE002", "Widget E2")]}
    page2 = {"q": [_skip_row("0xE003", "Widget E3"), _skip_row("0xE004", "Widget E4")]}

    read_txn = _make_reconcile_read_txn([page1, page2])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)
    encoder = _make_counting_encoder()
    controller = _make_direct_call_controller()

    cli_mod._reembed_all_pages(
        mock_client, encoder=encoder, controller=controller, remaining=4,
        progress_bar=MagicMock(),
    )

    queries = _reconcile_query_calls(read_txn)
    assert len(queries) == 2, (
        f"AC-RE-8: expected exactly 2 reconcile selection queries (2 full "
        f"pages of 2, remaining=4 hits 0) — no 3rd fetch, no re-processing. "
        f"Got {len(queries)}: {queries!r}"
    )
    assert "after:" not in queries[0], f"AC-RE-8: page 1 must omit 'after:'. Got: {queries[0]!r}"
    assert "after: 0xE002" in queries[1], (
        f"AC-RE-8: page 2 must carry 'after: 0xE002' (page 1's max uid). "
        f"Got: {queries[1]!r}"
    )
    encoder.assert_not_called()
    write_txn.mutate.assert_not_called()


def test_ac_re_8_short_page_terminates_without_extra_fetch() -> None:
    """AC-RE-8: Given a single reconcile page shorter than `remaining` (1
    row where remaining=5).
    When `_reembed_all_pages` runs.
    Then exactly 1 selection query occurs — a page shorter than requested
    is itself sufficient reason to stop, with no further fetch.
    """
    import partgraph.cli as cli_mod

    page1 = {"q": [_skip_row("0xF001", "Widget F1")]}
    read_txn = _make_reconcile_read_txn([page1])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)
    encoder = _make_counting_encoder()
    controller = _make_direct_call_controller()

    cli_mod._reembed_all_pages(
        mock_client, encoder=encoder, controller=controller, remaining=5,
        progress_bar=MagicMock(),
    )

    queries = _reconcile_query_calls(read_txn)
    assert len(queries) == 1, (
        f"AC-RE-8: a page shorter than the requested count must terminate "
        f"the run WITHOUT an extra fetch. Got {len(queries)}: {queries!r}"
    )


def test_ac_re_8_defensive_guard_stall_notice_on_non_advancing_cursor(capsys) -> None:
    """AC-RE-8: Given a mocked server that re-serves the SAME full page (and
    therefore the same max uid) on reconcile page 2 — a stalled cursor.

    ASSUMPTION (documented in the module docstring): the reconcile pass
    shares `_EMBED_SELECT_PAGE_SIZE` with the existing missing pass. If a
    separate reconcile-only page-size constant is introduced instead, this
    patch target must be renamed to match.

    When `_reembed_all_pages` runs (remaining=4, page size patched to 2).
    Then the non-advancing-cursor defensive guard fires: an explicit,
    path-free stall notice is printed to stdout, and the loop stops after
    exactly 2 selection queries (never a 3rd, never a sticky loop).
    """
    import partgraph.cli as cli_mod

    same_full_page = {"q": [_skip_row("0xE001", "Same E1"), _skip_row("0xE002", "Same E2")]}
    read_txn = _make_reconcile_read_txn([same_full_page, same_full_page])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)
    encoder = _make_counting_encoder()
    controller = _make_direct_call_controller()

    with patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 2):
        cli_mod._reembed_all_pages(
            mock_client, encoder=encoder, controller=controller, remaining=4,
            progress_bar=MagicMock(),
        )

    queries = _reconcile_query_calls(read_txn)
    assert len(queries) == 2, (
        f"AC-RE-8: the defensive guard must stop after exactly 2 selection "
        f"calls (page 1 sets the cursor; page 2 sees it hasn't advanced and "
        f"breaks). Got {len(queries)}: {queries!r}"
    )

    captured_out = _ANSI_RE.sub("", capsys.readouterr().out)
    stall_phrases = (
        "did not advance", "not advance", "stopping early", "stopped early",
        "no further progress", "cursor did not move", "cursor stalled",
    )
    assert any(phrase in captured_out.lower() for phrase in stall_phrases), (
        f"AC-RE-8: the stall guard must print an explicit notice (e.g. "
        f"'cursor did not advance' / 'stopping early'). Got:\n{captured_out!r}"
    )
    # Path-freeness via regex (never a literal path fragment in this file's
    # own source) — mirrors test_cli_embed.py's F4 stall-notice test exactly.
    assert not re.search(r"/(?:home|root|Users)/", captured_out), (
        "AC-RE-8: the stall notice must be path-free (no operator absolute path)."
    )


def test_ac_re_8_malformed_or_missing_uid_never_used_as_cursor() -> None:
    """AC-RE-8 (validate-before-interpolate): Given reconcile rows with an
    invalid "uid" — one missing the field entirely and one shaped like a
    DQL-injection payload — alongside one genuinely valid uid, followed by
    an empty terminating page.

    ASSUMPTION (see module docstring): reconcile shares
    `_EMBED_SELECT_PAGE_SIZE`; `remaining=None` mirrors `_embed_all_pages`'s
    unbounded contract so the loop continues past the first (full,
    patched-size) page to the empty terminator.

    When the reconcile pass pages past this block.
    Then neither invalid value is EVER interpolated raw into a subsequent
    query (no literal 'after: None' cursor either), and if a 2nd query is
    issued its cursor is derived from the one valid uid only.
    """
    import partgraph.cli as cli_mod

    malformed_uid = '0x1) } mutation { set { _:x <bad> "1" . } } #'
    page1 = {"q": [
        {"description": "Missing uid field entirely"},  # no "uid" key at all
        {"uid": malformed_uid},
        _skip_row("0xB002", "Widget B2"),
    ]}
    page2_empty = {"q": []}

    read_txn = _make_reconcile_read_txn([page1, page2_empty])
    write_txn = _make_write_txn()
    mock_client = _make_paged_mock_client(read_txn, write_txn)
    encoder = _make_counting_encoder()
    controller = _make_direct_call_controller()

    with patch.object(cli_mod, "_EMBED_SELECT_PAGE_SIZE", 3):
        cli_mod._reembed_all_pages(
            mock_client, encoder=encoder, controller=controller, remaining=None,
            progress_bar=MagicMock(),
        )

    all_queries = [c.args[0] for c in read_txn.query.call_args_list if c.args]
    for query_text in all_queries:
        assert malformed_uid not in query_text, (
            f"AC-RE-8: a malformed uid must NEVER be interpolated raw into a "
            f"DQL query (injection risk). Found it in: {query_text!r}"
        )
        assert "after: None" not in query_text, (
            f"AC-RE-8: a missing uid (None) must never be rendered as a "
            f"literal cursor. Found it in: {query_text!r}"
        )

    queries = _reconcile_query_calls(read_txn)
    if len(queries) >= 2:  # noqa: PLR2004
        assert "after: 0xB002" in queries[1], (
            f"AC-RE-8: cursor computation must skip both the missing and "
            f"malformed uids and use the one valid uid (0xB002). "
            f"Got: {queries[1]!r}"
        )


# ===========================================================================
# AC-RE-11: reconcile DB failure -> exit 1, path-free hint, no raw leak.
# ===========================================================================

def test_ac_re_11_reconcile_db_failure_exits_nonzero_path_free_hints_db_up() -> None:
    """AC-RE-11: Given the reconcile selection txn raises (DB down/refused).
    When `partgraph embed --changed` is invoked.
    Then exit code is non-zero, the output contains a path-free hint to run
    `partgraph db up`, the raw exception text never leaks into output, and
    (Gate 3 SHOULD — security F3) no operator absolute path leaks either,
    checked via regex rather than a literal substring (mirrors AC-EC-8's /
    AC-RE-8's stall-notice path-free assertion).
    """
    failing_txn = MagicMock()
    failing_txn.query.side_effect = RuntimeError("connection refused")
    failing_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = failing_txn

    with _patch_dgraph(mock_client), _patch_get_encoder():
        result = _invoke(["embed", "--changed"])

    assert result.exit_code != 0, (
        f"AC-RE-11: a reconcile DB failure must exit non-zero. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    assert "partgraph db up" in result.output, (
        f"AC-RE-11: output must hint 'partgraph db up'. Got:\n{result.output!r}"
    )
    assert "connection refused" not in result.output, (
        f"AC-RE-11: raw exception must not leak. Got:\n{result.output!r}"
    )
    assert not re.search(r"/(?:home|root|Users)/", result.output), (
        f"AC-RE-11: output must be path-free (no operator absolute path). "
        f"Got:\n{result.output!r}"
    )


# ===========================================================================
# AC-RE-13: backward compatibility — plain embed/--limit N unaffected;
# `--changed` is documented; `--changed` runs reconcile THEN the missing
# pass.
# ===========================================================================

def test_ac_re_13_embed_help_shows_changed_flag() -> None:
    """AC-RE-13: Given the embed command.
    When `partgraph embed --help` is invoked.
    Then the output documents the new `--changed` flag.
    """
    result = _invoke(["embed", "--help"])
    assert result.exit_code == 0, (
        f"AC-RE-13: embed --help must exit 0. Got {result.exit_code}."
    )
    assert "--changed" in result.output, (
        f"AC-RE-13: embed --help must document '--changed'. Got:\n{result.output}"
    )


def test_ac_re_13_plain_embed_without_changed_never_calls_reconcile_pass() -> None:
    """AC-RE-13 (backward-compat): Given NO --changed flag.
    When `partgraph embed --limit 10` is invoked.
    Then the reconcile pass (`_reembed_all_pages`) is NEVER called — only
    the existing missing-only pass runs, exactly as before PR4.
    """
    import partgraph.cli as cli_mod

    read_txn = _make_mock_parts_txn()
    write_txn = _make_write_txn()
    mock_client = _make_mock_client(read_txn, write_txn)
    reconcile_spy = MagicMock(return_value=0)

    with _patch_dgraph(mock_client), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_reembed_all_pages", reconcile_spy, create=True):
        result = _invoke(["embed", "--limit", "10"])

    assert result.exit_code == 0, (
        f"AC-RE-13: plain `embed --limit 10` (no --changed) must still exit 0. "
        f"Got {result.exit_code}.\n{result.output}"
    )
    reconcile_spy.assert_not_called()


def test_ac_re_13_changed_flag_runs_reconcile_pass_before_missing_pass() -> None:
    """AC-RE-13 (D3 ordering): Given `--changed`.
    When `partgraph embed --changed` is invoked.
    Then the reconcile pass (`_reembed_all_pages`) runs BEFORE the existing
    missing pass (`_embed_all_pages`) — never the reverse, never only one.
    """
    import partgraph.cli as cli_mod

    call_order: list[str] = []

    def _reconcile_spy(*args, **kwargs):
        call_order.append("reconcile")
        return 0

    def _missing_spy(*args, **kwargs):
        call_order.append("missing")
        return 0

    with _patch_dgraph(MagicMock()), \
         _patch_get_encoder(), \
         patch.object(
             cli_mod, "_reembed_all_pages", MagicMock(side_effect=_reconcile_spy), create=True,
         ), \
         patch.object(cli_mod, "_embed_all_pages", MagicMock(side_effect=_missing_spy)):
        _invoke(["embed", "--changed"])

    assert call_order == ["reconcile", "missing"], (
        f"AC-RE-13: `embed --changed` must run the reconcile pass "
        f"(_reembed_all_pages) BEFORE the existing missing pass "
        f"(_embed_all_pages). Got call order: {call_order!r}"
    )


# ===========================================================================
# AC-RE-14 (THE D2 guarantee): backfill once, then idempotent on re-run.
# ===========================================================================

def test_ac_re_14_reconcile_guarantee_backfill_then_idempotent_rerun() -> None:
    """AC-RE-14: Given a page of Parts that already HAVE an embedding but
    carry NO stored embed_text_hash (case ii for every row).
    When the reconcile pass runs once (RUN 1).
    Then every row is stamped {uid, embed_text_hash} (the sha256 oracle of
    its current text) and the encoder is NEVER called (backfill never
    re-embeds).

    Given the SAME rows are re-selected on a SECOND run (RUN 2), now
    carrying the just-backfilled hash (still equal to their current text's
    hash — nothing changed).
    When the reconcile pass runs again.
    Then NO mutation occurs at all and the encoder is still never called —
    the reconcile pass is idempotent/self-healing across repeated runs.
    """
    import partgraph.cli as cli_mod

    row_specs = [
        ("0xF001", "USB hub controller", "Interface IC", "QFN-32"),
        ("0xF002", "Linear voltage regulator", "Power IC", "SOT-23"),
    ]
    oracle_hashes = {
        uid: _oracle_hash(build_embed_text(_make_fake_part(description=d, category=c, package=p)))
        for uid, d, c, p in row_specs
    }

    # --- RUN 1: no stored hash on either row -> both are case ii (backfill). ---
    page_run1 = {"q": [
        _reembed_row(uid, description=d, category=c, package=p)  # no stored_hash
        for uid, d, c, p in row_specs
    ]}
    read_txn_1 = _make_reconcile_read_txn([page_run1])
    write_txn_1 = _make_write_txn()
    client_1 = _make_paged_mock_client(read_txn_1, write_txn_1)
    encoder_1 = _make_counting_encoder()
    controller_1 = _make_direct_call_controller()

    cli_mod._reembed_all_pages(
        client_1, encoder=encoder_1, controller=controller_1, remaining=2,
        progress_bar=MagicMock(),
    )

    encoder_1.assert_not_called()
    stamped: dict[str, dict] = {}
    for c_obj in write_txn_1.mutate.call_args_list:
        for item in (c_obj.kwargs.get("set_obj") or []):
            if isinstance(item, dict) and item.get("uid") in oracle_hashes:
                stamped[item["uid"]] = item

    assert set(stamped) == {uid for uid, *_ in row_specs}, (
        f"AC-RE-14 (run 1): every case-ii row must be stamped. Got: {stamped!r}"
    )
    for uid, item in stamped.items():
        assert set(item.keys()) == {"uid", "embed_text_hash"}, (
            f"AC-RE-14 (run 1): backfill stamp must be EXACTLY "
            f"{{uid, embed_text_hash}}. Got: {item!r}"
        )
        assert item["embed_text_hash"] == oracle_hashes[uid], (
            f"AC-RE-14 (run 1): stamped hash must equal the oracle. "
            f"Expected {oracle_hashes[uid]!r}, got {item['embed_text_hash']!r}"
        )

    # --- RUN 2: SAME rows, now carrying the just-backfilled (matching) hash. ---
    page_run2 = {"q": [
        _reembed_row(uid, description=d, category=c, package=p, stored_hash=oracle_hashes[uid])
        for uid, d, c, p in row_specs
    ]}
    read_txn_2 = _make_reconcile_read_txn([page_run2])
    write_txn_2 = _make_write_txn()
    client_2 = _make_paged_mock_client(read_txn_2, write_txn_2)
    encoder_2 = _make_counting_encoder()
    controller_2 = _make_direct_call_controller()

    cli_mod._reembed_all_pages(
        client_2, encoder=encoder_2, controller=controller_2, remaining=2,
        progress_bar=MagicMock(),
    )

    encoder_2.assert_not_called()
    write_txn_2.mutate.assert_not_called()


# ===========================================================================
# AC-RE-12: `--changed --limit N` bounds BOTH passes independently — the
# reconcile pass and the missing pass each receive remaining=N, not a split
# or shared/decrementing budget across the two passes.
# ===========================================================================

def test_ac_re_12_changed_with_limit_bounds_both_reconcile_and_missing_passes() -> None:
    """AC-RE-12: Given `--changed --limit 7`, with BOTH `_reembed_all_pages`
    (the reconcile pass) and `_embed_all_pages` (the missing pass) replaced
    by spies (`MagicMock(return_value=0)`) so no real pagination runs.
    When `partgraph embed --changed --limit 7` is invoked.
    Then EACH pass is called exactly once, and EACH receives `remaining == 7`
    as a keyword argument — `--limit` bounds both passes INDEPENDENTLY to N
    (never a split budget across the two passes, and never applied to only
    one of them).
    """
    import partgraph.cli as cli_mod

    reconcile_spy = MagicMock(return_value=0)
    missing_spy = MagicMock(return_value=0)

    with _patch_dgraph(MagicMock()), \
         _patch_get_encoder(), \
         patch.object(cli_mod, "_reembed_all_pages", reconcile_spy, create=True), \
         patch.object(cli_mod, "_embed_all_pages", missing_spy):
        result = _invoke(["embed", "--changed", "--limit", "7"])

    assert result.exit_code == 0, (
        f"AC-RE-12: `embed --changed --limit 7` with both passes spied must "
        f"exit 0. Got {result.exit_code}.\n{result.output}"
    )
    reconcile_spy.assert_called_once()
    missing_spy.assert_called_once()
    assert reconcile_spy.call_args.kwargs.get("remaining") == 7, (
        f"AC-RE-12: the reconcile pass (_reembed_all_pages) must receive "
        f"remaining == 7. Got: {reconcile_spy.call_args.kwargs.get('remaining')!r}"
    )
    assert missing_spy.call_args.kwargs.get("remaining") == 7, (
        f"AC-RE-12: the missing pass (_embed_all_pages) must ALSO receive "
        f"remaining == 7, independently of the reconcile pass (not a split "
        f"budget). Got: {missing_spy.call_args.kwargs.get('remaining')!r}"
    )
