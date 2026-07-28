"""
Tests: PR-C (feat/db-idle-autostop) — [Gate 3b SHOULD-FIX] a MECHANICAL
safety net catching a DB-touching command that omits the activity wiring
entirely (C-1/C-2), not merely the per-command behavioural proofs already in
`tests/unit/test_cli_activity_wiring.py`.

CHOICE MADE (the coordinator asked for one of two options, named
explicitly): a mechanical, ALLOWLIST-ENUMERATION scan — mirroring the
convention already established in `tests/unit/test_lifecycle_architecture.py`
(regex/AST scans over real source text) and
`tests/unit/test_repo_never_executes_lifecycle_mutations.py` (the SAME
fixed-point, same-module call-graph reachability technique this file reuses,
inverted: instead of flagging a reachable DANGEROUS call, this flags an
UNREACHED required one) — over funnelling `held_lease`'s entry alone through
`_connect_dgraph()`.

Why the scan, not the funnel: `_connect_dgraph()` is already the real
autostart funnel and genuinely covers 7 of the 9 allowlisted commands
(`db apply-schema` / `db check-index` bypass it and wire autostart
EXPLICITLY — see `src/partgraph/cli.py`'s own `_connect_dgraph` docstring,
"Seven of the nine autostart-capable commands reach the database exclusively
through here"). Funnelling `held_lease`'s ENTRY the same way would still
leave its EXIT and the (necessarily body-specific, per-page) `touch_activity`
call ungated for every command — the coordinator's own framing: "touch_activity
must fire after each command's own work, which is body-specific and genuinely
cannot be centralised". A funnel only ever protects ONE of the three moving
parts (entry); this scan checks that ALL NINE commands' own source
(transitively, within `cli.py`) references BOTH `held_lease` and
`touch_activity` — catching an omission of ANY of the three practically
(entry, exit-via-context-manager, or the stamp update itself), for the
CURRENT nine commands and for a future tenth added to the allowlist below.

THIS FILE DOES NOT IMPORT `partgraph.util.activity` — it reads
`src/partgraph/cli.py`'s own source text directly via `ast.parse`, exactly
like `test_lifecycle_architecture.py` reads `lifecycle.py`'s source without
importing it, so this file always collects and its tests genuinely RUN (and
currently FAIL, not skip — `cli.py` already exists; its command bodies just
do not reference the activity primitives yet, which is the correct,
meaningful RED signal, stronger than a collection-level ModuleNotFoundError).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_CLI_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "src" / "partgraph" / "cli.py"
)

#: The nine DB-touching commands PR-B2 already established as the autostart
#: allowlist (`tests/unit/test_cli_autostart.py`'s own B-6 section), mapped
#: to the actual `cli.py` function name that does the DB-touching work.
#: `ingest jlcparts` maps to `_stage_load` specifically (not `ingest_jlcparts`
#: itself): only the LOAD stage touches the database — fetch/normalize do
#: not (`tests/unit/test_cli_autostart.py`'s own
#: `test_b6_ingest_jlcparts_fetch_stage_never_triggers_autostart` /
#: `..._normalize_stage_never_triggers_autostart` pin that already).
_DB_TOUCHING_ALLOWLIST: dict[str, str] = {
    "stats": "stats",
    "search": "search",
    "show": "show",
    "embed": "embed",
    "refresh-links": "refresh_links",
    "refresh": "refresh",
    "db apply-schema": "apply_schema",
    "db check-index": "check_index",
    "ingest jlcparts (load stage)": "_stage_load",
}

_REQUIRED_IDENTIFIERS = ("held_lease", "touch_activity")


def _read_cli_source_or_skip() -> str:
    if not _CLI_PATH.exists():  # pragma: no cover — cli.py always exists in this repo
        pytest.skip("src/partgraph/cli.py does not exist.")
    return _CLI_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The scanner (test-only tooling — mirrors
# test_repo_never_executes_lifecycle_mutations.py's own
# _local_wrapper_function_names fixed-point technique, inverted).
# ---------------------------------------------------------------------------


def _module_level_function_defs(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """Return every TOP-LEVEL function def in *tree*, by name.

    Deliberately module-level only (mirrors the same scope discipline
    `test_repo_never_executes_lifecycle_mutations.py` already documents and
    defends): a same-named nested/local function inside an unrelated command
    is never conflated with the module-level helper of the same name.
    """
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _identifiers_in(node: ast.AST) -> set[str]:
    """Every `Name.id` and `Attribute.attr` anywhere in *node*'s subtree.

    Catches BOTH shapes the wiring could take: a bare call (`touch_activity(...)`,
    `Name`) and a `with held_lease(...):` (also a bare `Name` in `ast.With`'s
    `context_expr`) — `ast.walk` reaches it either way without special-casing
    the `With` statement at all.
    """
    ids: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            ids.add(child.id)
        elif isinstance(child, ast.Attribute):
            ids.add(child.attr)
    return ids


def _reachable_identifiers(
    entry_name: str, func_defs: dict[str, ast.FunctionDef]
) -> set[str]:
    """Every identifier reachable from *entry_name*'s own body, transitively,
    through same-module function calls only (fixed-point BFS — mirrors
    `_local_wrapper_function_names`'s own "any depth" resolution, applied to
    identifier collection instead of a single dangerous-name membership
    test). Cross-module resolution is explicitly out of scope, exactly as
    that file's own docstring discloses for its version of this technique.
    """
    if entry_name not in func_defs:
        return set()
    visited_functions: set[str] = set()
    frontier = [entry_name]
    all_identifiers: set[str] = set()
    while frontier:
        name = frontier.pop()
        if name in visited_functions:
            continue
        visited_functions.add(name)
        node = func_defs.get(name)
        if node is None:
            continue
        found = _identifiers_in(node)
        all_identifiers |= found
        for candidate in found:
            if candidate in func_defs and candidate not in visited_functions:
                frontier.append(candidate)
    return all_identifiers


# ---------------------------------------------------------------------------
# Self-test: prove the scanner has real teeth BEFORE trusting it against
# cli.py (mirrors test_repo_never_executes_lifecycle_mutations.py's own
# "Gate 3a BLOCKING 1" self-test discipline — a scanner that always passes
# proves nothing).
# ---------------------------------------------------------------------------


def _parse(source: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(source, filename="canary.py")
    return _module_level_function_defs(tree)


def test_scanner_finds_a_direct_reference_in_the_entry_functions_own_body() -> None:
    """[Positive control] Given a command that calls both primitives
    directly in its own body.
    When reachability is computed from that command.
    Then both required identifiers are found."""
    src = (
        "def held_lease(*a, **k): ...\n"
        "def touch_activity(*a, **k): ...\n"
        "def a_command():\n"
        "    with held_lease():\n"
        "        touch_activity()\n"
    )
    func_defs = _parse(src)
    reachable = _reachable_identifiers("a_command", func_defs)
    for required in _REQUIRED_IDENTIFIERS:
        assert required in reachable, f"expected {required!r} to be found, got {reachable!r}"


def test_scanner_finds_a_reference_reached_through_one_hop_via_a_helper() -> None:
    """[Positive control — the shape this repo's real commands actually use,
    e.g. `embed` calling `_embed_all_pages`, which is where the per-page
    heartbeat is expected to live] Given a command that calls a SEPARATE,
    same-module helper, and the HELPER (not the command itself) references
    the primitives.
    When reachability is computed from the command.
    Then both are still found — one-hop, same-module resolution."""
    src = (
        "def held_lease(*a, **k): ...\n"
        "def touch_activity(*a, **k): ...\n"
        "def _do_pages():\n"
        "    with held_lease():\n"
        "        touch_activity()\n"
        "def a_command():\n"
        "    _do_pages()\n"
    )
    func_defs = _parse(src)
    reachable = _reachable_identifiers("a_command", func_defs)
    for required in _REQUIRED_IDENTIFIERS:
        assert required in reachable, f"expected {required!r} to be found, got {reachable!r}"


def test_scanner_correctly_reports_neither_identifier_when_genuinely_absent() -> None:
    """[Negative control — proves the scanner does not just always pass]
    Given a command whose body (and everything it calls) never mentions
    either primitive.
    When reachability is computed from that command.
    Then NEITHER required identifier is found."""
    src = (
        "def held_lease(*a, **k): ...\n"
        "def touch_activity(*a, **k): ...\n"
        "def _unrelated_helper():\n"
        "    return 1\n"
        "def a_command():\n"
        "    return _unrelated_helper()\n"
    )
    func_defs = _parse(src)
    reachable = _reachable_identifiers("a_command", func_defs)
    for required in _REQUIRED_IDENTIFIERS:
        assert required not in reachable, (
            f"scanner falsely reported {required!r} reachable when it is genuinely absent"
        )


def test_scanner_does_not_conflate_a_same_named_nested_function_in_another_command() -> None:
    """[Negative control] Given TWO unrelated top-level commands, one of
    which happens to define a NESTED, same-named local function that itself
    references the primitives — nested functions are never collected as
    module-level `func_defs` at all, so this must not leak into the OTHER
    command's own reachability."""
    src = (
        "def held_lease(*a, **k): ...\n"
        "def touch_activity(*a, **k): ...\n"
        "def other_command():\n"
        "    def _local_helper():\n"
        "        with held_lease():\n"
        "            touch_activity()\n"
        "    return _local_helper()\n"
        "def a_command():\n"
        "    return 1\n"
    )
    func_defs = _parse(src)
    assert "_local_helper" not in func_defs, (
        "sanity check: a nested function must not appear in module-level func_defs"
    )
    reachable = _reachable_identifiers("a_command", func_defs)
    for required in _REQUIRED_IDENTIFIERS:
        assert required not in reachable


# ---------------------------------------------------------------------------
# The real scan.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command_label", "entry_function"),
    sorted(_DB_TOUCHING_ALLOWLIST.items()),
)
def test_db_touching_command_source_references_both_activity_primitives(
    command_label: str, entry_function: str
) -> None:
    """[Gate 3b SHOULD-FIX] Given *command_label*'s own entry function in
    `src/partgraph/cli.py`, and every same-module function it transitively
    calls (fixed-point, same discipline as
    `test_repo_never_executes_lifecycle_mutations.py`'s own wrapper
    resolution).
    When that reachable set of identifiers is inspected.
    Then it references BOTH `held_lease` and `touch_activity` somewhere —
    catching a command (current or future) that omits the activity wiring
    entirely, which no per-command behavioural test alone would catch
    unless a test happened to already exist for that exact command.
    """
    source = _read_cli_source_or_skip()
    tree = ast.parse(source, filename=str(_CLI_PATH))
    func_defs = _module_level_function_defs(tree)
    assert entry_function in func_defs, (
        f"{command_label!r}'s own entry function {entry_function!r} was not "
        f"found as a top-level function in src/partgraph/cli.py — the "
        "allowlist above may be stale."
    )
    reachable = _reachable_identifiers(entry_function, func_defs)
    missing = [name for name in _REQUIRED_IDENTIFIERS if name not in reachable]
    assert not missing, (
        f"{command_label!r} ({entry_function}) never references {missing!r} "
        "anywhere in its own body or anything it transitively calls within "
        "cli.py — this command completes real DB work without recording "
        "activity (C-1) and/or without holding a lease (C-3)."
    )
