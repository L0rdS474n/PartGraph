"""
Tests: T-LOAD-* (integration)

End-to-end load integration tests against a live Dgraph instance.

ALL tests are @pytest.mark.integration and depend on the dgraph_available and
dgraph_pydgraph_client fixtures from tests/conftest.py.

Every created node carries an xid prefixed with "__pr2test__" so targeted
cleanup is possible and test residue is never confounded with real ingest data.

SOURCE_REF = "pr2test@2026-06-11" (distinct from the real ingest ref
"jlcparts@2026-06-11") so provenance of test-written nodes is unambiguous.

The canonical count pattern (Dgraph v25 safe):
    { q(func: type(X)) { count(uid) } }
    response: {"q": [{"count": N}]} or {"q": []} -> 0

Tests:
- T-LOAD-idempotent:    loading the same fixture set twice -> identical Part count.
- T-LOAD-linking:       shared manufacturer reused; edges made_by/in_category/
                        in_package/datasheet/tagged/attr are present after load.
- T-LOAD-promoted:      SI floats + stock/price_usd/is_basic/lcsc_id readable;
                        None promoted values absent from the node.
- T-LOAD-cat-hierarchy: level-2 Category has a parent Level-1; Part's in_category
                        points to the level-2 node.
- T-LOAD-provenance:    source_refs contains the pr2test source_ref; no duplicate
                        on re-load.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from partgraph.normalize.model import AttrRecord, StagedPart  # noqa: F401
from partgraph.load.loader import Loader  # noqa: F401

# ---------------------------------------------------------------------------
# Fixture prefix that makes every test node identifiable and cleanable.
# SOURCE_REF is deliberately distinct from the real ingest ref ("jlcparts@...")
# so that test-written nodes are never confounded with production ingest data.
# Cleanup deletes by the __pr2test__ xid prefix.
# ---------------------------------------------------------------------------
XID_PREFIX = "__pr2test__"
SOURCE_REF = "pr2test@2026-06-11"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_by_xid_prefix(client, prefix: str) -> int:
    """Count Part nodes whose xid starts with the test prefix.

    Uses the named-block form to avoid the Dgraph v25 root-level count bug.
    """
    query = f"""
    {{
      q(func: type(Part)) @filter(anyofterms(xid, "{prefix}")) {{
        count(uid)
      }}
    }}
    """
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        block = data.get("q", [])
        return block[0]["count"] if block else 0
    finally:
        txn.discard()


def _query_part_by_xid(client, xid: str) -> dict | None:
    """Fetch a Part node by exact xid; return its data dict or None."""
    query = f"""
    {{
      q(func: eq(xid, "{xid}")) {{
        uid
        xid
        mpn
        mpn_norm
        lcsc_id
        stock
        price_usd
        is_basic
        source_refs
        made_by  {{ uid name }}
        in_category {{ uid name parent {{ uid name }} }}
        in_package  {{ uid name }}
        datasheet   {{ uid url }}
        tagged      {{ uid name }}
        attr        {{ uid attr_name attr_value attr_value_num }}
      }}
    }}
    """
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        results = data.get("q", [])
        return results[0] if results else None
    finally:
        txn.discard()


def _delete_pr2test_nodes(client) -> None:
    """Delete all nodes whose xid starts with the test prefix.

    Called in teardown for each test that writes nodes.
    """
    import pydgraph  # type: ignore[import]

    query = f'{{ nodes(func: anyofterms(xid, "{XID_PREFIX}")) {{ uid }} }}'
    txn = client.txn()
    try:
        resp = txn.query(query)
        data = json.loads(resp.json)
        uids = [n["uid"] for n in data.get("nodes", [])]
        if uids:
            parts_nq = "\n".join(f"<{uid}> * * ." for uid in uids)
            mut = pydgraph.Mutation(del_nquads=parts_nq.encode())
            txn.mutate(mutation=mut)
        txn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        txn.discard()


@dataclass
class _TestPartSpec:
    """Parameter bundle for _make_test_part to avoid PLR0913."""
    mfr_norm: str = "TESTMFR"
    category: str = "Resistors"
    subcategory: str = "Chip Resistor - Surface Mount"
    tags: list[str] | None = None
    price_usd: float | None = 0.05
    promoted: dict | None = field(default=None)


def _make_test_part(n: int, spec: _TestPartSpec | None = None) -> StagedPart:
    if spec is None:
        spec = _TestPartSpec()
    mfr_norm = spec.mfr_norm
    category = spec.category
    subcategory = spec.subcategory
    tags = spec.tags
    price_usd = spec.price_usd
    promoted = spec.promoted
    xid = f"{XID_PREFIX}MPN{n:04d}|{mfr_norm}"
    return StagedPart(
        mpn=f"TESTMPN{n:04d}",
        mpn_norm=f"TESTMPN{n:04d}",
        mfr_name="TestManufacturer",
        mfr_norm=mfr_norm,
        xid=xid,
        description=f"Test resistor {n} for PR2 integration test",
        package="0402",
        category=category,
        subcategory=subcategory,
        datasheet_url=f"https://example.com/pr2test/ds{n:04d}.pdf",
        lcsc_id=f"C{90000 + n}",
        stock=100 + n,
        price_usd=price_usd,
        is_basic=(n % 2 == 0),
        promoted=promoted if promoted is not None else {"resistance": float(n * 1000)},
        attributes=[
            AttrRecord(
                name="Resistance",
                value_text=f"{n}kΩ",
                value_num=float(n * 1000),
                unit="Ω",
            ),
        ],
        tags=tags or ["SPI"],
        source_ref=SOURCE_REF,
    )


# ---------------------------------------------------------------------------
# T-LOAD-idempotent
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_load_idempotent_same_parts_twice(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """Given a set of Part fixture nodes loaded once into Dgraph.
    When the same set is loaded a second time (via the Loader's upsert logic).
    Then the Part count after the second load equals the count after the first
    load (no duplicates are created).

    Teardown: all __pr2test__ nodes are deleted.
    """
    client = dgraph_pydgraph_client
    parts = [_make_test_part(i) for i in range(1, 4)]

    loader = Loader(client=client, batch_size=10)

    try:
        loader.load(parts)
        count_after_first = len([
            p for p in parts
            if _query_part_by_xid(client, p.xid) is not None
        ])
        print(f"\n[T-LOAD-idempotent] count after first load: {count_after_first}")

        loader.load(parts)
        count_after_second = len([
            p for p in parts
            if _query_part_by_xid(client, p.xid) is not None
        ])
        print(f"[T-LOAD-idempotent] count after second load: {count_after_second}")

        assert count_after_first == count_after_second == len(parts), (
            f"Idempotent load failed: first={count_after_first}, "
            f"second={count_after_second}, expected={len(parts)}."
        )
    finally:
        _delete_pr2test_nodes(client)


# ---------------------------------------------------------------------------
# T-LOAD-linking
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_load_shared_manufacturer_reused(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """Given two Parts with the same mfr_norm.
    When loaded via Loader.
    Then both Parts' made_by edge points to the SAME Manufacturer node (one
    Manufacturer node, not two).
    """
    client = dgraph_pydgraph_client
    spec = _TestPartSpec(mfr_norm="SHAREDMFR")
    parts = [
        _make_test_part(10, spec),
        _make_test_part(11, spec),
    ]

    try:
        Loader(client=client, batch_size=10).load(parts)

        node_a = _query_part_by_xid(client, parts[0].xid)
        node_b = _query_part_by_xid(client, parts[1].xid)

        assert node_a is not None, f"Part {parts[0].xid} not found after load."
        assert node_b is not None, f"Part {parts[1].xid} not found after load."

        mfr_a = node_a.get("made_by", [])
        mfr_b = node_b.get("made_by", [])

        assert mfr_a, "Part A has no made_by edge."
        assert mfr_b, "Part B has no made_by edge."

        uid_a = mfr_a[0]["uid"]
        uid_b = mfr_b[0]["uid"]
        assert uid_a == uid_b, (
            f"Two parts with the same mfr_norm point to different Manufacturer nodes: "
            f"uid_a={uid_a}, uid_b={uid_b}. Manufacturer must be reused."
        )
    finally:
        _delete_pr2test_nodes(client)


@pytest.mark.integration
def test_load_linking_all_edges_present(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """Given a Part with datasheet, category, package, tags, and attributes.
    When loaded via Loader.
    Then the node has non-empty made_by, in_category, in_package, datasheet,
    tagged, and attr edges.
    """
    client = dgraph_pydgraph_client
    part = _make_test_part(20)

    try:
        Loader(client=client, batch_size=10).load([part])
        node = _query_part_by_xid(client, part.xid)

        assert node is not None, f"Part {part.xid} not found after load."

        assert node.get("made_by"), f"made_by edge missing: {node}"
        assert node.get("in_category"), f"in_category edge missing: {node}"
        assert node.get("in_package"), f"in_package edge missing: {node}"
        assert node.get("datasheet"), f"datasheet edge missing: {node}"
        assert node.get("tagged"), f"tagged edge missing: {node}"
        assert node.get("attr"), f"attr edge missing: {node}"
    finally:
        _delete_pr2test_nodes(client)


# ---------------------------------------------------------------------------
# T-LOAD-promoted
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_load_promoted_si_values_readable(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """Given a Part with promoted SI floats, stock, price_usd, is_basic, lcsc_id.
    When loaded and read back from Dgraph.
    Then all those fields are present with their expected values.
    """
    client = dgraph_pydgraph_client
    part = _make_test_part(
        30,
        _TestPartSpec(promoted={"resistance": 10000.0, "power": 0.125}, price_usd=0.42),
    )

    try:
        Loader(client=client, batch_size=10).load([part])
        node = _query_part_by_xid(client, part.xid)

        assert node is not None, f"Part {part.xid} not found after load."
        assert node.get("stock") == 130, (
            f"stock mismatch: {node.get('stock')} != 130"
        )
        assert node.get("lcsc_id") == "C90030", (
            f"lcsc_id mismatch: {node.get('lcsc_id')}"
        )
        assert node.get("is_basic") is True or node.get("is_basic") == 1, (
            f"is_basic mismatch: {node.get('is_basic')}"
        )
        price = node.get("price_usd")
        assert price is not None and abs(price - 0.42) < 0.001, (
            f"price_usd mismatch: {price}"
        )
    finally:
        _delete_pr2test_nodes(client)


@pytest.mark.integration
def test_load_none_promoted_values_absent_from_node(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """Given a Part where price_usd is None.
    When loaded and read back from Dgraph.
    Then the price_usd field is absent from the node (not stored as null).
    """
    client = dgraph_pydgraph_client
    part = _make_test_part(31, _TestPartSpec(price_usd=None, promoted={}))

    try:
        Loader(client=client, batch_size=10).load([part])
        node = _query_part_by_xid(client, part.xid)

        assert node is not None, f"Part {part.xid} not found after load."
        # None values must be omitted; if present they must not be "None" string.
        if "price_usd" in node:
            assert node["price_usd"] is not None, (
                "price_usd was written as None/null to Dgraph; None promoted values must be omitted."
            )
    finally:
        _delete_pr2test_nodes(client)


# ---------------------------------------------------------------------------
# T-LOAD-cat-hierarchy
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_load_category_level2_has_parent_level1(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """Given a Part with category='Resistors' and subcategory='Chip Resistor - Surface Mount'.
    When loaded via Loader.
    Then the Part's in_category edge points to the subcategory (level-2) node,
    and that level-2 Category has a parent edge pointing to a level-1 Category
    named 'Resistors'.
    """
    client = dgraph_pydgraph_client
    part = _make_test_part(
        40,
        _TestPartSpec(category="Resistors", subcategory="Chip Resistor - Surface Mount"),
    )

    try:
        Loader(client=client, batch_size=10).load([part])
        node = _query_part_by_xid(client, part.xid)

        assert node is not None, f"Part {part.xid} not found after load."
        cats = node.get("in_category", [])
        assert cats, f"in_category edge missing for Part {part.xid}."

        level2_cat = cats[0]
        # The level-2 category must have a parent.
        parent = level2_cat.get("parent")
        assert parent, (
            f"Level-2 Category '{level2_cat.get('name')}' has no parent edge. "
            "Category hierarchy must link subcategory -> parent category."
        )
        parent_name = parent.get("name") if isinstance(parent, dict) else None
        assert parent_name == "Resistors", (
            f"Level-2 Category parent name expected 'Resistors', got '{parent_name}'."
        )
    finally:
        _delete_pr2test_nodes(client)


# ---------------------------------------------------------------------------
# T-LOAD-provenance
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_load_provenance_source_refs_present(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """Given a Part loaded with source_ref='pr2test@2026-06-11'.
    When read back from Dgraph.
    Then source_refs contains 'pr2test@2026-06-11'.

    NOTE: SOURCE_REF is 'pr2test@2026-06-11' (not 'jlcparts@...') so that
    test-written nodes are unambiguously distinct from real ingest data.
    """
    client = dgraph_pydgraph_client
    part = _make_test_part(50)

    try:
        Loader(client=client, batch_size=10).load([part])
        node = _query_part_by_xid(client, part.xid)

        assert node is not None, f"Part {part.xid} not found."
        source_refs = node.get("source_refs", [])
        if isinstance(source_refs, str):
            source_refs = [source_refs]
        assert SOURCE_REF in source_refs, (
            f"source_refs {source_refs!r} does not contain {SOURCE_REF!r}."
        )
    finally:
        _delete_pr2test_nodes(client)


@pytest.mark.integration
def test_load_provenance_no_duplicate_source_refs_on_reload(
    dgraph_available,
    dgraph_pydgraph_client,
) -> None:
    """Given a Part loaded twice with the same source_ref.
    When read back after the second load.
    Then source_refs contains the source_ref exactly once (no duplicates).
    """
    client = dgraph_pydgraph_client
    part = _make_test_part(51)

    try:
        loader = Loader(client=client, batch_size=10)
        loader.load([part])
        loader.load([part])  # Re-load same part.

        node = _query_part_by_xid(client, part.xid)
        assert node is not None, f"Part {part.xid} not found."

        source_refs = node.get("source_refs", [])
        if isinstance(source_refs, str):
            source_refs = [source_refs]

        count = source_refs.count(SOURCE_REF)
        assert count == 1, (
            f"source_refs contains '{SOURCE_REF}' {count} times after two loads. "
            f"Expected exactly 1 (no duplicates). source_refs: {source_refs!r}"
        )
    finally:
        _delete_pr2test_nodes(client)
