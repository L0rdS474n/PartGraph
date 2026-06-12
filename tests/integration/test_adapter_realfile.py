"""
Tests: T-ADAPT-REALFILE-*

Integration test against the real CDFER file
  data/raw/jlcpcb-components.sqlite3  (1.6 GB, 616 593 rows)

All tests are @pytest.mark.integration and are automatically skipped when the
file is absent so the suite stays green in CI environments that have not
downloaded the data file.

Tests:
- T-ADAPT-REALFILE-strategy-b:  introspection selects Strategy B on the real file.
- T-ADAPT-REALFILE-first5-parts: first 5 parts each have:
    * non-empty mpn
    * xid matching ^[A-Z0-9]+|[A-Z0-9]+$
    * lcsc_id matching ^C[0-9]+$
"""

from __future__ import annotations

import pathlib
import re

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_REAL_DB = _REPO_ROOT / "data" / "raw" / "jlcpcb-components.sqlite3"

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Skip condition
# ---------------------------------------------------------------------------

_SKIP_REAL = pytest.mark.skipif(
    not _REAL_DB.exists(),
    reason=(
        f"Real CDFER file not present at {_REAL_DB}. "
        "Run `partgraph ingest jlcparts --fetch` to download it (~1.6 GB). "
        "Skipping real-file integration tests."
    ),
)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_XID_RE = re.compile(r"^[A-Z0-9]+\|[A-Z0-9]+$")
_LCSC_ID_RE = re.compile(r"^C\d+$")


# ---------------------------------------------------------------------------
# T-ADAPT-REALFILE-strategy-b
# ---------------------------------------------------------------------------

@_SKIP_REAL
def test_adapter_realfile_strategy_b_selected() -> None:
    """Given the real downloaded CDFER file opened via open_jlcparts_db().
    When JlcpartsAdapter is constructed.
    Then no exception is raised AND the adapter's internal _strategy attribute
    equals 'B' (confirming real-file schema is the FK-joined shape).
    """
    from partgraph.sources.jlcparts import JlcpartsAdapter, open_jlcparts_db

    conn = open_jlcparts_db(_REAL_DB)
    try:
        adapter = JlcpartsAdapter(conn)
        assert adapter._strategy == "B", (  # noqa: SLF001
            f"Real CDFER file must select Strategy B (FK-joined); "
            f"got strategy={adapter._strategy!r}"  # noqa: SLF001
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T-ADAPT-REALFILE-first5-parts
# ---------------------------------------------------------------------------

@_SKIP_REAL
def test_adapter_realfile_first5_parts_valid() -> None:
    """Given the real downloaded CDFER file.
    When iter_parts() is called and the first 5 results are consumed.
    Then each StagedPart satisfies:
      - mpn is non-empty (truthy string)
      - xid matches ^[A-Z0-9]+|[A-Z0-9]+$
      - lcsc_id matches ^C[0-9]+$ (e.g. "C1002" for lcsc INTEGER 1002)

    This validates that the adapter correctly:
      - uses mfr column (or extra.mpn fallback) as the MPN source
      - renders lcsc INTEGER as "C{lcsc}" string
      - computes xid from normalized mpn/manufacturer
    """
    from partgraph.sources.jlcparts import JlcpartsAdapter, open_jlcparts_db

    conn = open_jlcparts_db(_REAL_DB)
    try:
        adapter = JlcpartsAdapter(conn)
        parts = []
        for part in adapter.iter_parts():
            parts.append(part)
            if len(parts) >= 5:
                break
    finally:
        conn.close()

    assert len(parts) >= 1, (
        "Real file yielded zero parts. Expected at least 5 from 616 593 rows."
    )

    for i, part in enumerate(parts):
        assert part.mpn, (
            f"Part #{i}: mpn must be non-empty. Got mpn={part.mpn!r}, "
            f"lcsc_id={part.lcsc_id!r}"
        )
        assert part.xid and _XID_RE.match(part.xid), (
            f"Part #{i} (mpn={part.mpn!r}): xid {part.xid!r} does not match "
            f"^[A-Z0-9]+|[A-Z0-9]+$"
        )
        assert part.lcsc_id and _LCSC_ID_RE.match(part.lcsc_id), (
            f"Part #{i} (mpn={part.mpn!r}): lcsc_id {part.lcsc_id!r} does not "
            f"match ^C[0-9]+$ (expected 'C' + integer, e.g. 'C1002')"
        )
