"""
Tests: PR-A (fix/db-down-all-instances) — partgraph.util.lifecycle ARCHITECTURE
regressions (Gate 3a/3b amendment at fa5c4ee, findings AC-P1, 3b-H1, 3b-L2).

Split out from tests/unit/test_lifecycle.py on PURPOSE: that file's own
top-level ``from partgraph.util.lifecycle import (...)`` already hard-fails
COLLECTION with ModuleNotFoundError until the module exists (the correct
test-first red state for its behavioural contract) — which means any
test-level "skip if the module is absent" logic placed in THAT file would be
unreachable dead code; the whole file errors before any individual test gets
a chance to run its own skip branch.

THIS file imports NOTHING from ``partgraph.util.lifecycle`` at module level
(only stdlib + pytest), so it ALWAYS collects and its tests ALWAYS run —
skipping cleanly, individually, while the module is absent, and going green
the moment the module lands correctly. Two of its three checks
(the docker/podman-literal scan and the forbidden-import scan) read the
SOURCE TEXT directly off disk via pathlib — they need no import at all. The
third (the re-export check) uses ``pytest.importorskip`` so it, too, skips
individually rather than failing collection.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "src" / "partgraph" / "util" / "lifecycle.py"
)


def _read_lifecycle_source_or_skip() -> str:
    if not _MODULE_PATH.exists():
        pytest.skip("src/partgraph/util/lifecycle.py does not exist yet (expected pre-PR-A).")
    return _MODULE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-P1 — no hard-coded engine literal in the leaf's own source (relocated
# from test_lifecycle.py so it is independently collectible/skippable).
# ---------------------------------------------------------------------------


def test_ac_p1_lifecycle_source_never_hardcodes_docker_or_podman_argv_literal() -> None:
    """AC-P1: Given every engine invocation must begin with compose_command()
    or engine_command() output — never a literal.
    When src/partgraph/util/lifecycle.py's source text is scanned.
    Then it contains no argv-shaped list literal opening with "docker" or
    "podman" (e.g. `["docker"` / `['podman'`), which would bypass detection.

    Skips cleanly (not error) if the source file does not exist yet, and goes
    green the moment the module lands correctly.
    """
    text = _read_lifecycle_source_or_skip()

    forbidden = re.compile(r"""\[\s*["'](docker|podman)["']""")
    match = forbidden.search(text)
    assert match is None, (
        f"lifecycle.py hard-codes an engine literal in argv position: {match.group(0)!r}. "
        "Every engine invocation must begin with compose_command()/engine_command()."
    )


# ---------------------------------------------------------------------------
# [3b-H1] The leaf-discipline rule ("never import partgraph.cli or any
# embed/query/load module") must be MECHANICALLY enforced, not prose-only.
# ---------------------------------------------------------------------------


def test_lifecycle_source_never_imports_partgraph_cli_embed_query_or_load() -> None:
    """[3b-H1] Given partgraph.util.lifecycle is documented as a LEAF module
    that must never import partgraph.cli or any embed/query/load module (so
    both the CLI and future PR-B/PR-C callers can import it without a cycle
    — mirrors container.py/health.py/index_health.py's own leaf discipline).
    When src/partgraph/util/lifecycle.py's source text is scanned.
    Then it contains none of: `import partgraph.cli`, `from partgraph.cli`,
    `from partgraph.embed`, `from partgraph.query`, `from partgraph.load`
    (module-level OR lazy/deferred — the grammar-level scan catches both,
    since a lazy `import partgraph.cli` inside a function body is textually
    identical to an eager one).

    Skips cleanly (not error) if the source file does not exist yet, and goes
    green the moment the module lands correctly.
    """
    text = _read_lifecycle_source_or_skip()

    forbidden_patterns = [
        r"^\s*import\s+partgraph\.cli\b",
        r"^\s*from\s+partgraph\.cli\b",
        r"^\s*from\s+partgraph\.embed\b",
        r"^\s*from\s+partgraph\.query\b",
        r"^\s*from\s+partgraph\.load\b",
    ]
    violations = []
    for pattern in forbidden_patterns:
        for match in re.finditer(pattern, text, flags=re.MULTILINE):
            violations.append(match.group(0).strip())

    assert not violations, (
        "partgraph.util.lifecycle must never import partgraph.cli or any "
        f"embed/query/load module (leaf discipline). Found: {violations!r}"
    )


# ---------------------------------------------------------------------------
# [3b-L2] partgraph.util must NOT re-export lifecycle's functions (mirrors
# the existing but currently UNENFORCED convention that health/index_health
# are not re-exported from src/partgraph/util/__init__.py, ADR-0018 Sec 4).
# ---------------------------------------------------------------------------


def test_partgraph_util_package_does_not_reexport_lifecycle_functions() -> None:
    """[3b-L2, amended: the forbidden-name set is now DERIVED, not hardcoded]
    Given ADR-0018 Section 4's precedent: neither `health` nor `index_health`
    is re-exported from `partgraph/util/__init__.py`, even though
    `container`/`resources` both are.
    When `partgraph.util` (the package's own `__init__.py`) is inspected.
    Then it does NOT expose ANY non-DTO name from
    `partgraph.util.lifecycle.__all__` (every function and every constant —
    `stop_all`, `find_partgraph_instances`, `unit_state`, and whatever else
    the leaf adds to `__all__` in the future, e.g. `volume_exists`) as a
    top-level attribute — a caller must always reach them via
    `partgraph.util.lifecycle` explicitly, never via the package shortcut.

    The forbidden-name set is derived from `lifecycle.__all__` at RUN TIME,
    filtering out the DTOs (`Instance`/`UnitState`/`DownResult`) by
    introspecting each exported object with `inspect.isclass` — a DTO's own
    naming convention (PascalCase, a frozen dataclass) is identified
    mechanically, not by a hardcoded exclusion list either. This means the
    two sibling checks in this file (the docker/podman-literal scan, the
    forbidden-import scan) and this one now share the SAME property: none of
    the three needs a manual entry updated when the leaf's public surface
    grows. Previously this test hardcoded the tuple
    `("stop_all", "find_partgraph_instances", "unit_state")`, so a NEW
    function or constant added to `__all__` (e.g. `volume_exists`, PR-B1)
    could be re-exported from `partgraph/util/__init__.py` in violation of
    the ADR-0018 Section 4 precedent while this test stayed green.

    Uses `pytest.importorskip` (not a top-level import) so THIS test skips
    individually and cleanly while partgraph.util.lifecycle does not yet
    exist, rather than erroring the whole file's collection.
    """
    lifecycle = pytest.importorskip(
        "partgraph.util.lifecycle",
        reason="partgraph.util.lifecycle does not exist yet (expected pre-PR-A).",
    )
    import partgraph.util as util_package  # noqa: PLC0415

    forbidden_names = [
        name for name in lifecycle.__all__
        if not inspect.isclass(getattr(lifecycle, name))
    ]
    assert forbidden_names, (
        "sanity check: expected at least one non-DTO name in "
        "partgraph.util.lifecycle.__all__ to check — an empty list here would "
        "make the loop below vacuously pass without testing anything."
    )

    for forbidden_name in forbidden_names:
        assert not hasattr(util_package, forbidden_name), (
            f"partgraph.util must NOT re-export {forbidden_name!r} — it must "
            "only be reachable via partgraph.util.lifecycle directly "
            "(mirrors the health/index_health precedent, ADR-0018 Sec 4)."
        )
