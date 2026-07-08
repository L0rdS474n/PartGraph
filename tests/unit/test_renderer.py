"""
Tests: AC-SF-24..26 (pure-serializer split) — partgraph.query.renderer JSON
envelope for `partgraph search --json`.

issue #15 PR2 introduces a machine-readable JSON envelope for `partgraph
search --json`. This file specifies the PURE, isolated contract of the
serializer that builds that envelope from a RankedResults + ParsedQuery pair
— independent of the CLI/Typer harness (which is covered separately in
tests/unit/test_cli_search.py's AC-SF-24..27/38 CLI-level tests, and
end-to-end in tests/integration/test_gate_pr6.py, AC-SF-39).

Module under test: partgraph.query.renderer
  - render_search_results_json(results: RankedResults, parsed: ParsedQuery)
      -> dict

This is the PROPOSED public contract this test file specifies for PR2 (no
function of this name exists yet — the RED-first test IS the spec). The
envelope shape:
    {"version": 1, "query": <parsed.raw_query>, "nearest_match": <bool>,
     "count": <int>, "results": [<row>, ...]}
Each row (12 keys since the hybrid semantic search PR — 'similarity' is an
ADDITIVE key, so the envelope version stays 1; AC-HY-9):
    {"mpn", "mpn_norm", "manufacturer", "package", "category", "stock",
     "is_basic", "price_usd", "match_type", "similarity", "datasheets", "params"}

Null policy: the 7 scalars (mpn/manufacturer/package/category/stock/
is_basic/price_usd) are ALWAYS present, None when the underlying RankedRow
field is None; mpn_norm is always a non-empty string; datasheets is a list of
raw URL strings ([] when none); params is a SPARSE dict of only the promoted
predicates actually present on the row ({} when none); match_type is one of
the MACHINE-SAFE names {"exact","trigram","fulltext","semantic","nearest"} —
never the human, bracket-carrying _MATCH_LABELS ("[Semantic]" etc, ADR-0008).

NOTE: `partgraph.query.renderer` already exists (render_search_results is
already implemented, PR3), so importing the MODULE succeeds today. Only the
NEW `render_search_results_json` name does not exist yet. Every test below
therefore does the import of that one new name LOCALLY (inside the test
function body, not at module level), so a missing name only fails THAT test
at call time (ImportError) rather than erroring collection of the whole
file. That local ImportError is the correct, uniform RED reason for every
test in this file today.
"""

from __future__ import annotations

import json as _json

import pytest

from partgraph.query.parser import ParsedQuery
from partgraph.query.renderer import render_search_results  # noqa: F401 — sanity import.
from partgraph.query.ranker import RankedResults, RankedRow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_parsed(
    *,
    package: str | None = None,
    text_tokens: list[str] | None = None,
    raw_query: str = "",
) -> ParsedQuery:
    return ParsedQuery(
        quantities=[],
        package=package,
        text_tokens=text_tokens or [],
        raw_query=raw_query,
    )


def _row(uid: str, mpn_norm: str, tier: str, **extra) -> RankedRow:
    """Build a RankedRow directly (bypassing rank_results/DQL parsing) for
    pure-serializer unit tests. Only fields explicitly passed are set;
    everything else keeps RankedRow's own defaults (None / [] / False).

    NOTE: extra may include price_usd=/category=, which do not exist as
    RankedRow fields until AC-SF-32 is implemented (test_ranker.py). In every
    test below the local `render_search_results_json` import (which raises
    ImportError today) executes BEFORE this helper is ever called, so that
    import failure — not a RankedRow TypeError — is the actual observed RED
    reason across this whole file.
    """
    return RankedRow(uid=uid, mpn_norm=mpn_norm, tier=tier, **extra)


# ---------------------------------------------------------------------------
# AC-SF-24: envelope shape {version, query, nearest_match, count, results}
# ---------------------------------------------------------------------------

def test_ac_sf_24_envelope_has_required_keys_and_count_matches_results_len() -> None:
    """AC-SF-24 (pure serializer): Given a RankedResults with 2 rows and a
    ParsedQuery with raw_query="MAX232".
    When render_search_results_json(results, parsed) is called.
    Then the returned dict has EXACTLY the keys {version, query, nearest_match,
    count, results}, version==1, query==parsed.raw_query,
    count==len(results.rows).
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="MAX232")
    results = RankedResults(
        rows=[
            _row("0x01", "MAX232A", "exact"),
            _row("0x02", "MAX232B", "trig"),
        ],
        nearest_match=False,
    )

    envelope = render_search_results_json(results, parsed)

    assert set(envelope) == {"version", "query", "nearest_match", "count", "results"}, (
        f"AC-SF-24: envelope must have exactly the 5 documented keys. "
        f"Got: {sorted(envelope)}"
    )
    assert envelope["version"] == 1
    assert envelope["query"] == "MAX232"
    assert envelope["nearest_match"] is False
    assert envelope["count"] == 2 == len(envelope["results"]), (
        f"AC-SF-24: count must equal len(results). Got count={envelope['count']}, "
        f"results={len(envelope['results'])}"
    )


def test_ac_sf_24_nearest_match_true_propagates_to_envelope() -> None:
    """AC-SF-24 (pure serializer): Given a RankedResults with
    nearest_match=True.
    When render_search_results_json is called.
    Then envelope["nearest_match"] is True (JSON boolean) and the row's
    match_type is "nearest".
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="1.2V MAX232")
    row = _row("0x30", "MAX232CPE", "nearest")
    results = RankedResults(rows=[row], nearest_match=True)

    envelope = render_search_results_json(results, parsed)

    assert envelope["nearest_match"] is True, (
        f"AC-SF-24: nearest_match must propagate as JSON true. "
        f"Got: {envelope['nearest_match']!r}"
    )
    assert envelope["results"][0]["match_type"] == "nearest"


def test_ac_sf_24_empty_results_gives_empty_envelope() -> None:
    """AC-SF-26 (pure serializer, mirrored here for the shape contract): Given
    a RankedResults with zero rows.
    When render_search_results_json is called.
    Then envelope == {"version":1,"query":<raw_query>,"nearest_match":False,
    "count":0,"results":[]}.
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="NOPE9999")
    results = RankedResults(rows=[], nearest_match=False)

    envelope = render_search_results_json(results, parsed)

    assert envelope == {
        "version": 1,
        "query": "NOPE9999",
        "nearest_match": False,
        "count": 0,
        "results": [],
    }, f"AC-SF-26: empty envelope must match the exact documented shape. Got: {envelope}"


def test_ac_sf_24_envelope_round_trips_through_json_dumps_loads() -> None:
    """AC-SF-24 (pure serializer): Given a populated envelope.
    When json.dumps(envelope) then json.loads(...) is applied.
    Then the round-tripped value is structurally identical — confirms every
    value in the envelope is a plain JSON-serializable type (no dataclass/
    custom object leaks into the output).
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="MAX232")
    row = _row(
        "0x20", "MAX232CPE", "exact",
        mpn="MAX232CPE", stock=1, is_basic=False, price_usd=1.0,
        datasheet_urls=["https://example.com/ds.pdf"], resistance=10000.0,
    )
    results = RankedResults(rows=[row], nearest_match=False)

    envelope = render_search_results_json(results, parsed)
    round_tripped = _json.loads(_json.dumps(envelope))

    assert round_tripped == envelope, (
        f"AC-SF-24: envelope must round-trip through json.dumps/loads "
        f"unchanged. Got: {round_tripped} vs {envelope}"
    )


# ---------------------------------------------------------------------------
# AC-SF-25: row shape {mpn, mpn_norm, manufacturer, package, category, stock,
# is_basic, price_usd, match_type, datasheets, params} + null policy
# ---------------------------------------------------------------------------

def test_ac_sf_25_row_has_exact_key_set() -> None:
    """AC-SF-25 (REWRITTEN — hybrid semantic search PR, AC-HY-9: 12-key set
    incl. 'similarity'): Given a single richly-populated RankedRow, INCLUDING
    the NEW similarity=0.8734 (a semantic-tier cosine value).
    When render_search_results_json is called.
    Then the row dict has EXACTLY the documented 12 keys (no more, no less —
    in particular no 'uid').

    CHANGED FROM PRE-HYBRID (documented, not silent): the pre-hybrid version
    of this test pinned an 11-key set. The hybrid semantic-search PR adds a
    12th key, 'similarity' (JSON-only — the human table never shows it, per
    AC-HY-8).
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="MAX232")
    row = _row(
        "0x10", "MAX232CPE", "semantic",
        mpn="MAX232CPE",
        manufacturer="Texas Instruments",
        package_name="PDIP-16",
        category="RS232 ICs",
        stock=250,
        is_basic=True,
        price_usd=0.4123,
        datasheet_urls=["https://example.com/ds.pdf"],
        voltage_max=5.5,
        similarity=0.8734,
    )
    results = RankedResults(rows=[row], nearest_match=False)

    envelope = render_search_results_json(results, parsed)
    row_json = envelope["results"][0]

    expected_keys = {
        "mpn", "mpn_norm", "manufacturer", "package", "category", "stock",
        "is_basic", "price_usd", "match_type", "datasheets", "params",
        "similarity",
    }
    assert set(row_json) == expected_keys, (
        f"AC-SF-25/AC-HY-9: row must have exactly these 12 keys: "
        f"{sorted(expected_keys)}. Got: {sorted(row_json)}"
    )
    assert "uid" not in row_json, "AC-SF-25: 'uid' must never appear in a JSON row."


def test_ac_sf_25_row_values_and_params_sparse_dict() -> None:
    """AC-SF-25 (pure serializer): Given a row with ONE promoted predicate set
    (voltage_max) and the rest absent.
    When render_search_results_json is called.
    Then every scalar/list value matches the source row, and
    params == {"voltage_max": 5.5} (sparse — only present predicates).
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="MAX232")
    row = _row(
        "0x11", "MAX232CPE", "exact",
        mpn="MAX232CPE",
        manufacturer="Texas Instruments",
        package_name="PDIP-16",
        category="RS232 ICs",
        stock=250,
        is_basic=True,
        price_usd=0.4123,
        datasheet_urls=["https://example.com/ds.pdf"],
        voltage_max=5.5,
    )
    results = RankedResults(rows=[row], nearest_match=False)

    envelope = render_search_results_json(results, parsed)
    row_json = envelope["results"][0]

    assert row_json["mpn"] == "MAX232CPE"
    assert row_json["mpn_norm"] == "MAX232CPE"
    assert row_json["manufacturer"] == "Texas Instruments"
    assert row_json["package"] == "PDIP-16"
    assert row_json["category"] == "RS232 ICs"
    assert row_json["stock"] == 250
    assert row_json["is_basic"] is True
    assert row_json["price_usd"] == pytest.approx(0.4123)
    assert row_json["match_type"] == "exact"
    assert row_json["datasheets"] == ["https://example.com/ds.pdf"]
    assert row_json["params"] == {"voltage_max": pytest.approx(5.5)}, (
        f"AC-SF-25: params must be a SPARSE dict of only-present promoted "
        f"predicates. Got: {row_json['params']}"
    )


def test_ac_sf_25_row_null_policy_scalars_null_mpn_norm_non_null_lists_empty() -> None:
    """AC-SF-25 (pure serializer): Given a minimal RankedRow with only
    uid/mpn_norm/tier set (everything else at RankedRow's own defaults).
    When render_search_results_json is called.
    Then the 7 scalar fields are present but None; mpn_norm is non-null;
    datasheets == []; params == {}.
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="MAX232")
    row = _row("0x12", "SPARSE232", "exact")
    results = RankedResults(rows=[row], nearest_match=False)

    envelope = render_search_results_json(results, parsed)
    row_json = envelope["results"][0]

    assert row_json["mpn_norm"] == "SPARSE232", "mpn_norm must always be non-null."
    for scalar in (
        "mpn", "manufacturer", "package", "category", "stock", "is_basic", "price_usd",
    ):
        assert scalar in row_json and row_json[scalar] is None, (
            f"AC-SF-25: absent scalar {scalar!r} must be present and None. "
            f"Got: {row_json.get(scalar, '<MISSING KEY>')!r}"
        )
    assert row_json["datasheets"] == [], "AC-SF-25: datasheets must be [] when none."
    assert row_json["params"] == {}, (
        "AC-SF-25: params must be {} when no promoted predicate is present."
    )


def test_ac_sf_25_match_type_maps_every_tier_to_machine_safe_label() -> None:
    """AC-SF-25 (pure serializer): Given one row per tier.
    When render_search_results_json is called.
    Then match_type maps: exact->exact, trig->trigram, fts->fulltext,
    semantic->semantic, nearest->nearest (MACHINE-SAFE names — never the
    human _MATCH_LABELS with brackets like '[Semantic]').
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="MAX232")
    expected = {
        "exact": "exact",
        "trig": "trigram",
        "fts": "fulltext",
        "semantic": "semantic",
        "nearest": "nearest",
    }
    for tier, expected_match_type in expected.items():
        results = RankedResults(rows=[_row(f"0x{tier}", "PART232", tier)], nearest_match=False)
        envelope = render_search_results_json(results, parsed)
        got = envelope["results"][0]["match_type"]
        assert got == expected_match_type, (
            f"AC-SF-25: tier {tier!r} must map to match_type "
            f"{expected_match_type!r}. Got: {got!r}"
        )
        assert "[" not in got and "]" not in got, (
            f"AC-SF-25: match_type must never contain brackets. Got: {got!r}"
        )


def test_ac_sf_25_datasheets_is_list_of_raw_url_strings_preserving_order() -> None:
    """AC-SF-25 (pure serializer): Given a row with TWO datasheet URLs.
    When render_search_results_json is called.
    Then datasheets == the exact list of raw URL strings, in order (not a
    single string, not a list of dicts).
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="MAX232")
    urls = [
        "https://www.ti.com/lit/ds/symlink/max232.pdf",
        "https://example.com/alt-max232.pdf",
    ]
    row = _row("0x13", "MAX232CPE", "exact", datasheet_urls=urls)
    results = RankedResults(rows=[row], nearest_match=False)

    envelope = render_search_results_json(results, parsed)
    assert envelope["results"][0]["datasheets"] == urls, (
        f"AC-SF-25: datasheets must be the raw URL string list, in order. "
        f"Got: {envelope['results'][0]['datasheets']!r}"
    )


# ===========================================================================
# AC-HY: hybrid semantic search robustness (Gate-1 ratified contract)
#
# _json_row gains "similarity" (cosine float for semantic rows, null
# otherwise); the JSON envelope 'version' stays 1 (additive key, ADR-0017).
# NO 384-float array may ever appear in the human table OR the JSON output
# (RankedRow structurally never carries the raw vector — see
# tests/unit/test_ranker.py AC-HY-7 — this file pins the RENDERER-facing
# regression guard). The human table is UNCHANGED: similarity is JSON-only.
# ===========================================================================

def test_ac_hy_8_no_384_float_vector_in_json_row_or_human_table() -> None:
    """AC-HY-8: Given a semantic-tier RankedRow carrying similarity=0.87 (a
    scalar cosine) — RankedRow structurally never carries the raw 384-float
    embedding (see test_ranker.py AC-HY-7), so this is the renderer-facing
    regression guard confirming NEITHER render path leaks a vector-shaped
    value.
    When BOTH render_search_results_json(...) (JSON) and
    render_search_results(...) (the human Rich table, captured via a Console
    recording to an in-memory buffer) are called on the SAME RankedResults.
    Then:
    - Every value in the JSON row is a scalar, a list of strings
      (datasheets) or a dict of numbers (params) — never a list of numbers
      (a length-384 vector would trivially violate this).
    - The JSON row's 'similarity' is the plain scalar float 0.87.
    - The human table's plain text never contains the literal similarity
      value "0.87" and gains no new 'Similarity' column — the existing
      column headers are unchanged (similarity is JSON-only).
    """
    import io as _io

    from rich.console import Console as _Console

    # render_search_results is already imported at module level (line ~50);
    # only render_search_results_json needs a local import (new name).
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="rs232 transceiver")
    row = _row(
        "0x40", "MAX232CPE", "semantic",
        mpn="MAX232CPE",
        manufacturer="Texas Instruments",
        package_name="PDIP-16",
        stock=10,
        is_basic=True,
        datasheet_urls=["https://example.com/ds.pdf"],
        voltage_max=5.5,
        similarity=0.87,
    )
    results = RankedResults(rows=[row], nearest_match=False)

    # --- JSON path ---------------------------------------------------------
    envelope = render_search_results_json(results, parsed)
    row_json = envelope["results"][0]

    def _is_scalar_or_shallow(value: object) -> bool:
        if isinstance(value, list):
            # Only a list of strings (datasheets) is legitimate; a numeric
            # list (a vector shape) must never appear.
            return all(isinstance(item, str) for item in value)
        if isinstance(value, dict):
            return all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in value.values()
            )
        return True  # str / int / float / bool / None.

    for key, value in row_json.items():
        assert _is_scalar_or_shallow(value), (
            f"AC-HY-8: JSON row key {key!r} holds a non-scalar, "
            f"non-string-list value ({value!r}) — a vector must never "
            f"appear in a JSON row."
        )
    assert row_json["similarity"] == pytest.approx(0.87), (
        f"AC-HY-8: JSON 'similarity' must be the plain scalar cosine float. "
        f"Got: {row_json['similarity']!r}"
    )

    # --- Human table path ----------------------------------------------------
    buffer = _io.StringIO()
    console = _Console(file=buffer, width=200, no_color=True)
    render_search_results(results, parsed, console)
    table_text = buffer.getvalue()

    assert "0.87" not in table_text, (
        f"AC-HY-8: the human table must never print the similarity value "
        f"(similarity is JSON-only). Got:\n{table_text}"
    )
    assert "Similarity" not in table_text, (
        f"AC-HY-8: the human table must not gain a new 'Similarity' column. "
        f"Got:\n{table_text}"
    )
    for header in ("Match", "MPN", "Manufacturer", "Package", "Stock", "Datasheet"):
        assert header in table_text, (
            f"AC-HY-8: expected the existing column header {header!r} to "
            f"still be present. Got:\n{table_text}"
        )


def test_ac_hy_9_json_row_similarity_cosine_for_semantic_null_otherwise_version_1() -> None:
    """AC-HY-9: Given TWO rows: one semantic-tier row with similarity=0.6543
    and one exact-tier row with similarity left at its RankedRow default
    (None).
    When render_search_results_json is called.
    Then:
    - Both row dicts have EXACTLY the same 12-key set (the AC-SF-25 11 plus
      'similarity').
    - The semantic row's JSON 'similarity' == 0.6543 (pytest.approx).
    - The exact row's JSON 'similarity' is JSON null (None).
    - envelope['version'] == 1 (unchanged by the additive 'similarity' key —
      ADR-0017 additive-key forward-compat policy).
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="rs232 transceiver")
    semantic_row = _row(
        "0x50", "SEMPART232", "semantic", mpn="SEMPART232", similarity=0.6543,
    )
    exact_row = _row("0x51", "EXACTPART232", "exact", mpn="EXACTPART232")
    results = RankedResults(rows=[semantic_row, exact_row], nearest_match=False)

    envelope = render_search_results_json(results, parsed)

    assert envelope["version"] == 1, (
        f"AC-HY-9: envelope version must stay 1 (additive key). "
        f"Got: {envelope['version']!r}"
    )

    sem_json, exact_json = envelope["results"]
    expected_keys = {
        "mpn", "mpn_norm", "manufacturer", "package", "category", "stock",
        "is_basic", "price_usd", "match_type", "datasheets", "params",
        "similarity",
    }
    assert set(sem_json) == expected_keys == set(exact_json), (
        f"AC-HY-9: both semantic and non-semantic rows must carry the SAME "
        f"12-key set (similarity present-but-null when N/A). "
        f"Got sem={sorted(sem_json)} exact={sorted(exact_json)}"
    )
    assert sem_json["similarity"] == pytest.approx(0.6543), (
        f"AC-HY-9: semantic row's similarity must be the cosine value. "
        f"Got: {sem_json['similarity']!r}"
    )
    assert exact_json["similarity"] is None, (
        f"AC-HY-9: a non-semantic row's similarity must be JSON null. "
        f"Got: {exact_json['similarity']!r}"
    )
