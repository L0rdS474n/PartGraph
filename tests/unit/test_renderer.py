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
Each row:
    {"mpn", "mpn_norm", "manufacturer", "package", "category", "stock",
     "is_basic", "price_usd", "match_type", "datasheets", "params"}

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
    """AC-SF-25 (pure serializer): Given a single richly-populated RankedRow.
    When render_search_results_json is called.
    Then the row dict has EXACTLY the documented keys (no more, no less — in
    particular no 'uid').
    """
    from partgraph.query.renderer import render_search_results_json

    parsed = _make_parsed(raw_query="MAX232")
    row = _row(
        "0x10", "MAX232CPE", "exact",
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

    expected_keys = {
        "mpn", "mpn_norm", "manufacturer", "package", "category", "stock",
        "is_basic", "price_usd", "match_type", "datasheets", "params",
    }
    assert set(row_json) == expected_keys, (
        f"AC-SF-25: row must have exactly these keys: {sorted(expected_keys)}. "
        f"Got: {sorted(row_json)}"
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
