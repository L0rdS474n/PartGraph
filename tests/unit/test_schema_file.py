"""
Tests: R6

Verifies that schema/partgraph.dql contains all required Dgraph predicate
declarations and type declarations.  Checks are done at the text/regex level
since DQL is not standard SQL and has no Python parser in the test environment.
"""

from __future__ import annotations

import pathlib
import re

import pytest

SCHEMA_REL = "schema/partgraph.dql"


@pytest.fixture(scope="module")
def schema_text(repo_root: pathlib.Path) -> str:
    """Return the full text of schema/partgraph.dql.

    Given the schema file exists.
    When we read it.
    Then we return its text for subsequent assertions.
    """
    schema_path = repo_root / SCHEMA_REL
    assert schema_path.exists(), f"{SCHEMA_REL} does not exist."
    assert schema_path.stat().st_size > 0, f"{SCHEMA_REL} is empty."
    return schema_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _has_predicate_with_attrs(text: str, predicate: str, *attr_fragments: str) -> bool:
    """Return True if `text` contains a line/block declaring `predicate` with
    all `attr_fragments` present somewhere in that declaration context.

    We look for the pattern:
        <predicate>: <type> @index(...) ...
    or a multi-line equivalent.  We extract up to 5 lines starting from the
    predicate name and check that all fragments appear in that window.
    """
    lines = text.splitlines()
    # Find line(s) that start the predicate declaration.
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{predicate}:") or stripped.startswith(f"{predicate} :"):
            # Gather this line plus a few continuation lines.
            window = "\n".join(lines[i : i + 5])
            if all(frag in window for frag in attr_fragments):
                return True
    return False


def _type_declared(text: str, type_name: str) -> bool:
    """Return True if `type <type_name> {` or `type <type_name>{` appears."""
    pattern = re.compile(rf"\btype\s+{re.escape(type_name)}\s*\{{", re.MULTILINE)
    return bool(pattern.search(text))


def _type_block_text(text: str, type_name: str) -> str:
    """Return the raw field-list text between `type <type_name> {` and its
    closing `}` (exclusive of the braces themselves), or "" if not found.

    DQL type blocks in this schema are flat (one predicate name per line, no
    nested `{`/`}`), so a non-greedy match to the first `}` is exact — there is
    no brace-nesting ambiguity to resolve.
    """
    pattern = re.compile(rf"\btype\s+{re.escape(type_name)}\s*\{{(.*?)\}}", re.DOTALL)
    match = pattern.search(text)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# R6 — predicate declarations
# ---------------------------------------------------------------------------

class TestPredicateDeclarations:
    def test_xid_with_exact_and_upsert(self, schema_text: str) -> None:
        """Given partgraph.dql defines the xid predicate.
        When we scan for its declaration.
        Then it must carry @index(exact) and @upsert directives.
        """
        assert _has_predicate_with_attrs(schema_text, "xid", "@index(exact)", "@upsert"), (
            "Predicate 'xid' must have @index(exact) and @upsert in schema/partgraph.dql"
        )

    def test_mpn_with_exact_and_trigram(self, schema_text: str) -> None:
        """Given partgraph.dql defines the mpn predicate.
        When we scan for its declaration.
        Then it must carry @index(exact) and @index(trigram) or combined form.
        """
        # Accept either @index(exact, trigram) or separate @index(exact) @index(trigram)
        line_window = ""
        for i, line in enumerate(schema_text.splitlines()):
            if line.lstrip().startswith("mpn:"):
                line_window = "\n".join(schema_text.splitlines()[i : i + 5])
                break
        assert line_window, "Predicate 'mpn' not found in schema/partgraph.dql"
        has_exact = "exact" in line_window
        has_trigram = "trigram" in line_window
        assert has_exact and has_trigram, (
            f"Predicate 'mpn' must have both 'exact' and 'trigram' index. Found:\n{line_window}"
        )

    def test_mpn_norm_with_exact_and_trigram(self, schema_text: str) -> None:
        """Given partgraph.dql defines the mpn_norm predicate.
        When we scan for its declaration.
        Then it must carry exact and trigram indexes.
        """
        line_window = ""
        for i, line in enumerate(schema_text.splitlines()):
            if line.lstrip().startswith("mpn_norm:"):
                line_window = "\n".join(schema_text.splitlines()[i : i + 5])
                break
        assert line_window, "Predicate 'mpn_norm' not found in schema/partgraph.dql"
        assert "exact" in line_window and "trigram" in line_window, (
            f"Predicate 'mpn_norm' must have exact and trigram. Found:\n{line_window}"
        )

    def test_family_name_with_exact_term_trigram(self, schema_text: str) -> None:
        """Given partgraph.dql defines the family_name predicate.
        When we scan for its declaration.
        Then it must carry exact, term, and trigram indexes.
        """
        line_window = ""
        for i, line in enumerate(schema_text.splitlines()):
            if line.lstrip().startswith("family_name:"):
                line_window = "\n".join(schema_text.splitlines()[i : i + 5])
                break
        assert line_window, "Predicate 'family_name' not found in schema/partgraph.dql"
        assert "exact" in line_window, f"'family_name' missing 'exact'. Window:\n{line_window}"
        assert "term" in line_window, f"'family_name' missing 'term'. Window:\n{line_window}"
        assert "trigram" in line_window, f"'family_name' missing 'trigram'. Window:\n{line_window}"

    def test_description_with_fulltext(self, schema_text: str) -> None:
        """Given partgraph.dql defines the description predicate.
        When we scan for its declaration.
        Then it must carry @index(fulltext).
        """
        assert _has_predicate_with_attrs(schema_text, "description", "fulltext"), (
            "Predicate 'description' must have @index(fulltext)"
        )

    def test_name_with_exact_term_trigram(self, schema_text: str) -> None:
        """Given partgraph.dql defines the name predicate.
        When we scan for its declaration.
        Then it must carry exact, term, and trigram indexes.
        """
        line_window = ""
        for i, line in enumerate(schema_text.splitlines()):
            if line.lstrip().startswith("name:"):
                line_window = "\n".join(schema_text.splitlines()[i : i + 5])
                break
        assert line_window, "Predicate 'name' not found in schema/partgraph.dql"
        assert "exact" in line_window, f"'name' missing 'exact'. Window:\n{line_window}"
        assert "term" in line_window, f"'name' missing 'term'. Window:\n{line_window}"
        assert "trigram" in line_window, f"'name' missing 'trigram'. Window:\n{line_window}"

    def test_url_with_exact(self, schema_text: str) -> None:
        """Given partgraph.dql defines the url predicate.
        When we scan for its declaration.
        Then it must carry @index(exact).
        """
        assert _has_predicate_with_attrs(schema_text, "url", "exact"), (
            "Predicate 'url' must have @index(exact)"
        )

    @pytest.mark.parametrize(
        "pred",
        [
            "voltage_min",
            "voltage_max",
            "current_max",
            "resistance",
            "capacitance",
            "inductance",
            "frequency_max",
            "power",
            "tolerance_pct",
        ],
    )
    def test_float_predicate_has_float_index(self, schema_text: str, pred: str) -> None:
        """Given partgraph.dql defines numeric float predicates.
        When we scan for each predicate's declaration.
        Then it must carry @index(float).
        """
        assert _has_predicate_with_attrs(schema_text, pred, "float"), (
            f"Predicate '{pred}' must have @index(float) in partgraph.dql"
        )

    def test_attr_name_with_term(self, schema_text: str) -> None:
        """Given partgraph.dql defines the attr_name predicate.
        When we scan for its declaration.
        Then it must carry @index(term).
        """
        assert _has_predicate_with_attrs(schema_text, "attr_name", "term"), (
            "Predicate 'attr_name' must have @index(term)"
        )

    def test_attr_value_with_term(self, schema_text: str) -> None:
        """Given partgraph.dql defines the attr_value predicate.
        When we scan for its declaration.
        Then it must carry @index(term).
        """
        assert _has_predicate_with_attrs(schema_text, "attr_value", "term"), (
            "Predicate 'attr_value' must have @index(term)"
        )

    def test_attr_value_num_with_float_index(self, schema_text: str) -> None:
        """Given partgraph.dql defines the attr_value_num predicate.
        When we scan for its declaration.
        Then it must carry @index(float).
        """
        assert _has_predicate_with_attrs(schema_text, "attr_value_num", "float"), (
            "Predicate 'attr_value_num' must have @index(float)"
        )

    def test_embedding_float32vector_hnsw_cosine(self, schema_text: str) -> None:
        """Given partgraph.dql defines the embedding predicate.
        When we scan for its declaration.
        Then it must be declared as float32vector with hnsw index and cosine metric.

        This is the contract for vector similarity search (AC G2).
        """
        # Must find: embedding: float32vector @index(hnsw(metric:"cosine"))
        line_window = ""
        for i, line in enumerate(schema_text.splitlines()):
            if line.lstrip().startswith("embedding:"):
                line_window = "\n".join(schema_text.splitlines()[i : i + 5])
                break
        assert line_window, "Predicate 'embedding' not found in schema/partgraph.dql"
        assert "float32vector" in line_window, (
            f"'embedding' must be of type float32vector. Found:\n{line_window}"
        )
        assert "hnsw" in line_window, (
            f"'embedding' must have hnsw index. Found:\n{line_window}"
        )
        assert "cosine" in line_window, (
            f"'embedding' hnsw index must specify metric:cosine. Found:\n{line_window}"
        )

    def test_stock_with_int_index(self, schema_text: str) -> None:
        """Given partgraph.dql defines the stock predicate.
        When we scan for its declaration.
        Then it must carry @index(int).
        """
        assert _has_predicate_with_attrs(schema_text, "stock", "int"), (
            "Predicate 'stock' must have @index(int)"
        )

    def test_lcsc_id_with_exact(self, schema_text: str) -> None:
        """Given partgraph.dql defines the lcsc_id predicate.
        When we scan for its declaration.
        Then it must carry @index(exact).
        """
        assert _has_predicate_with_attrs(schema_text, "lcsc_id", "exact"), (
            "Predicate 'lcsc_id' must have @index(exact)"
        )

    def test_source_refs_declared(self, schema_text: str) -> None:
        """Given partgraph.dql defines source_refs.
        When we scan for its declaration.
        Then a line beginning with 'source_refs:' must be present.
        """
        found = any(
            line.lstrip().startswith("source_refs:")
            for line in schema_text.splitlines()
        )
        assert found, "Predicate 'source_refs' not declared in schema/partgraph.dql"


    # -----------------------------------------------------------------------
    # Architecture review additions — edge predicates and remaining scalars
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "pred",
        ["made_by", "in_category", "in_package", "datasheet", "tagged", "attr"],
    )
    def test_list_uid_edge_with_reverse(self, schema_text: str, pred: str) -> None:
        """Given partgraph.dql defines multi-valued uid edge predicates.
        When we scan for each predicate's declaration.
        Then it must be declared as [uid] with @reverse.

        These predicates represent graph edges from Part to related nodes.
        @reverse is required so the graph can be traversed in both directions.
        """
        line_window = ""
        for i, line in enumerate(schema_text.splitlines()):
            if line.lstrip().startswith(f"{pred}:"):
                line_window = "\n".join(schema_text.splitlines()[i: i + 5])
                break
        assert line_window, (
            f"Predicate '{pred}' not found in {SCHEMA_REL}."
        )
        assert "[uid]" in line_window, (
            f"Predicate '{pred}' must be declared as [uid] (list of uids). "
            f"Found:\n{line_window}"
        )
        assert "@reverse" in line_window, (
            f"Predicate '{pred}' must carry @reverse. Found:\n{line_window}"
        )

    def test_variant_of_is_uid_with_reverse(self, schema_text: str) -> None:
        """Given partgraph.dql defines the variant_of predicate.
        When we scan for its declaration.
        Then it must be declared as uid (singular, not list) with @reverse.

        variant_of models a single parent variant relationship.
        """
        line_window = ""
        for i, line in enumerate(schema_text.splitlines()):
            if line.lstrip().startswith("variant_of:"):
                line_window = "\n".join(schema_text.splitlines()[i: i + 5])
                break
        assert line_window, f"Predicate 'variant_of' not found in {SCHEMA_REL}."
        # Must be uid (scalar), not [uid].
        assert "uid" in line_window, (
            f"Predicate 'variant_of' must be of type uid. Found:\n{line_window}"
        )
        assert "@reverse" in line_window, (
            f"Predicate 'variant_of' must carry @reverse. Found:\n{line_window}"
        )

    def test_equivalent_to_is_list_uid_with_reverse(self, schema_text: str) -> None:
        """Given partgraph.dql defines the equivalent_to predicate.
        When we scan for its declaration.
        Then it must be declared as [uid] (list) with @reverse.

        equivalent_to is symmetric and multi-valued — a part may have several
        equivalent alternatives.
        """
        line_window = ""
        for i, line in enumerate(schema_text.splitlines()):
            if line.lstrip().startswith("equivalent_to:"):
                line_window = "\n".join(schema_text.splitlines()[i: i + 5])
                break
        assert line_window, f"Predicate 'equivalent_to' not found in {SCHEMA_REL}."
        assert "[uid]" in line_window, (
            f"Predicate 'equivalent_to' must be declared as [uid]. Found:\n{line_window}"
        )
        assert "@reverse" in line_window, (
            f"Predicate 'equivalent_to' must carry @reverse. Found:\n{line_window}"
        )

    def test_price_usd_with_float_index(self, schema_text: str) -> None:
        """Given partgraph.dql defines the price_usd predicate.
        When we scan for its declaration.
        Then it must be declared as float with @index(float).
        """
        assert _has_predicate_with_attrs(schema_text, "price_usd", "float"), (
            f"Predicate 'price_usd' must have type float and @index(float) in {SCHEMA_REL}."
        )

    def test_is_basic_with_bool_index(self, schema_text: str) -> None:
        """Given partgraph.dql defines the is_basic predicate.
        When we scan for its declaration.
        Then it must be declared as bool with @index(bool).
        """
        assert _has_predicate_with_attrs(schema_text, "is_basic", "bool"), (
            f"Predicate 'is_basic' must have type bool and @index(bool) in {SCHEMA_REL}."
        )


# ---------------------------------------------------------------------------
# R6 — type declarations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "type_name",
    [
        "Part",
        "PartFamily",
        "Manufacturer",
        "Category",
        "Package",
        "Datasheet",
        "Tag",
        "AttrValue",
    ],
)
def test_type_declaration_exists(schema_text: str, type_name: str) -> None:
    """Given partgraph.dql defines the Dgraph schema.
    When we scan for type declarations.
    Then each required type must be declared with `type <Name> { ... }`.
    """
    assert _type_declared(schema_text, type_name), (
        f"Type '{type_name}' not declared in schema/partgraph.dql. "
        f"Expected: type {type_name} {{ ... }}"
    )


# ---------------------------------------------------------------------------
# AC-RL-1 — Datasheet.fail_count (issue #11, PR 1: refresh-links freshness
# foundation). Consecutive-HTTP-check-failure counter used by
# `partgraph refresh-links` to decide when a datasheet link is auto-purged
# (0 == healthy). This is the ONLY schema change PR 1 makes — no new index on
# verified_at is required (a dispatcher-verified read-only probe against
# Dgraph v25.3.4 showed `lt(verified_at, T)` inside an `@filter` with
# `func: type(Datasheet)` as root already works without one).
# ---------------------------------------------------------------------------

class TestDatasheetFailCount:
    def test_fail_count_declared_as_int(self, schema_text: str) -> None:
        """Given partgraph.dql defines the fail_count predicate.
        When we scan for its declaration.
        Then it must be declared with type int.
        """
        assert _has_predicate_with_attrs(schema_text, "fail_count", "int"), (
            "Predicate 'fail_count' must be declared as int in "
            f"{SCHEMA_REL} (consecutive datasheet-link-check failures; "
            "issue #11 PR 1)."
        )

    def test_datasheet_type_includes_fail_count(self, schema_text: str) -> None:
        """Given the Datasheet type declaration.
        When we inspect its field list.
        Then it must include 'fail_count'.
        """
        block = _type_block_text(schema_text, "Datasheet")
        assert block, f"type Datasheet {{ ... }} not found in {SCHEMA_REL}."
        assert re.search(r"\bfail_count\b", block), (
            f"type Datasheet must include 'fail_count'. Found block:\n{block}"
        )

    @pytest.mark.parametrize(
        "existing_field",
        ["url", "source", "verified_at", "http_status", "source_refs"],
    )
    def test_datasheet_type_still_includes_existing_fields(
        self, schema_text: str, existing_field: str
    ) -> None:
        """Given the Datasheet type declaration is extended with fail_count.
        When we inspect its field list.
        Then every pre-existing field (url/source/verified_at/http_status/
        source_refs) must still be present — adding fail_count must not
        displace any existing field.
        """
        block = _type_block_text(schema_text, "Datasheet")
        assert block, f"type Datasheet {{ ... }} not found in {SCHEMA_REL}."
        assert re.search(rf"\b{re.escape(existing_field)}\b", block), (
            f"type Datasheet must still include {existing_field!r} after "
            f"adding fail_count. Found block:\n{block}"
        )


# ---------------------------------------------------------------------------
# AC-RE-10 — Part.embed_text_hash (issue #11, PR 4: incremental re-embedding;
# ADR-0015). Content hash (sha256 hex digest) of the text that produced a
# Part's embedding, stamped alongside `embedding` by `embed_write` and
# compared CLIENT-SIDE (in Python) by `partgraph embed --changed` to detect a
# changed source text without a full re-embed. INDEX-FREE by design: the
# reconcile pass never filters or looks up by hash VALUE in DQL — it only
# reads the stored value (a plain has()/field read) and compares it to a
# freshly computed one in Python, mirroring the index-free precedent already
# set by stock_checked_at (issue #11 PR 2) and verified_at (issue #11 PR 1).
# ---------------------------------------------------------------------------

class TestEmbedTextHash:
    def test_embed_text_hash_declared_as_string(self, schema_text: str) -> None:
        """Given partgraph.dql defines the embed_text_hash predicate.
        When we scan for its declaration.
        Then it must be declared with type string.
        """
        assert _has_predicate_with_attrs(schema_text, "embed_text_hash", "string"), (
            "Predicate 'embed_text_hash' must be declared as string in "
            f"{SCHEMA_REL} (sha256 hex digest of the text that produced the "
            "embedding; issue #11 PR 4)."
        )

    def test_embed_text_hash_is_index_free_and_never_upsert(self, schema_text: str) -> None:
        """Given partgraph.dql defines the embed_text_hash predicate.
        When we scan for its declaration.
        Then it must carry NEITHER an @index directive NOR @upsert — the
        reconcile pass compares hashes client-side (in Python), never via a
        Dgraph index or an upsert-conflict lookup, mirroring the index-free
        precedent set by stock_checked_at / verified_at (Gate 3 SHOULD —
        security: ties the client-side-comparison invariant to the schema
        itself, not only to query text).
        """
        lines = schema_text.splitlines()
        window = ""
        for i, line in enumerate(lines):
            if line.lstrip().startswith("embed_text_hash:"):
                window = "\n".join(lines[i : i + 5])
                break
        assert window, f"Predicate 'embed_text_hash' not found in {SCHEMA_REL}."
        assert "@index" not in window, (
            "Predicate 'embed_text_hash' must be INDEX-FREE (mirrors "
            "stock_checked_at/verified_at — comparison is client-side, never "
            f"a Dgraph index). Found:\n{window}"
        )
        assert "@upsert" not in window, (
            "Predicate 'embed_text_hash' must NEVER carry @upsert — it is "
            "compared client-side, never used as an upsert conflict key. "
            f"Found:\n{window}"
        )

    def test_part_type_includes_embed_text_hash(self, schema_text: str) -> None:
        """Given the Part type declaration.
        When we inspect its field list.
        Then it must include 'embed_text_hash'.
        """
        block = _type_block_text(schema_text, "Part")
        assert block, f"type Part {{ ... }} not found in {SCHEMA_REL}."
        assert re.search(r"\bembed_text_hash\b", block), (
            f"type Part must include 'embed_text_hash'. Found block:\n{block}"
        )

    @pytest.mark.parametrize(
        "existing_field",
        ["xid", "embedding", "stock_checked_at", "description", "source_refs"],
    )
    def test_part_type_still_includes_existing_fields(
        self, schema_text: str, existing_field: str
    ) -> None:
        """Given the Part type declaration is extended with embed_text_hash.
        When we inspect its field list.
        Then every pre-existing field (xid/embedding/stock_checked_at/
        description/source_refs) must still be present — adding
        embed_text_hash must not displace any existing field.
        """
        block = _type_block_text(schema_text, "Part")
        assert block, f"type Part {{ ... }} not found in {SCHEMA_REL}."
        assert re.search(rf"\b{re.escape(existing_field)}\b", block), (
            f"type Part must still include {existing_field!r} after adding "
            f"embed_text_hash. Found block:\n{block}"
        )
