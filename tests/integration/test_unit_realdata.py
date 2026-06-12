"""
Tests: Real-data parse() success rate

@pytest.mark.integration — requires data/raw/jlcpcb-components.sqlite3 to be
present. Tests are SKIPPED with a clear reason if the file is absent.

Purpose: prove that the unit parser handles >= 90% of real attribute strings
that contain a recognizable unit token (pre-filtered). This complements the
deterministic fixture table in tests/unit/test_units.py.

Sampling:
  - seeded random.Random(1234) for reproducibility.
  - Sample >= 1000 attribute strings from the real SQLite that contain at
    least one unit-like token (digit followed by a unit abbreviation).
  - Compute and PRINT the parse success rate.
  - Assert rate >= 0.90.

The pre-filter criterion (documented here per ADR/B1 ruling):
  A string is included in the sample if it matches the regex:
    r'\\d+\\s*[pnuµmkKMGT]?[VΩFHzAW%]'
  i.e. it contains a digit followed (optionally with a prefix) by a recognized
  unit character. Strings that do NOT match are excluded from the success-rate
  denominator (they are legitimately non-parseable).
"""

from __future__ import annotations

import pathlib
import random
import re
import sqlite3
import sys

import pytest

# Unit filter regex: digit + optional SI prefix + unit abbreviation.
# This is the pre-filter; only strings matching it enter the sample.
_UNIT_TOKEN_RE = re.compile(r'\d+\s*[pnuµmkKMGT]?[VΩFHzAW%]')

_SAMPLE_SIZE = 1000
_SUCCESS_RATE_THRESHOLD = 0.90
_RNG_SEED = 1234

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SQLITE_PATH = REPO_ROOT / "data" / "raw" / "jlcpcb-components.sqlite3"


_DIMENSION_KEYWORDS: list[tuple[list[str], str]] = [
    (["voltage", "volt"],                              "voltage"),
    (["current", "ampere"],                            "current"),
    (["resistance", "resistor", "ohm"],                "resistance"),
    (["capacitance", "capacitor"],                     "capacitance"),
    (["inductance", "inductor"],                       "inductance"),
    (["frequency", "freq"],                            "frequency"),
    (["power", "watt"],                                "power"),
    (["time", "delay"],                                "time"),
    (["tolerance"],                                    "tolerance"),
]


def _extract_from_structured_attr(
    attr_name: str, attr_val: dict
) -> list[tuple[str, str]]:
    """Extract unit-bearing strings from a structured JLC attribute dict."""
    results: list[tuple[str, str]] = []
    primary = str(attr_val.get("primary", ""))
    if _UNIT_TOKEN_RE.search(primary):
        results.append((primary, _guess_dimension(attr_name)))
    for v in (attr_val.get("values") or {}).values():
        if isinstance(v, list) and len(v) >= 2:
            combined = str(v[0]) + str(v[1])
            if _UNIT_TOKEN_RE.search(combined):
                results.append((combined, _guess_dimension(attr_name)))
    return results


def _extract_attrs_from_extra(extra_json: str) -> list[tuple[str, str]]:
    """Parse one extra JSON string and return unit-bearing (value, dimension) pairs."""
    import json as _json  # noqa: PLC0415 — local import keeps module-level clean
    try:
        extra = _json.loads(extra_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(extra, dict):
        return []
    attrs = extra.get("attributes", {})
    if not isinstance(attrs, dict):
        return []
    results: list[tuple[str, str]] = []
    for attr_name, attr_val in attrs.items():
        if isinstance(attr_val, dict):
            results.extend(_extract_from_structured_attr(attr_name, attr_val))
        elif isinstance(attr_val, str) and _UNIT_TOKEN_RE.search(attr_val):
            results.append((attr_val, _guess_dimension(attr_name)))
    return results


def _collect_unit_strings(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return (attr_string, dimension_hint) pairs that match the unit pre-filter.

    Inspects the 'extra' column of the components table (JSON). Returns up to
    20000 candidates so the seeded random.sample has a large enough pool.
    """
    cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='components'")
    if cur.fetchone() is None:
        return []
    try:
        rows = conn.execute(
            "SELECT extra FROM components WHERE extra IS NOT NULL AND extra != '{}' LIMIT 20000"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    candidates: list[tuple[str, str]] = []
    for (extra_json,) in rows:
        if extra_json:
            candidates.extend(_extract_attrs_from_extra(extra_json))
    return candidates


def _guess_dimension(attr_name: str) -> str:
    """Map an attribute name to a dimension hint for parse()."""
    name_lower = attr_name.lower()
    for keywords, dimension in _DIMENSION_KEYWORDS:
        if any(kw in name_lower for kw in keywords):
            return dimension
    # Generic fallback — the parser must handle unknown dimensions gracefully.
    return "generic"


@pytest.mark.integration
def test_unit_realdata_parse_success_rate() -> None:
    """Given data/raw/jlcpcb-components.sqlite3 is present.
    When >= 1000 attribute strings that match the unit pre-filter are sampled
    using random.Random(1234).
    Then parse() succeeds (returns a float, not None) for >= 90% of them.

    The pre-filter (documented in module docstring) ensures that only strings
    containing a recognizable unit token enter the sample, making a 90% success
    rate a meaningful threshold.

    The success rate and sample size are PRINTED so they appear in --verbose output.
    """
    if not SQLITE_PATH.exists():
        pytest.skip(
            f"Real data file absent: {SQLITE_PATH}. "
            "Run `partgraph ingest jlcparts --fetch` to download it, "
            "then re-run integration tests."
        )

    from partgraph.normalize.units import parse

    conn = sqlite3.connect(str(SQLITE_PATH))
    try:
        candidates = _collect_unit_strings(conn)
    finally:
        conn.close()

    if len(candidates) < _SAMPLE_SIZE:
        pytest.skip(
            f"Not enough unit-bearing attribute strings in the DB "
            f"(found {len(candidates)}, need {_SAMPLE_SIZE}). "
            "The DB may be too small or attribute extraction failed."
        )

    rng = random.Random(_RNG_SEED)
    sample = rng.sample(candidates, _SAMPLE_SIZE)

    successes = 0
    failures: list[tuple[str, str]] = []

    for value_str, dimension in sample:
        try:
            result = parse(value_str, dimension)
        except Exception:  # noqa: BLE001
            result = None

        if result is not None:
            successes += 1
        else:
            failures.append((value_str, dimension))

    success_rate = successes / _SAMPLE_SIZE
    print(
        f"\n[T-UNIT-realdata] Sample size: {_SAMPLE_SIZE}, "
        f"Successes: {successes}, "
        f"Rate: {success_rate:.1%} "
        f"(threshold: {_SUCCESS_RATE_THRESHOLD:.0%})",
        file=sys.stderr,
    )
    if failures[:10]:
        print(
            f"[T-UNIT-realdata] First 10 failures: {failures[:10]}",
            file=sys.stderr,
        )

    assert success_rate >= _SUCCESS_RATE_THRESHOLD, (
        f"parse() success rate {success_rate:.1%} is below threshold "
        f"{_SUCCESS_RATE_THRESHOLD:.0%} on real JLCPCB attribute data "
        f"(sample size: {_SAMPLE_SIZE}, seed: {_RNG_SEED}).\n"
        f"First 10 failures: {failures[:10]}"
    )
