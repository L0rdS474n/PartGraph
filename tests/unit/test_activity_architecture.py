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
_TEST_ACTIVITY_PATH = _REPO_ROOT / "tests" / "unit" / "test_activity.py"


def _read_activity_source_or_skip() -> str:
    if not _ACTIVITY_PATH.exists():
        pytest.skip("src/partgraph/util/activity.py does not exist yet (expected pre-PR-C).")
    return _ACTIVITY_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# [Gate 5 finding — mutation-tested and confirmed] The import scanner, AST
# based rather than a hand-enumerated regex list, catching THREE distinct
# Python import shapes for each forbidden dotted module path:
#   1. `import a.b.c`               (ast.Import)
#   2. `from a.b.c import name`     (ast.ImportFrom, the "from" clause IS
#                                     itself the forbidden module)
#   3. `from a.b import c`          (ast.ImportFrom, the "from" clause is
#                                     the PARENT package and the forbidden
#                                     module is one of the imported NAMES)
# The regex list this scanner replaces enumerated only shapes 1 and 2 for
# five of its six forbidden targets. Gate 5's mutation run proved shape 3
# passes it silently: injecting `from partgraph.util import lifecycle` (and
# `... import container`) into the REAL `src/partgraph/util/activity.py`
# left the suite at 1507 passed, uncaught, while `import
# partgraph.util.lifecycle` and `from partgraph.util.lifecycle import
# stop_all` were both correctly caught — reproduced independently here (see
# the self-tests below) before trusting the fix, exactly as
# `test_repo_never_executes_lifecycle_mutations.py`'s own "Gate 3a BLOCKING
# 1" discipline demands: a scanner is proven to have real teeth on embedded
# canary source BEFORE it is trusted against the real tree.
#
# A single AST-based function replaces the 18-entry regex list (6 forbidden
# modules x 3 shapes) that the fix would otherwise require, both because
# hand-enumerating that many near-identical patterns is exactly the kind of
# mistake that created this gap in the first place, and because the THREE
# shapes above are the complete, closed set of ways CPython lets a name be
# imported (no fourth shape exists to omit next time).
# ---------------------------------------------------------------------------


def _find_forbidden_imports(
    source: str, label: str, forbidden_modules: frozenset[str]
) -> list[str]:
    """Return one message per forbidden import found in *source*, covering
    all three shapes described above uniformly. `node.module is None` (a
    relative `from . import x`) is skipped: this repo's own style is
    absolute imports throughout, exactly as
    `test_repo_never_executes_lifecycle_mutations.py`'s own scanner
    discloses as its own scope boundary — not a silent gap, a stated one.
    """
    tree = ast.parse(source, filename=label)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    violations.append(f"{label}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                if node.module in forbidden_modules:
                    violations.append(
                        f"{label}:{node.lineno}: from {node.module} import {alias.name}"
                    )
                    continue
                dotted = f"{node.module}.{alias.name}"
                if dotted in forbidden_modules:
                    violations.append(
                        f"{label}:{node.lineno}: from {node.module} import {alias.name}"
                    )
    return list(dict.fromkeys(violations))


# ---------------------------------------------------------------------------
# Self-test: prove the scanner has real teeth for EACH shape, and does not
# false-positive on a benign import, BEFORE trusting it against the real
# tree (mirrors test_repo_never_executes_lifecycle_mutations.py's own
# positive/negative-control discipline).
# ---------------------------------------------------------------------------


def test_scanner_catches_shape_1_plain_dotted_import() -> None:
    """[Positive control, shape 1] `import a.b.c`."""
    violations = _find_forbidden_imports(
        "import partgraph.util.lifecycle\n", "canary.py",
        frozenset({"partgraph.util.lifecycle"}),
    )
    assert violations, "shape 1 (`import a.b.c`) must be caught"


def test_scanner_catches_shape_2_from_the_forbidden_module_import_a_name() -> None:
    """[Positive control, shape 2] `from a.b.c import name`."""
    violations = _find_forbidden_imports(
        "from partgraph.util.lifecycle import stop_all\n", "canary.py",
        frozenset({"partgraph.util.lifecycle"}),
    )
    assert violations, "shape 2 (`from a.b.c import name`) must be caught"


def test_scanner_catches_shape_3_from_the_parent_package_import_the_forbidden_module() -> None:
    """[Gate 5 finding — positive control, shape 3] `from a.b import c` —
    the shape the old regex list silently missed for five of its six
    forbidden targets."""
    violations = _find_forbidden_imports(
        "from partgraph.util import lifecycle\n", "canary.py",
        frozenset({"partgraph.util.lifecycle"}),
    )
    assert violations, "shape 3 (`from a.b import c`) must be caught"


def test_scanner_catches_shape_3_for_a_top_level_module_target_too() -> None:
    """[Positive control, shape 3, top-level target] `from partgraph import
    cli` — the SAME shape 3 hazard, one level shallower (parent is
    `partgraph` itself rather than `partgraph.util`)."""
    violations = _find_forbidden_imports(
        "from partgraph import cli\n", "canary.py", frozenset({"partgraph.cli"}),
    )
    assert violations, "shape 3 at the top-level package boundary must be caught"


def test_scanner_does_not_flag_an_unrelated_benign_import() -> None:
    """[Negative control] Given imports of things that are NOT forbidden
    (including a sibling leaf, `partgraph.util.health`, and a stdlib
    module).
    When the scanner runs.
    Then nothing is flagged — proves the scanner discriminates by the exact
    forbidden set, not merely "any import at all"."""
    src = "from partgraph.util import health\nimport os\nimport json\n"
    violations = _find_forbidden_imports(
        src, "canary.py", frozenset({"partgraph.util.lifecycle", "partgraph.util.container"}),
    )
    assert not violations, violations


# ---------------------------------------------------------------------------
# activity.py must never import partgraph.cli, or any container/embed/query/
# load module — it "knows nothing about containers".
# ---------------------------------------------------------------------------

_ACTIVITY_FORBIDDEN_MODULES = frozenset({
    "partgraph.cli",
    "partgraph.embed",
    "partgraph.query",
    "partgraph.load",
    "partgraph.util.lifecycle",
    "partgraph.util.container",
})


def test_activity_source_never_imports_partgraph_cli_embed_query_load_or_container_or_lifecycle() -> None:
    """Given `partgraph.util.activity` is documented as a LEAF module that
    knows nothing about containers, Compose, or the container engine, and
    must never import `partgraph.cli` or any embed/query/load module —
    mirrors `partgraph.util.lifecycle`'s own leaf discipline, EXTENDED here
    to also forbid `partgraph.util.lifecycle`/`partgraph.util.container`
    themselves (the "activity.py knows nothing about containers" half of the
    mutual-ignorance contract).
    When `src/partgraph/util/activity.py`'s source is AST-parsed and scanned
    for all three import shapes above, against every forbidden module.
    Then none is found. Skips cleanly (not error) if the source file does
    not exist yet.
    """
    text = _read_activity_source_or_skip()
    violations = _find_forbidden_imports(
        text, str(_ACTIVITY_PATH), _ACTIVITY_FORBIDDEN_MODULES
    )
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
    When `src/partgraph/util/lifecycle.py`'s real, current source is
    AST-scanned for all three import shapes against `partgraph.util.activity`.
    Then none is found — proving this file catches a FUTURE violation (a
    later PR wiring activity into lifecycle would fail this test), not
    merely documenting an absence that happens to be true today because the
    module does not exist yet.
    """
    assert _LIFECYCLE_PATH.exists(), (
        "src/partgraph/util/lifecycle.py is expected to already exist (PR-A landed)."
    )
    text = _LIFECYCLE_PATH.read_text(encoding="utf-8")
    violations = _find_forbidden_imports(
        text, str(_LIFECYCLE_PATH), frozenset({"partgraph.util.activity"})
    )
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


# ---------------------------------------------------------------------------
# `test_activity.py`'s own module docstring hand-enumerates every
# `REASON_*` tag in its "Pinned contract" section — a prose inventory a
# reader trusts as authoritative. It silently went one tag short: when
# `REASON_STAMP_UNRECORDABLE` landed, nothing forced that enumeration to be
# updated to match, because prose is not an assertion — the exact shape a
# separate finding in this body of work has already named for `cli.py`'s
# phantom convention claim, the false `subprocess.run` idiom claim, and a
# `--limit 0` scenario that never existed. This guard makes the inventory
# self-checking instead of relying on a human to notice next time: it
# extracts every backtick-quoted `REASON_...` token from that file's OWN
# raw source, between two stable, single-line anchor phrases bracketing the
# list, and diffs it against `partgraph.util.activity.__all__`'s ACTUAL
# `REASON_*` names — never a second hand-typed copy of either side.
#
# [Gate 5 finding 1] The FIRST version of these anchors bracketed the WHOLE
# "Module-level constants" paragraph ("Module-level constants:" through
# "State-file mechanics (C-1, C-3, C-14):"), not only the enumerated list —
# and that paragraph's own explanatory aside, right after the list, mentions
# `REASON_STAMP_UNRECORDABLE` BY NAME too (the sentence recording that this
# very guard exists). A tag removed from the LIST alone, with that aside
# left untouched, would still be "found" by the wide anchors — the guard
# would pass green on exactly the drift it exists to catch: a guard whose
# anchors are wider than the thing it guards, one commit after the guard
# that fixed the same shape elsewhere. The anchors below are now tightened
# to bracket ONLY the four-line list itself — starting immediately AFTER
# the last sentence of the PRECEDING paragraph and ending immediately
# BEFORE the aside begins — so the aside's own mention of the tag is
# provably outside the span. `test_reason_inventory_guard_is_tripped_by_a_
# tag_removed_from_the_list_alone` below demonstrates this against a real,
# targeted mutation rather than merely asserting the anchors look right.
# ---------------------------------------------------------------------------

_REASON_INVENTORY_START = "ever receives an already-parsed `idle_timeout_minutes: float`."
_REASON_INVENTORY_END = "— plain string tags"


def _extract_documented_reason_names(source: str) -> set[str]:
    """Extract every backtick-quoted `REASON_[A-Z_]+` token from *source*,
    strictly between the two module-level anchor phrases above (both
    excluded from the span itself). Shared by the guard test and its own
    controls below, so the controls exercise the EXACT SAME extraction
    logic the guard uses — never a second, independently-drifting copy."""
    start = source.index(_REASON_INVENTORY_START) + len(_REASON_INVENTORY_START)
    end = source.index(_REASON_INVENTORY_END, start)
    return set(re.findall(r"REASON_[A-Z_]+", source[start:end]))


def test_reason_tags_docstring_inventory_matches_the_modules_actual_reason_constants() -> None:
    """Given `tests/unit/test_activity.py`'s own module docstring hand-lists
    every `REASON_*` tag strictly between its two tight anchor phrases
    (immediately before the list and immediately after it — neither anchor
    itself, nor anything outside that span, is scanned).
    When every backtick-quoted `REASON_[A-Z_]+` token in that span is
    extracted via regex and compared against
    `partgraph.util.activity.__all__`'s ACTUAL `REASON_*` names, read live
    from the module rather than re-typed here.
    Then the two sets are IDENTICAL. A tag added to the module without
    updating that prose — exactly what happened when
    `REASON_STAMP_UNRECORDABLE` landed — now fails a real assertion instead
    of silently reading as an authoritative, quietly-wrong inventory; a tag
    removed or renamed is caught the same way. If either anchor phrase is
    ever reworded, extraction finds an empty or wrong span and this test
    fails loudly rather than passing on a false positive — the anchors are
    checked live against the real file, never assumed stable forever.
    Skips cleanly (not error) if `partgraph.util.activity` does not exist
    yet, mirroring every other test in this file.
    """
    activity = pytest.importorskip(
        "partgraph.util.activity",
        reason="partgraph.util.activity does not exist yet (expected pre-PR-C).",
    )
    actual_reason_names = {name for name in activity.__all__ if name.startswith("REASON_")}
    assert actual_reason_names, "sanity: activity.py must define at least one REASON_* tag"

    assert _TEST_ACTIVITY_PATH.exists(), f"expected {_TEST_ACTIVITY_PATH} to exist"
    source = _TEST_ACTIVITY_PATH.read_text(encoding="utf-8")
    documented_reason_names = _extract_documented_reason_names(source)

    assert documented_reason_names == actual_reason_names, (
        "tests/unit/test_activity.py's module-docstring 'Pinned contract' "
        "REASON_* inventory has drifted from partgraph.util.activity.__all__'s "
        f"actual set. documented={sorted(documented_reason_names)!r} "
        f"actual={sorted(actual_reason_names)!r}"
    )


def test_reason_inventory_anchor_phrases_are_themselves_present_in_the_real_source() -> None:
    """[Negative control for the guard above] Given the same two anchor
    phrases the guard above relies on to bracket the inventory.
    When `tests/unit/test_activity.py`'s real source is searched for each.
    Then both are found, in order — proving the guard above is not silently
    matching an empty span (e.g. via `str.index` raising `ValueError` and
    the test erroring in a way that could be mistaken for "nothing to
    check") but genuinely bracketing real, non-empty content."""
    if not _TEST_ACTIVITY_PATH.exists():
        pytest.skip("tests/unit/test_activity.py does not exist.")
    source = _TEST_ACTIVITY_PATH.read_text(encoding="utf-8")
    assert _REASON_INVENTORY_START in source
    assert _REASON_INVENTORY_END in source
    assert source.index(_REASON_INVENTORY_START) < source.index(_REASON_INVENTORY_END)


def test_reason_inventory_guard_is_tripped_by_a_tag_removed_from_the_list_alone() -> None:
    """[Gate 5 finding 1 — demonstrated against a real mutation, not merely
    asserted] Given the SAME paragraph's own aside sentence mentions
    `REASON_STAMP_UNRECORDABLE` by name, OUTSIDE the tightened list span
    (sanity-checked below via `_extract_documented_reason_names` finding it
    absent from a span that does NOT include the aside).
    When the REAL source of `tests/unit/test_activity.py` is read and then
    surgically mutated to remove `REASON_STAMP_UNRECORDABLE` from the LIST
    ONLY — a single, targeted string replacement of the exact phrase
    joining the list's last item to the prose that follows it — leaving
    every other character, INCLUDING the aside's own separate mention of
    the identical tag, completely untouched.
    Then the SAME extraction function the real guard test uses reports a
    documented set that is MISSING the tag and DIFFERS from
    `partgraph.util.activity.__all__`'s actual set — proving the tightened
    anchors genuinely trip on this exact scenario (a tag dropped from the
    list while surrounding prose survives), the precise blind spot the wide
    anchors had. This is a demonstration against real, mutated text, not an
    assertion about what the anchors are expected to do.
    """
    activity = pytest.importorskip(
        "partgraph.util.activity",
        reason="partgraph.util.activity does not exist yet (expected pre-PR-C).",
    )
    actual_reason_names = {name for name in activity.__all__ if name.startswith("REASON_")}
    assert "REASON_STAMP_UNRECORDABLE" in actual_reason_names, (
        "sanity: this control is only meaningful while the module actually "
        "defines this tag"
    )

    assert _TEST_ACTIVITY_PATH.exists(), f"expected {_TEST_ACTIVITY_PATH} to exist"
    source = _TEST_ACTIVITY_PATH.read_text(encoding="utf-8")

    aside_mention = "go stale the way it did once already — `REASON_STAMP_UNRECORDABLE`"
    assert aside_mention in source, (
        "expected the paragraph's own aside to mention REASON_STAMP_UNRECORDABLE "
        "by name, outside the list — the scenario this control exercises"
    )

    list_tail = "`REASON_STAMP_UNRECORDABLE` — plain string tags"
    assert source.count(list_tail) == 1, (
        f"expected exactly one occurrence of the list's own tail to mutate: "
        f"{source.count(list_tail)}"
    )
    mutated_source = source.replace(list_tail, "— plain string tags", 1)
    assert mutated_source != source, "sanity: the targeted mutation must change something"
    assert aside_mention in mutated_source, (
        "the mutation must leave the aside's own separate mention of the tag "
        "completely untouched — proving this is a genuine 'removed from the "
        "list alone' scenario, not a strawman that also strips the aside"
    )

    documented_after_mutation = _extract_documented_reason_names(mutated_source)
    assert "REASON_STAMP_UNRECORDABLE" not in documented_after_mutation, (
        "the tightened guard must not recover the removed tag from the "
        "aside sentence outside its span"
    )
    assert documented_after_mutation != actual_reason_names, (
        "removing a tag from the list alone, with the aside's own mention of "
        "the SAME tag left completely untouched, must trip the guard — this "
        "is exactly the blind spot Gate 5 found in the pre-tightening anchors"
    )
