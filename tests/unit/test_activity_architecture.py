"""
Tests: PR-C (feat/db-idle-autostop) — `partgraph.util.activity` ARCHITECTURE
(mechanical leaf-discipline guards), mirroring
`tests/unit/test_lifecycle_architecture.py`'s own established convention for
`partgraph.util.lifecycle` — extended in the OTHER direction too, since this
PR's whole premise is that the two leaves stay mutually ignorant of each
other ("`lifecycle.py` knows nothing about activity. `activity.py` knows
nothing about containers.").

Split out from `tests/unit/test_activity.py` for the SAME reason
`test_lifecycle_architecture.py` was split from `test_lifecycle.py`: that
file's own top-level `from partgraph.util.activity import (...)` already
hard-fails COLLECTION with `ModuleNotFoundError` until the module exists —
which would make any test-level "skip if absent" branch placed in THAT file
unreachable dead code. THIS file imports nothing from
`partgraph.util.activity` at module level (only stdlib + pytest, plus a
READ of `partgraph.util.lifecycle`'s own already-existing source for the
reverse-direction check), so it always collects and every test skips
cleanly, individually, while `activity.py` is absent, and goes green the
moment it lands with the right shape.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_ACTIVITY_PATH = _REPO_ROOT / "src" / "partgraph" / "util" / "activity.py"
_LIFECYCLE_PATH = _REPO_ROOT / "src" / "partgraph" / "util" / "lifecycle.py"


def _read_activity_source_or_skip() -> str:
    if not _ACTIVITY_PATH.exists():
        pytest.skip("src/partgraph/util/activity.py does not exist yet (expected pre-PR-C).")
    return _ACTIVITY_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# activity.py must never import partgraph.cli, or any container/embed/query/
# load module — it "knows nothing about containers".
# ---------------------------------------------------------------------------


def test_activity_source_never_imports_partgraph_cli_embed_query_load_or_container_or_lifecycle() -> None:
    """Given `partgraph.util.activity` is documented as a LEAF module that
    knows nothing about containers, Compose, or the container engine, and
    must never import `partgraph.cli` or any embed/query/load module —
    mirrors `partgraph.util.lifecycle`'s own leaf discipline, EXTENDED here
    to also forbid `partgraph.util.lifecycle`/`partgraph.util.container`
    themselves (the "activity.py knows nothing about containers" half of the
    mutual-ignorance contract).
    When `src/partgraph/util/activity.py`'s source text is scanned.
    Then it contains none of the forbidden import forms below (module-level
    OR lazy/deferred — the grammar-level scan catches both).
    Skips cleanly (not error) if the source file does not exist yet.
    """
    text = _read_activity_source_or_skip()

    forbidden_patterns = [
        r"^\s*import\s+partgraph\.cli\b",
        r"^\s*from\s+partgraph\.cli\b",
        r"^\s*from\s+partgraph\.embed\b",
        r"^\s*from\s+partgraph\.query\b",
        r"^\s*from\s+partgraph\.load\b",
        r"^\s*import\s+partgraph\.util\.lifecycle\b",
        r"^\s*from\s+partgraph\.util\.lifecycle\b",
        r"^\s*import\s+partgraph\.util\.container\b",
        r"^\s*from\s+partgraph\.util\.container\b",
    ]
    violations = []
    for pattern in forbidden_patterns:
        for match in re.finditer(pattern, text, flags=re.MULTILINE):
            violations.append(match.group(0).strip())

    assert not violations, (
        "partgraph.util.activity must never import partgraph.cli, any "
        "embed/query/load module, or partgraph.util.lifecycle/container "
        f"(leaf discipline — 'knows nothing about containers'). Found: {violations!r}"
    )


def test_lifecycle_source_never_mentions_activity_the_reverse_direction_regression_lock() -> None:
    """Given the OTHER half of the mutual-ignorance contract: "lifecycle.py
    knows nothing about activity" — and `partgraph.util.lifecycle` already
    exists today (PR-A), so this half can be checked NOW, as a genuine
    regression lock, not merely a post-landing aspiration.
    When `src/partgraph/util/lifecycle.py`'s real, current source is scanned.
    Then it does not import `partgraph.util.activity` in any form — proving
    this file catches a FUTURE violation (a later PR wiring activity into
    lifecycle would fail this test), not merely documenting an absence that
    happens to be true today because the module does not exist yet.
    """
    assert _LIFECYCLE_PATH.exists(), (
        "src/partgraph/util/lifecycle.py is expected to already exist (PR-A landed)."
    )
    text = _LIFECYCLE_PATH.read_text(encoding="utf-8")
    forbidden_patterns = [
        r"^\s*import\s+partgraph\.util\.activity\b",
        r"^\s*from\s+partgraph\.util\.activity\b",
        r"^\s*from\s+partgraph\.util\s+import\s+activity\b",
    ]
    violations = [
        match.group(0).strip()
        for pattern in forbidden_patterns
        for match in re.finditer(pattern, text, flags=re.MULTILINE)
    ]
    assert not violations, (
        "partgraph.util.lifecycle must never import partgraph.util.activity "
        f"(the two leaves must stay mutually ignorant). Found: {violations!r}"
    )


# ---------------------------------------------------------------------------
# psutil must be imported LAZILY, never at module level (mirrors
# src/partgraph/util/resources.py's own documented ARCH-1 precedent: "import
# psutil MUST stay lazy... a module-level import here would make merely
# importing the CLI (or partgraph.util) require psutil and hard-fail when it
# is absent").
# ---------------------------------------------------------------------------


def test_psutil_is_never_imported_at_activity_module_top_level() -> None:
    """Given `partgraph/util/__init__.py` imports `partgraph.util.activity`'s
    sibling modules eagerly, and `cli.py` sits on that same import path
    (mirrors ARCH-1 in `src/partgraph/util/resources.py`).
    When `src/partgraph/util/activity.py`'s AST is inspected.
    Then no `import psutil` / `from psutil import ...` statement appears
    directly in the MODULE body (`tree.body`) — only inside a function
    (lazy, call-time import) is acceptable. Distinguishes real module-level
    placement from a merely-indented lazy import via the AST, not a regex
    that could be fooled by indentation alone.
    Skips cleanly (not error) if the source file does not exist yet.
    """
    text = _read_activity_source_or_skip()
    tree = ast.parse(text, filename=str(_ACTIVITY_PATH))

    def _mentions_psutil(node: ast.stmt) -> bool:
        if isinstance(node, ast.Import):
            return any(alias.name.split(".")[0] == "psutil" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            return node.module is not None and node.module.split(".")[0] == "psutil"
        return False

    top_level_violations = [
        ast.dump(node) for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _mentions_psutil(node)
    ]
    assert not top_level_violations, (
        "psutil must be imported LAZILY inside a function body, never at "
        f"module level (mirrors resources.py's ARCH-1): {top_level_violations!r}"
    )


# ---------------------------------------------------------------------------
# partgraph.util must NOT re-export activity's functions (mirrors the
# health/index_health/lifecycle precedent, ADR-0018 Sec 4).
# ---------------------------------------------------------------------------


def test_partgraph_util_package_does_not_reexport_activity_functions() -> None:
    """Given ADR-0018 Section 4's precedent, already extended to `lifecycle`
    ([3b-L2] in `test_lifecycle_architecture.py`): neither `health`,
    `index_health`, nor `lifecycle` is re-exported from
    `partgraph/util/__init__.py`.
    When `partgraph.util` (the package's own `__init__.py`) is inspected.
    Then it does NOT expose any non-DTO name from
    `partgraph.util.activity.__all__` as a top-level attribute. The
    forbidden-name set is DERIVED from `activity.__all__` at RUN TIME
    (filtering DTOs via `inspect.isclass`), mirroring the lifecycle test's
    own fix for the same "hardcoded tuple silently stops covering new
    names" failure mode.
    Uses `pytest.importorskip` so this test skips individually and cleanly
    while `partgraph.util.activity` does not yet exist.
    """
    activity = pytest.importorskip(
        "partgraph.util.activity",
        reason="partgraph.util.activity does not exist yet (expected pre-PR-C).",
    )
    import partgraph.util as util_package  # noqa: PLC0415

    forbidden_names = [
        name for name in activity.__all__
        if not inspect.isclass(getattr(activity, name))
    ]
    assert forbidden_names, (
        "sanity check: expected at least one non-DTO name in "
        "partgraph.util.activity.__all__ to check."
    )
    for forbidden_name in forbidden_names:
        assert not hasattr(util_package, forbidden_name), (
            f"partgraph.util must NOT re-export {forbidden_name!r} — it must "
            "only be reachable via partgraph.util.activity directly."
        )
