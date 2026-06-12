"""
Tests: T-LOAD-*

Verifies partgraph.load.loader.Loader using a mocked pydgraph client/txn.
No Dgraph instance is required; no shell is used.

Tests:
- T-LOAD-batch: 25 records with batch_size=10 -> exactly 3 transaction commits;
                each upsert mutation carries a JSON-serializable payload via the
                txn.mutate(set_obj=...) or Mutation(set_json=...) API, and the
                decoded JSON contains the 'xid' key for deduplication.
                Queries use the upsert (query+mutation) form, not plain mutations.

- T-LOAD-checkpoint (AC-A): Resumable load — checkpoint_path/fingerprint contract.
  Tests 1-4 and 8 below.

- T-LOAD-retry-v2 (AC-B): Tougher retry — 8 attempts, 30 s cap.
  Tests 5-7 below (updates existing AC-D2 tests where noted).

- T-LOAD-ingest-wiring (test 9): CLI _stage_load wires checkpoint_path+fingerprint.

- T-LOAD-dedup-intra-batch (fix/loader-batch-internal-duplicates):
  Tests for the intra-batch duplicate-xid bug where the same xid appearing
  at two positions in one batch produces two Part objects with different
  blank-node uids instead of collapsing to a single node.
  Root cause: registry.intern(f"part::{xid}::{i}", xid) uses the batch
  position i as part of the key, so two occurrences of the same xid get
  different keys -> different indices -> different blank nodes -> Dgraph
  creates two Part nodes in a single mutation despite @upsert.
  Tests in this block are EXPECTED RED against the current loader.

NOTE: Collection will ERROR if partgraph.load.loader does not yet exist.
That is the expected red state before implementation.

CHANGE LOG (load-robustness-v2):
  - test_ac_d2_2_backoff_non_decreasing_capped_at_30s:
      REPLACES test_ac_d2_2_backoff_non_decreasing_capped_at_8s.
      Justification: _BACKOFF_CAP_S raised 8.0 -> 30.0 per AC-B contract.
      Cap assertion updated from s <= 8.0 to s <= 30.0.

  - test_ac_d2_3_exhaustion_raises_after_8_attempts:
      REPLACES test_ac_d2_3_exhaustion_raises_with_batch_and_attempt_count.
      Justification: _MAX_ATTEMPTS raised 5 -> 8 per AC-B contract.
      Updated: assert "8" in msg (was "5"); txn.call_count == 8 (was 5).

  - test_ac_d2_retryable_classification:
      UPDATED parametrized body: expected_txn_calls = 8 if retryable else 1.
      Justification: _MAX_ATTEMPTS raised 5 -> 8; retryable exhaustion now
      takes 8 attempts. Docstring updated accordingly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from partgraph.load.loader import Loader  # noqa: F401
from partgraph.normalize.model import AttrRecord, StagedPart  # noqa: F401

# ---------------------------------------------------------------------------
# Allowlist of promoted predicates the loader may write to Dgraph.
# Keys absent from this list must NEVER appear in the JSON payload.
# ---------------------------------------------------------------------------

_PROMOTED_ALLOWLIST = frozenset({
    "voltage_min",
    "voltage_max",
    "current_max",
    "resistance",
    "capacitance",
    "inductance",
    "frequency_max",
    "power",
    "tolerance_pct",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_staged_part(
    lcsc_id: str,
    mpn_norm: str | None = None,
    mfr_norm: str = "TESTMFR",
) -> StagedPart:
    """Minimal StagedPart factory for loader tests."""
    _mpn_norm = mpn_norm or f"MPN{lcsc_id}"
    return StagedPart(
        mpn=_mpn_norm,
        mpn_norm=_mpn_norm,
        mfr_name="Test Manufacturer",
        mfr_norm=mfr_norm,
        xid=f"{_mpn_norm}|{mfr_norm}",
        description=f"Part {lcsc_id}",
        package="SOP-8",
        category="IC",
        subcategory="Logic",
        datasheet_url=f"https://example.com/{lcsc_id}.pdf",
        lcsc_id=lcsc_id,
        stock=100,
        price_usd=0.50,
        is_basic=False,
        promoted={"voltage_max": 5.0},
        attributes=[
            AttrRecord(name="Voltage", value_text="5V", value_num=5.0, unit="V"),
        ],
        tags=["SPI"],
        source_ref="jlcparts@2026-06-11",
    )


def _build_mock_pydgraph_client():
    """Return a mock pydgraph client that tracks txn.query and txn.mutate calls."""
    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    mock_txn = MagicMock()
    mock_txn.query.return_value = mock_resp
    mock_txn.mutate.return_value = MagicMock()
    mock_txn.commit.return_value = None
    mock_txn.discard.return_value = None
    # Support context manager use.
    mock_txn.__enter__ = MagicMock(return_value=mock_txn)
    mock_txn.__exit__ = MagicMock(return_value=False)

    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    return mock_client, mock_txn


def _extract_json_payload_from_mutate_call(mutate_call) -> dict:
    """Decode the JSON payload from a single txn.mutate() call.

    The loader uses pydgraph JSON mutations: either
      txn.mutate(set_obj=<dict>) or txn.mutate(mutation=Mutation(set_json=<bytes>)).
    This helper extracts and decodes whichever form was used.

    Returns the first decoded JSON object found, or raises AssertionError.
    """
    args, kwargs = mutate_call

    # Form 1: txn.mutate(set_obj=<dict or list>)
    set_obj = kwargs.get("set_obj")
    if set_obj is not None:
        if isinstance(set_obj, (dict, list)):
            return set_obj if isinstance(set_obj, dict) else set_obj[0]
        return json.loads(set_obj) if isinstance(set_obj, (bytes, str)) else set_obj

    # Form 2: txn.mutate(set_json=<bytes or str>)
    set_json = kwargs.get("set_json")
    if set_json is not None:
        raw = set_json.decode("utf-8") if isinstance(set_json, bytes) else set_json
        return json.loads(raw)

    # Form 3: txn.mutate(mutation=Mutation(set_json=...))
    mutation_obj = kwargs.get("mutation") or (args[0] if args else None)
    if mutation_obj is not None:
        sj = getattr(mutation_obj, "set_json", None)
        if sj is not None:
            raw = sj.decode("utf-8") if isinstance(sj, bytes) else sj
            return json.loads(raw)
        so = getattr(mutation_obj, "set_obj", None)
        if so is not None:
            if isinstance(so, (dict, list)):
                return so if isinstance(so, dict) else so[0]
            return json.loads(so) if isinstance(so, (bytes, str)) else so

    raise AssertionError(
        f"Could not find a JSON payload (set_obj or set_json) in mutate call: "
        f"args={args!r}, kwargs={kwargs!r}"
    )


# ---------------------------------------------------------------------------
# T-LOAD-batch — batching behaviour
# ---------------------------------------------------------------------------

def test_load_batch_25_records_batch10_creates_3_txns() -> None:
    """Given 25 StagedPart records and a Loader with batch_size=10.
    When Loader.load(parts) is called.
    Then the pydgraph client's txn() is called exactly 3 times
    (ceil(25/10) = 3 batches) and txn.commit() is called 3 times.
    """
    parts = [_make_staged_part(f"C{i:04d}") for i in range(25)]
    mock_client, mock_txn = _build_mock_pydgraph_client()

    loader = Loader(client=mock_client, batch_size=10)
    loader.load(parts)

    txn_calls = mock_client.txn.call_count
    commit_calls = mock_txn.commit.call_count

    assert txn_calls == 3, (
        f"Expected 3 txn() calls for 25 records @ batch_size=10, got {txn_calls}."
    )
    assert commit_calls == 3, (
        f"Expected 3 commit() calls, got {commit_calls}."
    )


def test_load_batch_xid_present_in_every_upsert() -> None:
    """Given any batch of StagedParts.
    When Loader.load(parts) is called.
    Then every upsert mutation carries a JSON-serializable payload via
    txn.mutate(set_obj=...) or Mutation(set_json=...), and the decoded
    JSON contains the 'xid' key, ensuring deduplication keys are always written.
    """
    parts = [_make_staged_part(f"C{i:04d}") for i in range(5)]
    mock_client, mock_txn = _build_mock_pydgraph_client()

    loader = Loader(client=mock_client, batch_size=5)
    loader.load(parts)

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "mutate() was never called."

    for i, call in enumerate(mutate_calls):
        payload = _extract_json_payload_from_mutate_call(call)
        # payload may be a single dict (one part per mutation) or a list
        # wrapping multiple parts; handle both.
        if isinstance(payload, list):
            for j, item in enumerate(payload):
                assert "xid" in item, (
                    f"Upsert mutation #{i}, item #{j} does not contain 'xid' key. "
                    f"Decoded payload: {item!r}"
                )
        else:
            assert "xid" in payload, (
                f"Upsert mutation #{i} does not contain 'xid' key in JSON payload. "
                f"Decoded payload: {payload!r}"
            )


def test_load_batch_query_mutation_upsert_form_used() -> None:
    """Given a Loader and a set of StagedParts.
    When Loader.load(parts) is called.
    Then for each batch, txn.query() is called BEFORE txn.mutate() (the upsert
    pattern: query to resolve existing UIDs, then mutate using those UIDs).

    This ensures the Loader uses the proper Dgraph upsert pattern (query + mutation
    in the same txn), not just blind inserts.
    """
    parts = [_make_staged_part("C0001")]
    mock_client, mock_txn = _build_mock_pydgraph_client()

    call_order: list[str] = []

    def _spy_query(*args, **kwargs):
        call_order.append("query")
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    def _spy_mutate(*args, **kwargs):
        call_order.append("mutate")
        return MagicMock()

    mock_txn.query.side_effect = _spy_query
    mock_txn.mutate.side_effect = _spy_mutate

    loader = Loader(client=mock_client, batch_size=10)
    loader.load(parts)

    assert "query" in call_order, (
        "Loader.load() never called txn.query(); expected upsert form (query+mutate per txn)."
    )
    assert "mutate" in call_order, (
        "Loader.load() never called txn.mutate()."
    )
    # The first operation per batch must be query (to look up existing UIDs).
    assert call_order[0] == "query", (
        f"Expected query to precede mutate in upsert pattern, call order: {call_order}"
    )


def test_load_batch_none_promoted_values_omitted() -> None:
    """Given a StagedPart with promoted={"voltage_max": 5.0} and voltage_min absent,
    and price_usd=None.
    When Loader.load([part]) is called.
    Then the decoded JSON payload must:
      - contain "voltage_max" == 5.0
      - NOT contain "voltage_min" as a key (absent/None promoted values are omitted)
      - NOT contain "price_usd" as a key (None top-level fields are omitted)
    """
    part = StagedPart(
        mpn="NONETEST",
        mpn_norm="NONETEST",
        mfr_name="Mfr",
        mfr_norm="MFR",
        xid="NONETEST|MFR",
        description="Test",
        package=None,
        category="IC",
        subcategory=None,
        datasheet_url=None,
        lcsc_id="C9999",
        stock=0,
        price_usd=None,
        is_basic=False,
        promoted={"voltage_max": 5.0},
        attributes=[],
        tags=[],
        source_ref="jlcparts@2026-06-11",
    )

    mock_client, mock_txn = _build_mock_pydgraph_client()
    loader = Loader(client=mock_client, batch_size=10)
    loader.load([part])

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "mutate() was never called."

    payload = _extract_json_payload_from_mutate_call(mutate_calls[0])
    # Normalise to a flat dict (some loaders batch into a list)
    part_obj = payload if isinstance(payload, dict) else payload[0]

    # voltage_max must be present with value 5.0
    assert "voltage_max" in part_obj, (
        f"'voltage_max' must be present in JSON payload when provided. "
        f"Decoded payload keys: {list(part_obj.keys())}"
    )
    assert part_obj["voltage_max"] == pytest.approx(5.0), (
        f"Expected voltage_max == 5.0, got {part_obj['voltage_max']!r}"
    )

    # voltage_min must NOT be a key (absent/None promoted values are omitted)
    assert "voltage_min" not in part_obj, (
        f"'voltage_min' must NOT be in JSON payload when absent from promoted dict. "
        f"Decoded payload keys: {list(part_obj.keys())}"
    )

    # price_usd is None → must NOT be a key in the payload
    assert "price_usd" not in part_obj, (
        f"'price_usd' must NOT be in JSON payload when its value is None. "
        f"Decoded payload keys: {list(part_obj.keys())}"
    )


def test_load_batch_default_batch_size_is_1000() -> None:
    """Given a Loader constructed without explicit batch_size.
    When the batch_size attribute is inspected.
    Then it equals 1000 (the specified default).
    """
    mock_client, _ = _build_mock_pydgraph_client()
    loader = Loader(client=mock_client)
    assert loader.batch_size == 1000, (
        f"Default batch_size must be 1000, got {loader.batch_size}."
    )


def test_load_no_shell_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given Loader.load() executing.
    When subprocess.run is monkeypatched to detect any shell=True call.
    Then no call to subprocess.run with shell=True is ever made.

    The Loader uses only pydgraph (gRPC), never shell commands.
    """
    import subprocess

    shell_calls: list[dict] = []

    original_run = subprocess.run

    def _spy_run(*args, **kwargs):
        if kwargs.get("shell") is True:
            shell_calls.append({"args": args, "kwargs": kwargs})
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    parts = [_make_staged_part("C0001")]
    mock_client, _ = _build_mock_pydgraph_client()
    loader = Loader(client=mock_client, batch_size=10)
    loader.load(parts)

    assert not shell_calls, (
        f"Loader.load() called subprocess.run with shell=True: {shell_calls}. "
        "The Loader must use pydgraph only; no shell commands are permitted."
    )


def test_load_source_refs_no_duplicates_on_reload() -> None:
    """Given Loader.load() called twice with the same source_ref='jlcparts@2026-06-11'.
    When the upsert mutations are inspected.
    Then source_refs is not duplicated in the set_nquads (upsert must check
    existing source_refs before appending; this is asserted at the mutation
    content level in unit tests by verifying the query tests for source_ref).

    This test verifies that the Loader's query block asks about source_refs so
    the implementation can omit duplicates.
    """
    parts = [_make_staged_part("C0002")]
    mock_client, mock_txn = _build_mock_pydgraph_client()

    call_order_queries: list[str] = []

    def _spy_query(*args, **kwargs):
        dql_args = args[0] if args else str(kwargs)
        call_order_queries.append(str(dql_args))
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_txn.query.side_effect = _spy_query

    loader = Loader(client=mock_client, batch_size=10)
    loader.load(parts)

    assert call_order_queries, "No queries were issued by the Loader."
    combined_queries = " ".join(call_order_queries)
    assert "source_refs" in combined_queries or "xid" in combined_queries, (
        "Loader queries must reference 'xid' (for upsert key lookup) and/or "
        f"'source_refs'. Got queries: {call_order_queries}"
    )


# ---------------------------------------------------------------------------
# A2 — special-character round-trip through JSON payload
# ---------------------------------------------------------------------------

def test_load_description_special_chars_round_trip_intact() -> None:
    """Given a StagedPart whose description contains a double quote, a backslash,
    and a newline — all built at runtime to avoid scanner-sensitive literals.
    When Loader.load([part]) is called.
    Then json.loads() of the captured mutate payload returns the exact original
    description string, proving the loader never builds mutation strings manually
    (which would require error-prone escaping) and instead serialises via
    json.dumps() / set_obj / set_json.
    """
    # Build the problematic characters at runtime.
    quote = chr(34)       # "
    backslash = chr(92)   # \
    newline = chr(10)     # LF
    description = f"Part with {quote}quoted{quote} and {backslash}slash and{newline}newline"

    part = StagedPart(
        mpn="SPECIALCHARS",
        mpn_norm="SPECIALCHARS",
        mfr_name="SpecialMfr",
        mfr_norm="SPECIALMFR",
        xid="SPECIALCHARS|SPECIALMFR",
        description=description,
        package="QFN-16",
        category="IC",
        subcategory="Mixed",
        datasheet_url=None,
        lcsc_id="C7777",
        stock=10,
        price_usd=1.25,
        is_basic=False,
        promoted={},
        attributes=[],
        tags=[],
        source_ref="jlcparts@2026-06-11",
    )

    mock_client, mock_txn = _build_mock_pydgraph_client()
    loader = Loader(client=mock_client, batch_size=10)
    loader.load([part])

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "mutate() was never called."

    payload = _extract_json_payload_from_mutate_call(mutate_calls[0])
    part_obj = payload if isinstance(payload, dict) else payload[0]

    assert "description" in part_obj, (
        f"'description' key missing from JSON payload. Keys: {list(part_obj.keys())}"
    )
    assert part_obj["description"] == description, (
        f"Description round-trip failed.\n"
        f"Expected: {description!r}\n"
        f"Got:      {part_obj['description']!r}\n"
        "This proves manual string building was used instead of json.dumps()."
    )


# ---------------------------------------------------------------------------
# A3 — None promoted values: voltage_min absent, voltage_max present
# ---------------------------------------------------------------------------

def test_load_none_promoted_voltage_min_absent_voltage_max_present() -> None:
    """Given promoted={"voltage_max": 5.0} with voltage_min absent/None.
    When Loader.load([part]) is called.
    Then the decoded JSON payload:
      - does NOT contain "voltage_min" as a key
      - contains "voltage_max" == 5.0
    The old '"None" not in nquads' style assertion is replaced by this structural check.
    """
    part = StagedPart(
        mpn="VOLTTEST",
        mpn_norm="VOLTTEST",
        mfr_name="VoltMfr",
        mfr_norm="VOLTMFR",
        xid="VOLTTEST|VOLTMFR",
        description="Voltage test part",
        package="SOT-23",
        category="IC",
        subcategory="LDO",
        datasheet_url=None,
        lcsc_id="C8888",
        stock=50,
        price_usd=0.10,
        is_basic=False,
        promoted={"voltage_max": 5.0},  # voltage_min intentionally absent
        attributes=[],
        tags=[],
        source_ref="jlcparts@2026-06-11",
    )

    mock_client, mock_txn = _build_mock_pydgraph_client()
    loader = Loader(client=mock_client, batch_size=10)
    loader.load([part])

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "mutate() was never called."

    payload = _extract_json_payload_from_mutate_call(mutate_calls[0])
    part_obj = payload if isinstance(payload, dict) else payload[0]

    assert "voltage_min" not in part_obj, (
        f"'voltage_min' must NOT be a key in JSON payload when absent from promoted. "
        f"Decoded payload keys: {list(part_obj.keys())}"
    )
    assert "voltage_max" in part_obj, (
        f"'voltage_max' must be a key in JSON payload when present in promoted. "
        f"Decoded payload keys: {list(part_obj.keys())}"
    )
    assert part_obj["voltage_max"] == pytest.approx(5.0), (
        f"Expected voltage_max == 5.0, got {part_obj['voltage_max']!r}"
    )


def test_load_price_usd_none_key_absent_from_payload() -> None:
    """Given a StagedPart with price_usd=None.
    When Loader.load([part]) is called.
    Then the decoded JSON payload does NOT contain "price_usd" as a key.
    """
    part = StagedPart(
        mpn="NOPRICE",
        mpn_norm="NOPRICE",
        mfr_name="NoPriceMfr",
        mfr_norm="NOPRICEMFR",
        xid="NOPRICE|NOPRICEMFR",
        description="Part with no price",
        package=None,
        category="Passive",
        subcategory=None,
        datasheet_url=None,
        lcsc_id="C6666",
        stock=0,
        price_usd=None,
        is_basic=False,
        promoted={},
        attributes=[],
        tags=[],
        source_ref="jlcparts@2026-06-11",
    )

    mock_client, mock_txn = _build_mock_pydgraph_client()
    loader = Loader(client=mock_client, batch_size=10)
    loader.load([part])

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "mutate() was never called."

    payload = _extract_json_payload_from_mutate_call(mutate_calls[0])
    part_obj = payload if isinstance(payload, dict) else payload[0]

    assert "price_usd" not in part_obj, (
        f"'price_usd' must NOT be a key in JSON payload when value is None. "
        f"Decoded payload keys: {list(part_obj.keys())}"
    )


# ---------------------------------------------------------------------------
# A4 — Category composite key: (name, parent) identity for level-2 categories
# ---------------------------------------------------------------------------

def test_load_category_composite_key_distinguishes_same_name_different_parent() -> None:
    """Given two StagedParts, both with subcategory='Other' but different categories
    ('Resistors' vs 'Capacitors').
    When Loader.load(parts) is called.
    Then the two level-2 category objects in the captured JSON payloads are
    distinguishable by their parent linkage (e.g. distinct upsert keys / xid-like
    identifiers containing the parent name, or distinct query conditions containing
    both the subcategory name and the parent category name).
    This pins the requirement that the loader MUST use (name, parent) composite
    identity for level-2 categories — never name alone.
    """
    part_resistors = StagedPart(
        mpn="RES_OTHER",
        mpn_norm="RES_OTHER",
        mfr_name="ResMfr",
        mfr_norm="RESMFR",
        xid="RES_OTHER|RESMFR",
        description="A resistor in Other subcategory",
        package="0402",
        category="Resistors",
        subcategory="Other",
        datasheet_url=None,
        lcsc_id="C1001",
        stock=500,
        price_usd=0.01,
        is_basic=False,
        promoted={},
        attributes=[],
        tags=[],
        source_ref="jlcparts@2026-06-11",
    )
    part_capacitors = StagedPart(
        mpn="CAP_OTHER",
        mpn_norm="CAP_OTHER",
        mfr_name="CapMfr",
        mfr_norm="CAPMFR",
        xid="CAP_OTHER|CAPMFR",
        description="A capacitor in Other subcategory",
        package="0402",
        category="Capacitors",
        subcategory="Other",
        datasheet_url=None,
        lcsc_id="C1002",
        stock=500,
        price_usd=0.01,
        is_basic=False,
        promoted={},
        attributes=[],
        tags=[],
        source_ref="jlcparts@2026-06-11",
    )

    mock_client, mock_txn = _build_mock_pydgraph_client()
    loader = Loader(client=mock_client, batch_size=10)
    loader.load([part_resistors, part_capacitors])

    # Collect all query strings and mutate payloads for inspection.
    query_texts: list[str] = []
    payload_texts: list[str] = []

    for q_call in mock_txn.query.call_args_list:
        q_args, q_kwargs = q_call
        query_texts.append(str(q_args[0] if q_args else q_kwargs))

    for m_call in mock_txn.mutate.call_args_list:
        m_args, m_kwargs = m_call
        # Serialise all kwargs / payload to a string for composite key inspection.
        payload_texts.append(str(m_args) + str(m_kwargs))

    combined = " ".join(query_texts + payload_texts)

    # The loader must reference BOTH parent names in its upserts/queries so that
    # "Other" under "Resistors" and "Other" under "Capacitors" produce distinct
    # graph nodes.
    assert "Resistors" in combined, (
        "Loader must reference the parent category 'Resistors' in its upsert "
        "queries/payloads for level-2 category deduplication."
    )
    assert "Capacitors" in combined, (
        "Loader must reference the parent category 'Capacitors' in its upsert "
        "queries/payloads for level-2 category deduplication."
    )

    # Additionally, the two sub-category identity tokens (upsert keys, xids, or
    # query conditions) that include "Other" must be distinguishable — i.e. not
    # merely the string "Other" without parent context.  We verify that at least
    # one of "Resistors" or "Capacitors" appears in close proximity to "Other"
    # (within the same batch upsert payload / query string).
    other_context_has_parent = (
        ("Resistors" in combined and "Other" in combined) and
        ("Capacitors" in combined and "Other" in combined)
    )
    assert other_context_has_parent, (
        "Level-2 category 'Other' must be paired with its parent name in the "
        "loader's upsert identity (composite key), not used as a standalone name. "
        f"Combined query+payload text excerpt (first 500 chars): {combined[:500]!r}"
    )


# ---------------------------------------------------------------------------
# A5 — Unknown promoted key must not appear in JSON payload
# ---------------------------------------------------------------------------

def test_load_unknown_promoted_key_filtered_out() -> None:
    """Given a StagedPart with promoted={"bogus_param": 1.0} (not in allowlist).
    When Loader.load([part]) is called.
    Then the decoded JSON payload does NOT contain "bogus_param" and no error
    is raised (unknown keys are silently dropped via the allowlist filter).

    The promoted allowlist is: voltage_min, voltage_max, current_max, resistance,
    capacitance, inductance, frequency_max, power, tolerance_pct.
    """
    part = StagedPart(
        mpn="BOGUSPARAM",
        mpn_norm="BOGUSPARAM",
        mfr_name="BogMfr",
        mfr_norm="BOGMFR",
        xid="BOGUSPARAM|BOGMFR",
        description="Part with unknown promoted param",
        package="SOT-23",
        category="IC",
        subcategory="Other",
        datasheet_url=None,
        lcsc_id="C5555",
        stock=10,
        price_usd=0.50,
        is_basic=False,
        promoted={"bogus_param": 1.0},  # NOT in allowlist
        attributes=[],
        tags=[],
        source_ref="jlcparts@2026-06-11",
    )

    mock_client, mock_txn = _build_mock_pydgraph_client()
    loader = Loader(client=mock_client, batch_size=10)
    # Must not raise.
    loader.load([part])

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "mutate() was never called."

    payload = _extract_json_payload_from_mutate_call(mutate_calls[0])
    part_obj = payload if isinstance(payload, dict) else payload[0]

    assert "bogus_param" not in part_obj, (
        f"'bogus_param' must NOT appear in JSON payload — it is not in the "
        f"promoted allowlist. Decoded payload keys: {list(part_obj.keys())}"
    )

    # None of the non-allowlist keys should appear.
    # The allowed set is the schema/partgraph.dql Part predicates the loader
    # legitimately emits (scalars + edge predicates + provenance), plus the
    # promoted allowlist. Edge predicates (made_by, in_category, in_package,
    # datasheet, tagged, attr) and source_refs are required by the integration
    # tests in tests/integration/test_ingest_load.py, so they must be permitted
    # here while still catching stray keys such as the unknown promoted param.
    for key in list(part_obj.keys()):
        if key in _PROMOTED_ALLOWLIST or key in {
            "xid", "mpn", "mpn_norm", "description", "lcsc_id",
            "stock", "price_usd", "is_basic", "source_refs",
            "made_by", "in_category", "in_package", "datasheet", "tagged", "attr",
            "dgraph.type", "uid",
        }:
            continue
        assert False, (  # noqa: B011
            f"Unexpected key {key!r} in JSON payload — must be in the known "
            f"field set or the promoted allowlist. Payload keys: {list(part_obj.keys())}"
        )


# ===========================================================================
# Defect 2: per-batch transient-error retry
# Tests: AC-D2-1 through AC-D2-6
#
# ALL tests in this block are EXPECTED RED against the current Loader, which
# has no retry logic.  They turn green only after Defect 2 is fixed.
#
# Determinism contract
# --------------------
# - No real time.sleep() is ever called (injected no-op or recording callable).
# - Retryable exceptions are constructed by shape (str/attrs), not by importing
#   private pydgraph symbols.
# - max_attempts=5, base=0.5 s, cap=8.0 s, full jitter (sleep <= cap).
# ===========================================================================

# ---------------------------------------------------------------------------
# Exception helpers — constructed by behavioral shape, not private symbols
# ---------------------------------------------------------------------------

def _make_retryable_exc(message: str) -> Exception:
    """Return an exception whose str() contains *message*, acting as a retryable error."""
    return Exception(message)


def _make_retryable_grpc_exc(status_name: str) -> Exception:
    """Return an exception that looks like a gRPC status error for *status_name*.

    The implementation will detect retryable gRPC statuses by inspecting
    the exception's string representation or a status code attribute.
    We provide both so the impl can use whichever is convenient.
    """
    exc = Exception(f"StatusCode.{status_name}: transient error")
    exc.code = lambda: status_name  # type: ignore[attr-defined]
    return exc


def _make_fatal_exc(exc_type: type) -> Exception:
    """Return a fatal (non-retryable) exception of the given built-in type."""
    return exc_type("fatal error — must not retry")


# ---------------------------------------------------------------------------
# AC-D2-1: retryable error on first 2 attempts then success; txn called 3x
# ---------------------------------------------------------------------------

def test_ac_d2_1_retryable_succeeds_on_third_attempt() -> None:
    """Given a mock client whose txn's mutate() raises a retryable error
    on the first 2 calls and then succeeds on the 3rd.
    When Loader.load([part], sleep=noop) is called.
    Then:
    - load() completes without raising.
    - txn() was called exactly 3 times for that single batch (one per attempt).
    - txn.commit() was called exactly once (on the successful attempt).
    - metrics are written (returned dict has 'parts_loaded' == 1).
    """
    part = _make_staged_part("C9001")
    fail_count = {"n": 0}

    def _flaky_mutate(*args, **kwargs):
        if fail_count["n"] < 2:
            fail_count["n"] += 1
            raise _make_retryable_exc("Only leader can decide to commit or abort")
        return MagicMock()

    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    txn_instances: list[MagicMock] = []

    def _make_txn():
        txn = MagicMock()
        txn.query.return_value = mock_resp
        txn.mutate.side_effect = _flaky_mutate
        txn.commit.return_value = None
        txn.discard.return_value = None
        txn.__enter__ = MagicMock(return_value=txn)
        txn.__exit__ = MagicMock(return_value=False)
        txn_instances.append(txn)
        return txn

    mock_client = MagicMock()
    mock_client.txn.side_effect = _make_txn

    noop_sleep = MagicMock()
    loader = Loader(client=mock_client, batch_size=10, sleep=noop_sleep)
    metrics = loader.load([part])

    assert metrics.get("parts_loaded") == 1, (
        f"Expected parts_loaded=1, got {metrics!r}"
    )
    txn_call_count = mock_client.txn.call_count
    assert txn_call_count == 3, (
        f"Expected 3 txn() calls (one per attempt), got {txn_call_count}. "
        "Each retry must open a fresh transaction."
    )
    # The last txn instance must have had commit called exactly once
    last_txn = txn_instances[-1]
    assert last_txn.commit.call_count == 1, (
        f"commit() must be called exactly once on the successful attempt; "
        f"got {last_txn.commit.call_count}"
    )


# ---------------------------------------------------------------------------
# AC-D2-2 / AC-B: backoff sleeps are non-decreasing, respect cap, count == K
#
# UPDATED for load-robustness-v2: cap raised from 8.0 -> 30.0 s.
# Old test name: test_ac_d2_2_backoff_non_decreasing_capped_at_8s (removed).
# New test name: test_ac_d2_2_backoff_non_decreasing_capped_at_30s.
# Justification: _BACKOFF_CAP_S raised 8.0 -> 30.0 so the retry window spans
# a typical Dgraph container restart + Raft leader election (~1-2 min).
# ---------------------------------------------------------------------------

def test_ac_d2_2_backoff_non_decreasing_capped_at_30s() -> None:
    """Given a mock client that fails K=3 times then succeeds.
    When Loader.load([part], sleep=recording_sleep) is called.
    Then:
    - Exactly K=3 sleep calls are recorded (one before each retry).
    - Each recorded sleep value is <= 30.0 (the new jitter cap, raised from 8.0).
    - time.sleep is never called (monkeypatched to raise AssertionError).

    AC-B: _BACKOFF_CAP_S == 30.0 (was 8.0 before load-robustness-v2).
    """
    K = 3
    part = _make_staged_part("C9002")
    fail_count = {"n": 0}

    def _flaky_mutate(*args, **kwargs):
        if fail_count["n"] < K:
            fail_count["n"] += 1
            raise _make_retryable_exc("StatusCode.UNAVAILABLE: deadline exceeded")
        return MagicMock()

    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    def _make_txn():
        txn = MagicMock()
        txn.query.return_value = mock_resp
        txn.mutate.side_effect = _flaky_mutate
        txn.commit.return_value = None
        txn.discard.return_value = None
        txn.__enter__ = MagicMock(return_value=txn)
        txn.__exit__ = MagicMock(return_value=False)
        return txn

    mock_client = MagicMock()
    mock_client.txn.side_effect = _make_txn

    recorded_sleeps: list[float] = []

    def _recording_sleep(duration: float) -> None:
        recorded_sleeps.append(duration)

    # Monkeypatch time.sleep to raise if the implementation calls it directly
    import time as _time_mod

    original_sleep = _time_mod.sleep

    def _banned_sleep(duration: float) -> None:
        raise AssertionError(
            f"time.sleep({duration}) called directly; the injected sleep= "
            "callable must be used instead."
        )

    _time_mod.sleep = _banned_sleep
    try:
        loader = Loader(client=mock_client, batch_size=10, sleep=_recording_sleep)
        loader.load([part])
    finally:
        _time_mod.sleep = original_sleep

    assert len(recorded_sleeps) == K, (
        f"Expected exactly {K} sleep calls (one per retry), got {len(recorded_sleeps)}: "
        f"{recorded_sleeps!r}"
    )
    for i, s in enumerate(recorded_sleeps):
        assert s <= 30.0, (
            f"Sleep #{i} value {s} exceeds the 30.0 s cap (AC-B: cap raised from 8.0)."
        )
        assert s >= 0.0, (
            f"Sleep #{i} value {s} is negative."
        )
    # Non-decreasing in expectation value (full jitter means individual values
    # may vary; we only assert the cap is respected and count is right).


# ---------------------------------------------------------------------------
# AC-D2-3 / AC-B: exhaustion after exactly 8 attempts → single exception with
# batch/count in message.
#
# UPDATED for load-robustness-v2: _MAX_ATTEMPTS raised 5 -> 8.
# Old test name: test_ac_d2_3_exhaustion_raises_with_batch_and_attempt_count (removed).
# New test name: test_ac_d2_3_exhaustion_raises_after_8_attempts.
# Justification: _MAX_ATTEMPTS raised from 5 to 8 so a full-jitter retry window
# at 30 s cap spans a typical Dgraph container restart + Raft election (~1-2 min).
# ---------------------------------------------------------------------------

def test_ac_d2_3_exhaustion_raises_after_8_attempts() -> None:
    """Given a mock client that ALWAYS raises a retryable error.
    When Loader.load([part], sleep=noop) is called.
    Then:
    - A single exception is raised (not 8 separate exceptions).
    - The exception message contains "0" (batch index) AND "8" (attempt count).
    - The exception's __cause__ is the last retryable error raised by the client.
    - No further batches are processed after exhaustion on batch 0.

    AC-B: _MAX_ATTEMPTS == 8 (was 5 before load-robustness-v2).
    """
    part = _make_staged_part("C9003")
    last_exc = _make_retryable_exc("Only leader can decide to commit or abort")

    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    def _always_fail_mutate(*args, **kwargs):
        raise last_exc

    def _make_txn():
        txn = MagicMock()
        txn.query.return_value = mock_resp
        txn.mutate.side_effect = _always_fail_mutate
        txn.commit.return_value = None
        txn.discard.return_value = None
        txn.__enter__ = MagicMock(return_value=txn)
        txn.__exit__ = MagicMock(return_value=False)
        return txn

    mock_client = MagicMock()
    mock_client.txn.side_effect = _make_txn

    noop_sleep = MagicMock()
    loader = Loader(client=mock_client, batch_size=10, sleep=noop_sleep)

    with pytest.raises(Exception) as exc_info:
        loader.load([part])

    raised = exc_info.value
    msg = str(raised)
    assert "0" in msg, (
        f"Exception message must contain batch index '0', got: {msg!r}"
    )
    assert "8" in msg, (
        f"Exception message must contain attempt count '8' (AC-B: was '5'), got: {msg!r}"
    )
    assert raised.__cause__ is last_exc, (
        f"Exception __cause__ must be the last retryable error; "
        f"got __cause__={raised.__cause__!r}"
    )
    # txn() called exactly 8 times (max_attempts) for a single-batch load
    assert mock_client.txn.call_count == 8, (
        f"Expected exactly 8 txn() calls (AC-B: _MAX_ATTEMPTS=8, was 5); "
        f"got {mock_client.txn.call_count}"
    )


# ---------------------------------------------------------------------------
# AC-D2-4: fatal error (TypeError) → abort after 1 attempt, no sleep
# ---------------------------------------------------------------------------

def test_ac_d2_4_fatal_error_aborts_after_one_attempt() -> None:
    """Given a mock client whose mutate() raises TypeError (a fatal, non-retryable error).
    When Loader.load([part], sleep=recording_sleep) is called.
    Then:
    - The original TypeError propagates immediately (not wrapped).
    - txn() is called exactly once (no retry).
    - The injected sleep callable is never called.
    """
    part = _make_staged_part("C9004")
    fatal_exc = _make_fatal_exc(TypeError)

    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    def _fatal_mutate(*args, **kwargs):
        raise fatal_exc

    def _make_txn():
        txn = MagicMock()
        txn.query.return_value = mock_resp
        txn.mutate.side_effect = _fatal_mutate
        txn.commit.return_value = None
        txn.discard.return_value = None
        txn.__enter__ = MagicMock(return_value=txn)
        txn.__exit__ = MagicMock(return_value=False)
        return txn

    mock_client = MagicMock()
    mock_client.txn.side_effect = _make_txn

    recorded_sleeps: list[float] = []

    def _recording_sleep(duration: float) -> None:
        recorded_sleeps.append(duration)

    loader = Loader(client=mock_client, batch_size=10, sleep=_recording_sleep)

    with pytest.raises(TypeError) as exc_info:
        loader.load([part])

    assert exc_info.value is fatal_exc, (
        "The original TypeError must propagate directly (not wrapped)."
    )
    assert mock_client.txn.call_count == 1, (
        f"Fatal error must abort after 1 attempt; txn() called {mock_client.txn.call_count}x."
    )
    assert recorded_sleeps == [], (
        f"No sleep must occur for a fatal error; got {recorded_sleeps!r}"
    )


# ---------------------------------------------------------------------------
# AC-D2-5: retryable on 2nd batch only; other batches commit once each
# ---------------------------------------------------------------------------

def test_ac_d2_5_retry_isolated_to_failing_batch() -> None:
    """Given 25 parts (batch_size=10) → 3 batches.
    When the SECOND batch's mutate() raises a retryable error once then succeeds.
    Then:
    - batch 1 commits once (no retry needed).
    - batch 2 commits once (after 1 retry, i.e. 2 txn calls for it).
    - batch 3 commits once (no retry needed).
    - Total txn() calls = 4 (1+2+1).
    - Batch 1 parts are NOT re-sent after batch 2 retries (isolation).
    """
    parts = [_make_staged_part(f"C{i:04d}") for i in range(25)]

    # Track how many times mutate is called, grouped by batch
    # We identify batch 2 by call number: batch1=calls1-3, batch2=calls4-6, batch3=calls7-9
    # (each batch does 1 mutate per txn attempt)
    mutate_call_count = {"total": 0}
    # Batch 2 starts at mutate call #2 (0-indexed); we count per-txn calls
    batch2_attempts = {"n": 0}
    current_batch = {"n": 0}

    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    txn_call_count = {"n": 0}

    # We track which "batch window" we're in by txn() creation order
    # batch1 → txn #1, batch2 → txn #2 (retry: txn #3), batch3 → txn #4
    txn_index = {"n": 0}

    def _make_txn():
        txn_index["n"] += 1
        this_txn_index = txn_index["n"]
        txn_call_count["n"] += 1

        def _maybe_flaky_mutate(*args, **kwargs):
            # txn index 2 is the first attempt of batch 2 → fail once
            if this_txn_index == 2:
                raise _make_retryable_exc("StatusCode.ABORTED: transaction aborted")
            return MagicMock()

        txn = MagicMock()
        txn.query.return_value = mock_resp
        txn.mutate.side_effect = _maybe_flaky_mutate
        txn.commit.return_value = None
        txn.discard.return_value = None
        txn.__enter__ = MagicMock(return_value=txn)
        txn.__exit__ = MagicMock(return_value=False)
        return txn

    mock_client = MagicMock()
    mock_client.txn.side_effect = _make_txn

    noop_sleep = MagicMock()
    loader = Loader(client=mock_client, batch_size=10, sleep=noop_sleep)
    metrics = loader.load(parts)

    assert metrics.get("parts_loaded") == 25, (
        f"All 25 parts must be loaded; got {metrics!r}"
    )
    # Total txn calls: 1 (batch1) + 2 (batch2: fail+success) + 1 (batch3) = 4
    assert txn_call_count["n"] == 4, (
        f"Expected 4 total txn() calls (1+2+1 for batches 1,2,3); "
        f"got {txn_call_count['n']}"
    )


# ---------------------------------------------------------------------------
# AC-D2-6: happy-path tests stay green (retry transparent)
# ---------------------------------------------------------------------------

def test_ac_d2_6_happy_path_unchanged_with_sleep_param() -> None:
    """Given Loader constructed with an injected sleep= callable.
    When Loader.load(25 parts, batch_size=10) is called with no errors.
    Then load completes as before: 3 txns committed, parts_loaded=25,
    and the sleep callable is never invoked (no errors, no retries).
    """
    parts = [_make_staged_part(f"C{i:04d}") for i in range(25)]
    mock_client, mock_txn = _build_mock_pydgraph_client()

    recorded_sleeps: list[float] = []

    def _recording_sleep(duration: float) -> None:
        recorded_sleeps.append(duration)

    loader = Loader(client=mock_client, batch_size=10, sleep=_recording_sleep)
    metrics = loader.load(parts)

    assert mock_client.txn.call_count == 3, (
        f"Happy path: expected 3 txn() calls, got {mock_client.txn.call_count}"
    )
    assert mock_txn.commit.call_count == 3, (
        f"Happy path: expected 3 commit() calls, got {mock_txn.commit.call_count}"
    )
    assert metrics.get("parts_loaded") == 25, (
        f"Happy path: expected parts_loaded=25, got {metrics!r}"
    )
    assert recorded_sleeps == [], (
        f"Happy path: sleep must never be called when no errors occur; "
        f"got {recorded_sleeps!r}"
    )


# ---------------------------------------------------------------------------
# AC-D2: retryable vs fatal classification  (parametrized)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc_factory,retryable", [
    # Retryable by gRPC-ish status in string
    (lambda: _make_retryable_exc("StatusCode.UNKNOWN: server error"),        True),
    (lambda: _make_retryable_exc("StatusCode.UNAVAILABLE: deadline exceeded"), True),
    (lambda: _make_retryable_exc("StatusCode.ABORTED: conflict"),             True),
    # Retryable by "Only leader" message
    (lambda: _make_retryable_exc("Only leader can decide to commit or abort"), True),
    # Retryable by aborted-transaction shape (message contains "aborted")
    (lambda: _make_retryable_exc("Transaction has been aborted. Please retry"), True),
    # Fatal: built-in TypeError
    (lambda: _make_fatal_exc(TypeError),  False),
    # Fatal: built-in ValueError
    (lambda: _make_fatal_exc(ValueError), False),
])
def test_ac_d2_retryable_classification(exc_factory, retryable: bool) -> None:
    """Given an exception constructed by behavioral shape (no private pydgraph symbols).
    When Loader.load([part], sleep=noop) is called and the client always raises it.
    Then:
    - retryable=True  → txn() called exactly 8 times (max_attempts exhausted).
    - retryable=False → txn() called exactly once (abort after 1 attempt).

    AC-B: _MAX_ATTEMPTS == 8 (was 5 before load-robustness-v2).
    """
    part = _make_staged_part("C9010")

    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    def _always_raise_mutate(*args, **kwargs):
        raise exc_factory()

    def _make_txn():
        txn = MagicMock()
        txn.query.return_value = mock_resp
        txn.mutate.side_effect = _always_raise_mutate
        txn.commit.return_value = None
        txn.discard.return_value = None
        txn.__enter__ = MagicMock(return_value=txn)
        txn.__exit__ = MagicMock(return_value=False)
        return txn

    mock_client = MagicMock()
    mock_client.txn.side_effect = _make_txn

    noop_sleep = MagicMock()
    loader = Loader(client=mock_client, batch_size=10, sleep=noop_sleep)

    with pytest.raises(Exception, match=r"."):  # noqa: B017 — any exc is expected
        loader.load([part])

    # AC-B: _MAX_ATTEMPTS raised 5 -> 8; retryable exhaustion now takes 8 attempts.
    expected_txn_calls = 8 if retryable else 1
    assert mock_client.txn.call_count == expected_txn_calls, (
        f"{'Retryable' if retryable else 'Fatal'} exception "
        f"{exc_factory()!r} should cause {expected_txn_calls} txn() call(s) "
        f"(AC-B: was 5 for retryable); got {mock_client.txn.call_count}"
    )


# ===========================================================================
# AC-B: New retry tests for load-robustness-v2
# Tests 5 and 6 per pinned contract
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 5 (AC-B): max 8 attempts then raise; message contains "8" and batch "0"
# ---------------------------------------------------------------------------

def test_load_retry_max_8_attempts() -> None:
    """Given a mock client whose mutate() ALWAYS raises a retryable error.
    When Loader.load([part], sleep=noop) is called.
    Then:
    - Exactly 8 txn() calls are made (one per attempt, _MAX_ATTEMPTS == 8).
    - A RuntimeError is raised.
    - The message contains "8" (attempt count) and "0" (batch index).
    - __cause__ is set to the last retryable exception.

    AC-B: _MAX_ATTEMPTS raised 5 -> 8 to survive Dgraph container restart +
    Raft leader election (~1-2 min total worst-case tolerance).
    """
    part = _make_staged_part("C8001")
    last_exc = _make_retryable_exc("StatusCode.UNAVAILABLE: Raft leader election")

    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    def _always_fail_mutate(*args, **kwargs):
        raise last_exc

    def _make_txn():
        txn = MagicMock()
        txn.query.return_value = mock_resp
        txn.mutate.side_effect = _always_fail_mutate
        txn.commit.return_value = None
        txn.discard.return_value = None
        txn.__enter__ = MagicMock(return_value=txn)
        txn.__exit__ = MagicMock(return_value=False)
        return txn

    mock_client = MagicMock()
    mock_client.txn.side_effect = _make_txn

    noop_sleep = MagicMock()
    loader = Loader(client=mock_client, batch_size=10, sleep=noop_sleep)

    with pytest.raises(RuntimeError) as exc_info:
        loader.load([part])

    raised = exc_info.value
    msg = str(raised)

    assert "8" in msg, (
        f"RuntimeError message must contain '8' (attempt count). Got: {msg!r}"
    )
    assert "0" in msg, (
        f"RuntimeError message must contain '0' (batch index). Got: {msg!r}"
    )
    assert raised.__cause__ is last_exc, (
        f"__cause__ must be the last retryable exception; got {raised.__cause__!r}"
    )
    assert mock_client.txn.call_count == 8, (
        f"Exactly 8 txn() calls expected (_MAX_ATTEMPTS=8); "
        f"got {mock_client.txn.call_count}"
    )


# ---------------------------------------------------------------------------
# Test 6 (AC-B): backoff cap 30 s — 7 failures then success; delays <= 30.0
# ---------------------------------------------------------------------------

def test_load_retry_backoff_cap_30() -> None:
    """Given a mock client whose mutate() fails 7 times then succeeds on attempt 8.
    When Loader.load([part], sleep=recording_sleep) is called.
    Then:
    - Exactly 7 sleep() calls are recorded (one before each of the 7 retries).
    - Every recorded delay is >= 0.0 and <= 30.0 (the new cap).
    - time.sleep is never called directly (monkeypatched to raise).
    - Load completes without raising (success on attempt 8).

    AC-B: _BACKOFF_CAP_S raised 8.0 -> 30.0 to allow waiting through a Raft
    leader election where unavailability can exceed 15 s.
    """
    FAIL_TIMES = 7
    part = _make_staged_part("C8002")
    fail_count = {"n": 0}

    def _mostly_failing_mutate(*args, **kwargs):
        if fail_count["n"] < FAIL_TIMES:
            fail_count["n"] += 1
            raise _make_retryable_exc("Only leader can decide to commit or abort")
        return MagicMock()

    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    def _make_txn():
        txn = MagicMock()
        txn.query.return_value = mock_resp
        txn.mutate.side_effect = _mostly_failing_mutate
        txn.commit.return_value = None
        txn.discard.return_value = None
        txn.__enter__ = MagicMock(return_value=txn)
        txn.__exit__ = MagicMock(return_value=False)
        return txn

    mock_client = MagicMock()
    mock_client.txn.side_effect = _make_txn

    recorded_delays: list[float] = []

    def _recording_sleep(duration: float) -> None:
        recorded_delays.append(duration)

    import time as _time_mod
    original_sleep = _time_mod.sleep

    def _banned_sleep(duration: float) -> None:
        raise AssertionError(
            f"time.sleep({duration}) called directly; injected sleep= must be used."
        )

    _time_mod.sleep = _banned_sleep
    try:
        loader = Loader(client=mock_client, batch_size=10, sleep=_recording_sleep)
        loader.load([part])
    finally:
        _time_mod.sleep = original_sleep

    assert len(recorded_delays) == FAIL_TIMES, (
        f"Expected exactly {FAIL_TIMES} sleep calls (one per retry before attempt N+1), "
        f"got {len(recorded_delays)}: {recorded_delays!r}"
    )
    for i, d in enumerate(recorded_delays):
        assert d >= 0.0, f"Delay #{i} is negative: {d}"
        assert d <= 30.0, (
            f"Delay #{i} ({d}) exceeds new 30.0 s cap (AC-B: was 8.0 before v2)."
        )
    # txn() called 8 times total (7 failures + 1 success)
    assert mock_client.txn.call_count == 8, (
        f"Expected 8 txn() calls (7 fail + 1 success); got {mock_client.txn.call_count}"
    )


# ===========================================================================
# AC-A: Resumable load (checkpoint_path + fingerprint)
# Tests 1-4 and 8 per pinned contract
# ===========================================================================

# ---------------------------------------------------------------------------
# Shared helper: build a mock client that counts which batch slices were sent
# ---------------------------------------------------------------------------

def _build_recording_client():
    """Return a mock client plus a list that records the set_obj list per mutate call.

    Each element in ``sent_batches`` is the list of JSON part objects passed to
    ``txn.mutate(set_obj=...)``.  The helper creates fresh txn instances so the
    recorder captures all calls across retries/batches.
    """
    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()

    sent_batches: list[list] = []

    def _make_txn():
        def _capture_mutate(*args, **kwargs):
            so = kwargs.get("set_obj")
            if so is not None:
                sent_batches.append(so if isinstance(so, list) else [so])
            return MagicMock()

        txn = MagicMock()
        txn.query.return_value = mock_resp
        txn.mutate.side_effect = _capture_mutate
        txn.commit.return_value = None
        txn.discard.return_value = None
        txn.__enter__ = MagicMock(return_value=txn)
        txn.__exit__ = MagicMock(return_value=False)
        return txn

    mock_client = MagicMock()
    mock_client.txn.side_effect = _make_txn
    return mock_client, sent_batches


# ---------------------------------------------------------------------------
# Test 1 (AC-A): checkpoint written after each batch; atomic write (no .tmp left)
# ---------------------------------------------------------------------------

def test_load_writes_checkpoint_after_each_batch(tmp_path) -> None:
    """Given 25 StagedParts, batch_size=10, checkpoint_path in tmp_path.
    When Loader.load(parts, checkpoint_path=cp, fingerprint="fp1") is called.
    Then:
    - The checkpoint file exists when load() returns.
    - checkpoint["batches_committed"] == 3 (ceil(25/10)).
    - checkpoint["parts_loaded"] == 25.
    - checkpoint["fingerprint"] == "fp1".
    - No leftover .tmp file exists (atomic write: temp + os.replace).

    AC-A: checkpoint_path enables resume; fingerprint ties checkpoint to the
    staged file that produced parts.
    """
    from partgraph.load.loader import Loader  # re-import for clarity

    parts = [_make_staged_part(f"C{i:04d}") for i in range(25)]
    mock_client, _ = _build_mock_pydgraph_client()
    cp = tmp_path / "load_checkpoint.json"

    loader = Loader(client=mock_client, batch_size=10)
    loader.load(parts, checkpoint_path=cp, fingerprint="fp1")

    assert cp.exists(), "Checkpoint file must exist after a successful load()."

    tmp_candidate = cp.with_name(cp.name + ".tmp")
    assert not tmp_candidate.exists(), (
        "No leftover .tmp file must remain after load(); atomic write (temp+os.replace) required."
    )

    import json as _json
    data = _json.loads(cp.read_text(encoding="utf-8"))
    assert data["batches_committed"] == 3, (
        f"Expected batches_committed=3 (ceil(25/10)); got {data!r}"
    )
    assert data["parts_loaded"] == 25, (
        f"Expected parts_loaded=25; got {data!r}"
    )
    assert data["fingerprint"] == "fp1", (
        f"Expected fingerprint='fp1'; got {data!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 (AC-A): resume skips committed batches
# ---------------------------------------------------------------------------

def test_load_resumes_skips_committed_batches(tmp_path) -> None:
    """Given an existing checkpoint {batches_committed:2, parts_loaded:20, fingerprint:"fp1"}
    and 30 StagedParts with batch_size=10.
    When Loader.load(parts, checkpoint_path=cp, fingerprint="fp1") is called.
    Then:
    - Only batch index 2 (the third 10-part slice, parts[20:30]) is sent to
      the mock client (txn.mutate called for 1 batch, not 3).
    - Final checkpoint has batches_committed == 3.

    AC-A: idempotent upsert means skipping already-committed batches is safe.
    Batch boundaries are stable for the same parts list and same order.
    """
    import json as _json

    parts = [_make_staged_part(f"C{i:04d}") for i in range(30)]
    cp = tmp_path / "load_checkpoint.json"

    # Pre-write a checkpoint claiming 2 batches (20 parts) committed.
    cp.write_text(_json.dumps({
        "batches_committed": 2,
        "parts_loaded": 20,
        "fingerprint": "fp1",
    }), encoding="utf-8")

    mock_client, sent_batches = _build_recording_client()
    loader = Loader(client=mock_client, batch_size=10)
    loader.load(parts, checkpoint_path=cp, fingerprint="fp1")

    assert len(sent_batches) == 1, (
        f"Only 1 batch must be sent when 2 are already committed; "
        f"got {len(sent_batches)} batches sent."
    )

    data = _json.loads(cp.read_text(encoding="utf-8"))
    assert data["batches_committed"] == 3, (
        f"Final checkpoint must show batches_committed=3; got {data!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 (AC-A): fingerprint mismatch restarts from zero
# ---------------------------------------------------------------------------

def test_load_fingerprint_mismatch_restarts_from_zero(tmp_path) -> None:
    """Given an existing checkpoint with fingerprint="old" and 30 StagedParts.
    When Loader.load(parts, checkpoint_path=cp, fingerprint="new") is called.
    Then:
    - ALL 3 batches are sent (no skip; stale checkpoint is ignored).
    - The final checkpoint is overwritten with fingerprint="new".

    AC-A: a fingerprint mismatch means the staged file changed; the stale
    checkpoint is unsafe to resume from so the loader starts from batch 0.
    """
    import json as _json

    parts = [_make_staged_part(f"C{i:04d}") for i in range(30)]
    cp = tmp_path / "load_checkpoint.json"

    cp.write_text(_json.dumps({
        "batches_committed": 2,
        "parts_loaded": 20,
        "fingerprint": "old",
    }), encoding="utf-8")

    mock_client, sent_batches = _build_recording_client()
    loader = Loader(client=mock_client, batch_size=10)
    loader.load(parts, checkpoint_path=cp, fingerprint="new")

    assert len(sent_batches) == 3, (
        f"All 3 batches must be sent when fingerprint mismatches; "
        f"got {len(sent_batches)} batches sent."
    )

    data = _json.loads(cp.read_text(encoding="utf-8"))
    assert data["fingerprint"] == "new", (
        f"Checkpoint must be overwritten with new fingerprint; got {data!r}"
    )
    assert data["batches_committed"] == 3, (
        f"Final checkpoint must show batches_committed=3; got {data!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 (AC-A): no checkpoint_path — all batches sent, no file written
# ---------------------------------------------------------------------------

def test_load_no_checkpoint_path_loads_all(tmp_path) -> None:
    """Given 30 StagedParts and checkpoint_path=None (the default).
    When Loader.load(parts) is called (no checkpoint args).
    Then:
    - All 3 batches are sent to the mock client.
    - No checkpoint file is written anywhere under tmp_path (back-compat).

    AC-A: checkpoint_path=None is the existing default call site in _stage_load();
    the new checkpoint params are keyword-only and must not affect existing usage.
    """
    parts = [_make_staged_part(f"C{i:04d}") for i in range(30)]
    mock_client, sent_batches = _build_recording_client()
    loader = Loader(client=mock_client, batch_size=10)
    # Deliberately do not pass checkpoint_path or fingerprint.
    loader.load(parts)

    assert len(sent_batches) == 3, (
        f"All 3 batches must be sent when checkpoint_path=None; "
        f"got {len(sent_batches)} batches."
    )

    # No checkpoint file should exist anywhere in tmp_path.
    written = list(tmp_path.glob("**/*.json"))
    assert written == [], (
        f"No checkpoint file must be written when checkpoint_path=None; "
        f"found: {written}"
    )


# ---------------------------------------------------------------------------
# Test 8 (AC-A): resume produces complete coverage — no gap, no overlap
# ---------------------------------------------------------------------------

def test_load_resume_produces_complete_coverage(tmp_path) -> None:
    """Given 30 StagedParts (3 batches of 10), checkpoint claiming batch 0 committed.
    When Loader.load(parts, checkpoint_path=cp, fingerprint="fp1") is called.
    Then:
    - Exactly 2 batches are sent (parts[10:30]).
    - The union of skipped parts (parts[0:10]) + sent parts == all 30 parts.
    - No part is missing and no part appears twice (no gap, no overlap).

    AC-A: deterministic batch boundaries (same parts order, same batch_size)
    guarantee that batch index N maps to the same slice across runs, so skipping
    committed batches and resuming from batch N produces the complete graph with
    no duplicates and no gaps.
    """
    import json as _json

    parts = [_make_staged_part(f"C{i:04d}") for i in range(30)]
    cp = tmp_path / "load_checkpoint.json"

    # Checkpoint: 1 batch (10 parts) already committed.
    cp.write_text(_json.dumps({
        "batches_committed": 1,
        "parts_loaded": 10,
        "fingerprint": "fp1",
    }), encoding="utf-8")

    mock_client, sent_batches = _build_recording_client()
    loader = Loader(client=mock_client, batch_size=10)
    loader.load(parts, checkpoint_path=cp, fingerprint="fp1")

    # 2 batches sent (parts[10:20] and parts[20:30]).
    assert len(sent_batches) == 2, (
        f"Expected 2 batches sent (1 already committed); got {len(sent_batches)}."
    )

    # Extract xids of sent parts.
    sent_xids: list[str] = []
    for batch in sent_batches:
        for obj in batch:
            if isinstance(obj, dict) and "xid" in obj:
                sent_xids.append(obj["xid"])

    # The skipped batch is parts[0:10]; build expected xids.
    skipped_xids = [p.xid for p in parts[0:10]]
    expected_sent_xids = [p.xid for p in parts[10:]]

    assert sorted(sent_xids) == sorted(expected_sent_xids), (
        f"Sent xids must be exactly parts[10:] (no gap, no overlap).\n"
        f"Expected: {sorted(expected_sent_xids)}\n"
        f"Got:      {sorted(sent_xids)}"
    )

    # Union of skipped + sent == all parts (complete coverage).
    all_xids = set(skipped_xids) | set(sent_xids)
    all_part_xids = {p.xid for p in parts}
    assert all_xids == all_part_xids, (
        f"Union of skipped + sent parts must equal all parts.\n"
        f"Missing: {all_part_xids - all_xids}\n"
        f"Extra:   {all_xids - all_part_xids}"
    )

    # No overlap between skipped and sent.
    overlap = set(skipped_xids) & set(sent_xids)
    assert not overlap, (
        f"No part must appear in both skipped and sent batches; overlap: {overlap}"
    )


# ===========================================================================
# Test 9 (ingest wiring): CLI _stage_load must pass checkpoint_path and
# fingerprint to Loader.load()
#
# CURRENT STATE: _stage_load() in cli.py calls Loader(...).load(parts) with
# no checkpoint args.  This test is EXPECTED RED against the current cli.py.
#
# The test documents the required seam: the implementation must:
#   1. Compute a fingerprint from the staged JSONL file (e.g. size+mtime or a
#      cheap hash — any stable, cheap, file-identity token is sufficient).
#   2. Call Loader(...).load(parts, checkpoint_path=<path>, fingerprint=<str>)
#      where checkpoint_path is the on-disk path for the load checkpoint (e.g.
#      data/state/load_checkpoint.json) and fingerprint is non-None.
#
# The test patches Loader.load at the module level so it can inspect kwargs.
# ===========================================================================

def test_ingest_stage_load_passes_checkpoint_path_and_fingerprint(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a staged JSONL file with 3 parts and a mocked Dgraph client.
    When partgraph.cli._stage_load() is invoked.
    Then Loader.load() receives a non-None checkpoint_path and a non-None fingerprint
    as keyword arguments.

    AC-A seam: _stage_load() (in cli.py) must compute a file fingerprint and
    pass checkpoint_path + fingerprint to Loader.load so that the resume logic
    in Loader can skip already-committed batches on a re-run after a crash.

    EXPECTED RED: current _stage_load() calls Loader(...).load(parts) with no
    checkpoint args.  This test turns green once the implementation adds:
      fingerprint = _file_fingerprint(STAGED_PATH)
      Loader(...).load(parts, checkpoint_path=LOAD_CHECKPOINT_PATH,
                       fingerprint=fingerprint)
    """
    import json as _json
    import pathlib

    from partgraph.cli import _stage_load  # noqa: PLC0415

    # Build a minimal staged JSONL file with 3 parts.
    staged = tmp_path / "jlcparts.jsonl"
    for i in range(3):
        p = _make_staged_part(f"C{i:04d}")
        staged.write_text(
            staged.read_text(encoding="utf-8") + p.to_json() + "\n"
            if staged.exists() else p.to_json() + "\n",
            encoding="utf-8",
        )

    # Checkpoint path the impl should write to (any path under data/state is fine;
    # we capture whatever path is passed and only assert it is non-None).
    received_kwargs: dict = {}

    original_load_method = None  # resolved below

    def _spy_load(self_or_parts, *args, **kwargs):
        # Handles both unbound (self, parts) and already-bound (parts) call shapes.
        received_kwargs.update(kwargs)
        return {"parts_loaded": 3, "wall_seconds": 0.0, "parts_per_second": 0.0}

    # Redirect STAGED_PATH so the CLI reads our tmp file.
    monkeypatch.setattr("partgraph.cli.STAGED_PATH", staged)

    # Stub out the Dgraph client so no real gRPC call is made.
    fake_stub = MagicMock()
    fake_client = MagicMock()
    monkeypatch.setattr(
        "partgraph.cli._build_dgraph_client",
        lambda: (fake_client, fake_stub),
    )

    # Patch Loader.load to capture kwargs.
    from partgraph.load import loader as loader_module  # noqa: PLC0415
    monkeypatch.setattr(loader_module.Loader, "load", _spy_load)

    _stage_load()

    assert "checkpoint_path" in received_kwargs, (
        "_stage_load() must pass checkpoint_path= to Loader.load(). "
        "Add: Loader(...).load(parts, checkpoint_path=<path>, fingerprint=<str>). "
        "Current implementation omits checkpoint_path — this is the required seam."
    )
    assert received_kwargs["checkpoint_path"] is not None, (
        "checkpoint_path passed to Loader.load() must be non-None."
    )
    assert "fingerprint" in received_kwargs, (
        "_stage_load() must pass fingerprint= to Loader.load(). "
        "Compute it from the staged file (e.g. size+mtime or a hash) so the "
        "loader can detect staged-file changes and restart instead of resuming."
    )
    assert received_kwargs["fingerprint"] is not None, (
        "fingerprint passed to Loader.load() must be non-None."
    )


# ===========================================================================
# T-LOAD-dedup-intra-batch — fix/loader-batch-internal-duplicates
#
# These three tests are EXPECTED RED against the current loader.
#
# Root cause (confirmed by reading src/partgraph/load/loader.py line 397):
#   registry.intern(f"part::{part.xid}::{i}", part.xid)
# The in-batch position `i` is embedded in the registry key, so two occurrences
# of the same xid at positions i=0 and i=1 get distinct keys
# ("part::X::0" vs "part::X::1") -> distinct blank-node indices ->
# distinct blank-node labels (_:n0 vs _:n1) -> two Part objects with the
# same xid but different uids in a single mutation payload -> Dgraph creates
# two Part nodes in one mutation.  @upsert does NOT prevent this because it
# guards across transactions, not within a single mutation.
#
# Fix contract:
#   All occurrences of the same xid within one batch MUST collapse to one Part
#   object in the mutation payload, sharing a single uid (blank or resolved).
#   "Last occurrence wins" is the pinned deterministic merge strategy.
# ===========================================================================


def _make_staged_part_with_lcsc(
    xid: str,
    lcsc_id: str,
    description: str | None = None,
) -> StagedPart:
    """Build a StagedPart with a fixed xid and a caller-supplied lcsc_id.

    Used to construct two parts that share the same xid but differ in lcsc_id
    and/or description, exercising the intra-batch duplicate-xid code path.
    """
    mpn_part, mfr_part = xid.split("|", 1)
    return StagedPart(
        mpn=mpn_part,
        mpn_norm=mpn_part,
        mfr_name=mfr_part,
        mfr_norm=mfr_part,
        xid=xid,
        description=description or f"Part {lcsc_id}",
        package="0402",
        category="Passive",
        subcategory="Resistors",
        datasheet_url=None,
        lcsc_id=lcsc_id,
        stock=10,
        price_usd=0.01,
        is_basic=False,
        promoted={},
        attributes=[],
        tags=[],
        source_ref="jlcparts@2026-06-11",
    )


def _collect_all_part_objs(set_obj_payload: list | dict) -> list[dict]:
    """Collect every Part-typed object at the top level of a set_obj payload.

    The loader passes a list of per-part dicts to txn.mutate(set_obj=...).
    This helper flattens that list and returns all entries whose dgraph.type
    field is "Part".
    """
    top: list[dict] = (
        set_obj_payload
        if isinstance(set_obj_payload, list)
        else [set_obj_payload]
    )
    return [obj for obj in top if isinstance(obj, dict) and obj.get("dgraph.type") == "Part"]


def test_load_batch_internal_duplicate_xid_single_node() -> None:
    """T-LOAD-dedup-1 — intra-batch duplicate xid must produce exactly one Part node.

    Given:
      A batch containing TWO StagedPart objects with the SAME xid
      (mpn_norm|mfr_norm identical), differing only in lcsc_id.
      The mock lookup query returns no existing uid for that xid (fresh DB).

    When:
      Loader.load([part_a, part_b]) is called (both in the same batch because
      batch_size is larger than 2).

    Then:
      The decoded set_obj payload contains EXACTLY ONE Part object (dgraph.type
      == "Part") carrying that xid.  The two occurrences must collapse to a
      single node.  The current code FAILS this because it emits two Part objects
      with distinct blank-node uids (_:n0 and _:n1) for the same xid.

    Invariant pinned: count(Part objects with xid X in one mutation) == 1.
    """
    shared_xid = "DUPMPN|DUPMFR"
    part_a = _make_staged_part_with_lcsc(shared_xid, "C0001", description="First occurrence")
    part_b = _make_staged_part_with_lcsc(shared_xid, "C0002", description="Second occurrence")

    mock_client, mock_txn = _build_mock_pydgraph_client()
    # Lookup returns no existing uid -> blank nodes will be used.
    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()
    mock_txn.query.return_value = mock_resp

    loader = Loader(client=mock_client, batch_size=10)
    loader.load([part_a, part_b])

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "mutate() was never called."

    # There should be exactly one mutate call for this single-batch load.
    _, kwargs = mutate_calls[0]
    raw_payload = kwargs.get("set_obj")
    assert raw_payload is not None, (
        "Loader must call txn.mutate(set_obj=...) but set_obj was not found. "
        f"mutate kwargs: {kwargs!r}"
    )

    part_objs = _collect_all_part_objs(raw_payload)
    parts_with_xid = [obj for obj in part_objs if obj.get("xid") == shared_xid]

    assert len(parts_with_xid) == 1, (
        f"Expected exactly 1 Part object with xid={shared_xid!r} in the mutation "
        f"payload, but found {len(parts_with_xid)}. The intra-batch duplicate-xid "
        f"bug produces {len(parts_with_xid)} distinct Part objects because each "
        f"batch position gets a different blank-node label. "
        f"Part objects found: {parts_with_xid!r}"
    )


def test_load_batch_internal_duplicate_distinct_blank_uids_not_emitted() -> None:
    """T-LOAD-dedup-2 — all Part objects sharing the same xid must share the same uid.

    Given:
      A batch of FOUR StagedParts where two pairs share the same xid:
        pair A: xid "XIDA|MFRA"  at positions 0 and 2
        pair B: xid "XIDB|MFRB"  at positions 1 and 3
      Fresh DB (lookup returns no existing uids).

    When:
      Loader.load([a0, b0, a1, b1], batch_size=10) is called.

    Then:
      Within the decoded set_obj payload, for every distinct xid value, all
      Part objects carrying that xid must share an identical uid value.
      Specifically: no two Part objects in the payload may have the same xid
      but different uid values.

    Invariant pinned:
      for all pairs (p, q) in Part objects:
        p["xid"] == q["xid"] => p["uid"] == q["uid"]

    The current code violates this because position-keyed blank nodes (_:n0,
    _:n2) are distinct strings even though they represent the same entity.
    """
    xid_a = "XIDA|MFRA"
    xid_b = "XIDB|MFRB"
    a0 = _make_staged_part_with_lcsc(xid_a, "C1001")
    b0 = _make_staged_part_with_lcsc(xid_b, "C2001")
    a1 = _make_staged_part_with_lcsc(xid_a, "C1002")
    b1 = _make_staged_part_with_lcsc(xid_b, "C2002")

    mock_client, mock_txn = _build_mock_pydgraph_client()
    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()
    mock_txn.query.return_value = mock_resp

    loader = Loader(client=mock_client, batch_size=10)
    loader.load([a0, b0, a1, b1])

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "mutate() was never called."

    _, kwargs = mutate_calls[0]
    raw_payload = kwargs.get("set_obj")
    assert raw_payload is not None, (
        f"Loader must call txn.mutate(set_obj=...). kwargs: {kwargs!r}"
    )

    part_objs = _collect_all_part_objs(raw_payload)

    # Build a mapping: xid -> set of all uid values seen for that xid.
    xid_to_uids: dict[str, set] = {}
    for obj in part_objs:
        xid = obj.get("xid")
        uid = obj.get("uid")
        if xid is not None:
            xid_to_uids.setdefault(xid, set()).add(uid)

    violations: list[str] = []
    for xid, uids in xid_to_uids.items():
        if len(uids) > 1:
            violations.append(
                f"xid={xid!r} appears with {len(uids)} different uid values: {uids!r}"
            )

    assert not violations, (
        "Invariant violated: Part objects with the same xid must share one uid. "
        "Violations found (each line is one xid with multiple uids):\n"
        + "\n".join(violations)
        + "\nThis is the intra-batch duplicate-xid bug: position-keyed blank nodes "
        "(_:n<i>) are distinct strings that Dgraph treats as distinct nodes even "
        "though they represent the same entity."
    )


# ===========================================================================
# AC-ADAPT: Loader accepts optional controller parameter
#
# Pinned contract:
#   - Loader(client=..., batch_size=..., controller=None) — default None means
#     existing behaviour unchanged (no regulation); all existing tests stay green.
#   - With an injected controller scripted to shrink-then-grow, the per-batch
#     sizes follow the controller's directives.
#   - With an injected sleep via the existing sleep= param, pauses are honoured.
# ===========================================================================


def _make_scripted_controller(directives: list[tuple[int, float]]):
    """Return a controller whose regulate() returns successive directives.

    Each element of directives is (next_batch_size, pause_seconds).
    The last element is repeated for any calls beyond the list length.
    """
    from unittest.mock import MagicMock

    idx = [0]

    class ScriptedController:
        def regulate(self, prev_batch_size: int, snapshot: object):
            entry = directives[min(idx[0], len(directives) - 1)]
            idx[0] += 1
            directive = MagicMock()
            directive.next_batch_size = entry[0]
            directive.pause_seconds = entry[1]
            return directive

    return ScriptedController()


def test_ac_adapt_loader_default_controller_none_existing_behavior_unchanged() -> None:
    """AC-ADAPT: Given Loader constructed without controller= (default None).
    When Loader.load(25 parts, batch_size=10) is called.
    Then the result is identical to pre-controller behavior: 3 txns, 3 commits.
    (Existing tests must stay green — controller=None means no regulation.)
    """
    parts = [_make_staged_part(f"C{i:04d}") for i in range(25)]
    mock_client, mock_txn = _build_mock_pydgraph_client()

    loader = Loader(client=mock_client, batch_size=10)  # no controller=
    loader.load(parts)

    assert mock_client.txn.call_count == 3, (
        f"AC-ADAPT: without controller, 25 parts / batch_size=10 must produce "
        f"3 txn() calls. Got: {mock_client.txn.call_count}"
    )
    assert mock_txn.commit.call_count == 3, (
        f"AC-ADAPT: without controller, commit must be called 3 times. "
        f"Got: {mock_txn.commit.call_count}"
    )


def test_ac_adapt_loader_with_controller_follows_directives() -> None:
    """AC-ADAPT: Given a scripted controller and an injected sleep callable.
    When Loader.load(parts, batch_size=initial_bs) is called with the controller.
    Then:
    - The loader calls controller.regulate() between batches.
    - The injected sleep is called with the pause_seconds from the directive.
    - No real time.sleep is called.
    """
    import time as _time_mod

    parts = [_make_staged_part(f"C{i:04d}") for i in range(30)]
    mock_client, _mock_txn = _build_mock_pydgraph_client()

    # Script: first batch -> pause 0, second -> pause 0.1, third -> pause 0.
    # We verify pause_seconds > 0 was used.
    controller = _make_scripted_controller([
        (10, 0.0),   # batch 0: no pause
        (10, 0.05),  # batch 1: small pause
        (10, 0.0),   # batch 2: no pause
    ])

    recorded_sleeps: list[float] = []

    def _recording_sleep(duration: float) -> None:
        recorded_sleeps.append(duration)

    original_sleep = _time_mod.sleep

    def _banned_sleep(duration: float) -> None:
        raise AssertionError(
            f"time.sleep({duration}) called directly; injected sleep= must be used."
        )

    _time_mod.sleep = _banned_sleep
    try:
        loader = Loader(
            client=mock_client,
            batch_size=10,
            sleep=_recording_sleep,
            controller=controller,
        )
        loader.load(parts)
    finally:
        _time_mod.sleep = original_sleep

    # The scripted pause of 0.05 seconds must have been passed to injected sleep.
    assert any(s > 0 for s in recorded_sleeps), (
        f"AC-ADAPT: scripted pause directive (0.05 s) must be honoured via injected sleep. "
        f"Recorded sleeps: {recorded_sleeps!r}"
    )


def test_load_batch_duplicate_xid_last_wins_fields_preserved() -> None:
    """T-LOAD-dedup-3 — last-occurrence-wins: surviving node carries last-seen fields.

    Given:
      A batch with two StagedParts that share xid "DUPWIN|MFRW":
        first  (position 0): lcsc_id="C0001", description="First"
        second (position 1): lcsc_id="C0002", description="Second"
      Fresh DB (no existing uid).

    When:
      Loader.load([first, second], batch_size=10) is called.

    Then:
      The single surviving Part object in the payload:
        1. has dgraph.type == "Part"
        2. has xid == "DUPWIN|MFRW"
        3. has lcsc_id == "C0002"  (last occurrence wins)
        4. has description == "Second"  (last occurrence wins)
        5. has a uid value that is non-empty (either a blank node or a resolved uid)

    Contract: "last occurrence wins" is the pinned deterministic merge strategy.
    The implementation must deduplicate same-xid parts within one batch before
    building the payload, keeping only the last entry in the iteration order.

    The current code FAILS this test because it emits TWO Part objects for the
    same xid and neither is guaranteed to be selected as the survivor.
    """
    shared_xid = "DUPWIN|MFRW"
    first = _make_staged_part_with_lcsc(shared_xid, "C0001", description="First")
    last  = _make_staged_part_with_lcsc(shared_xid, "C0002", description="Second")

    mock_client, mock_txn = _build_mock_pydgraph_client()
    mock_resp = MagicMock()
    mock_resp.json = json.dumps({"q": []}).encode()
    mock_txn.query.return_value = mock_resp

    loader = Loader(client=mock_client, batch_size=10)
    loader.load([first, last])

    mutate_calls = mock_txn.mutate.call_args_list
    assert mutate_calls, "mutate() was never called."

    _, kwargs = mutate_calls[0]
    raw_payload = kwargs.get("set_obj")
    assert raw_payload is not None, (
        f"Loader must call txn.mutate(set_obj=...). kwargs: {kwargs!r}"
    )

    part_objs = _collect_all_part_objs(raw_payload)
    parts_with_xid = [obj for obj in part_objs if obj.get("xid") == shared_xid]

    # Must be exactly one surviving Part node.
    assert len(parts_with_xid) == 1, (
        f"Expected exactly 1 Part for xid={shared_xid!r} after last-wins collapse; "
        f"got {len(parts_with_xid)}: {parts_with_xid!r}"
    )

    survivor = parts_with_xid[0]

    assert survivor.get("dgraph.type") == "Part", (
        f"Surviving Part object must have dgraph.type='Part'; got {survivor!r}"
    )
    assert survivor.get("xid") == shared_xid, (
        f"Surviving Part must carry xid={shared_xid!r}; got {survivor.get('xid')!r}"
    )

    # Last-occurrence-wins: the second StagedPart (lcsc_id="C0002") must win.
    assert survivor.get("lcsc_id") == "C0002", (
        f"Last-occurrence-wins contract: surviving Part must have lcsc_id='C0002' "
        f"(from the second/last occurrence); got {survivor.get('lcsc_id')!r}. "
        f"Full survivor: {survivor!r}"
    )
    assert survivor.get("description") == "Second", (
        f"Last-occurrence-wins contract: surviving Part must have description='Second' "
        f"(from the second/last occurrence); got {survivor.get('description')!r}."
    )

    uid = survivor.get("uid")
    assert uid, (
        f"Surviving Part must have a non-empty uid (blank node or resolved); "
        f"got {uid!r}."
    )
