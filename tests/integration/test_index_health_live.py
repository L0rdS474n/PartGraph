"""
tests/integration/test_index_health_live.py (OPTIONAL, minimal)

Read-only live check for `partgraph.util.index_health.check_index_integrity()`
against a REAL, running Dgraph instance (ADR-0019, index-integrity PR C).

The hermetic tests/unit/test_index_health.py suite injects every HTTP
response, so it can never catch a REAL drift between schema/partgraph.dql and
a REAL live Dgraph schema, nor exercise the leaf's real (lazily-imported)
`requests` wiring end-to-end. This file exists to close exactly that gap, kept
deliberately minimal per its "OPTIONAL" scope.

`check_index_integrity()` is READ-ONLY by contract — schema introspection,
`has(embedding)`, and `similar_to` are all DQL reads; the leaf has no mutation
path at all — so this file needs no write step and no teardown.

Marked `@pytest.mark.integration`; skipped via the `dgraph_available` fixture
when no live Dgraph is reachable (deselect with `-m "not integration"`, the
same convention every other file in this directory uses).

Import discipline: `partgraph.util.index_health` is imported LOCALLY inside
the test function (via `pytest.importorskip`), NOT at module level. Gate 4
has not created that leaf yet, and pytest imports every collected module's
top-level code REGARDLESS of markers — a module-level import here would raise
ModuleNotFoundError at COLLECTION time and abort the ENTIRE pytest session
(pytest refuses to run any test at all once a collection error occurs),
defeating `-m "not integration"` for every OTHER, already-green test file in
the suite. Deferring the import to test-body time means this file collects
cleanly right now; the test itself simply does not exist to run yet (it is
`integration`-marked and deselected by every `-m "not integration"`
invocation in the meantime).
"""

from __future__ import annotations

import pytest

from partgraph.cli import SCHEMA_FILE
from partgraph.schema import load_schema


@pytest.mark.integration
def test_check_index_integrity_live_reachable_and_reports_a_clean_message(
    dgraph_available,
) -> None:
    """Given a real, running Dgraph instance and the real on-disk schema file.
    When check_index_integrity() is called with ZERO overrides (real
    `requests`, real DGRAPH_QUERY_URL, real INDEX_PROBE_TIMEOUT_S) against the
    live DB.
    Then it returns reachable=True and a non-empty, single-line, path-free
    message — proving the leaf's REAL HTTP wiring (not just its hermetically
    faked unit contract) actually round-trips against a live Dgraph `/query`
    endpoint.

    This test intentionally does NOT assert schema_ok/self_similarity_ok:
    whether THIS environment's schema has drifted, or whether any part has
    been embedded yet, is state this repo's CI/dev DB legitimately varies —
    asserting either here would make the suite flaky against real data. Use
    `partgraph db check-index` directly for a human-readable verdict; this
    test only pins that the read-only wiring itself works end-to-end.
    """
    from partgraph.util.index_health import check_index_integrity  # noqa: PLC0415

    schema_text = load_schema(SCHEMA_FILE)

    result = check_index_integrity(schema_text=schema_text)

    assert result.reachable is True, (
        "check_index_integrity() reported unreachable against a live Dgraph "
        f"that dgraph_available already confirmed healthy. "
        f"message={result.message!r}"
    )
    assert isinstance(result.message, str) and result.message
    assert "\n" not in result.message
    assert "/" not in result.message
    print(f"\n[LIVE] check_index_integrity(): {result.message}")
