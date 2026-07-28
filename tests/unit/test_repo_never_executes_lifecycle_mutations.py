"""
Tests: PR-B1 (feat/db-lifecycle-doctor-and-docs) — AC-B2, "the repo documents
and detects, it never executes".

`docs/db-lifecycle.md` (AC-B1) and `partgraph db doctor` (AC-B3) both need to
TALK ABOUT the quadlet unit's `~/.config/containers/systemd/` directory,
`systemctl --user daemon-reload`, and the word "quadlet" itself — that
directory is shared with FIVE unrelated cve-graph units
(`cve-alpha.container`, `cve-loader.container`, `cve-ratel.container`,
`cve-zero.container`, `min-web.container`) this repository has promised, in
ADR-0021, never to touch. If PartGraph's own Python source ever actually
EXECUTED a write into that directory, or actually ran `daemon-reload`, it
would be mutating a directory it does not own. This file is the mechanical
guard for that promise.

WHY THIS IS NOT A NAIVE "grep the whole file for the word" SCAN — and why
that would be WRONG: `src/partgraph/util/lifecycle.py` ALREADY legitimately
says "quadlet" eight times, always in a docstring (PR-A). The forthcoming
`db doctor` command (AC-B3) is REQUIRED to print the literal words
"WantedBy=" and "daemon-reload" as part of its own remediation text — that
is DISPLAY output (`console.print(...)`), never an executed command. A
blanket "no non-docstring string may contain this word" rule would make
AC-B3's own remediation text impossible to write while staying green here,
which would be a genuinely self-contradictory pair of acceptance tests.

THE ACTUAL SEMANTIC LINE this file draws: a forbidden term may appear
ANYWHERE in `src/` **except** where it would reach a call this process
would itself EXECUTE or a filesystem WRITE this process would itself
PERFORM — subprocess/process-exec calls (`subprocess.run`/`Popen`/`call`/
`check_call`/`check_output`, `os.system`/`os.popen`/`os.exec*`/`os.spawn*`)
and file-mutating calls (`open`/`write_text`/`write_bytes`/`replace`/
`rename`/`remove`/`unlink`/`rmtree`/`move`/`copy*`/`mkdir`/`makedirs`/
`rmdir`/`symlink`/`chmod`/`truncate`) — INCLUDING when the actual call is
made through a LOCAL wrapper function around one of those (this repo's own
idiom, see the GATE 3A note below), and INCLUDING when a forbidden term
reaches such a call via a same-file variable assignment rather than an
inline literal. Everything else — a docstring, a `#` comment (never even
reaches the AST, so it is automatically exempt), a `console.print(...)`
remediation message, a module-level constant used only for display — is
legitimate prose and is deliberately left alone. This is also EXACTLY the
"concrete example" this file's own author was asked to defend against:
"genuinely fail if someone later adds a `subprocess.run([...
"daemon-reload"])`" — that shape, and the two shapes below it that this
repo's real code actually uses, are precisely what `_find_violations`
detects, and the self-test section proves it does, with positive controls
(must flag) and negative controls (must NOT flag: docstring, comment,
print/display text).

GATE 3A BLOCKING 1 (fixed here; the earlier version of this scanner was
verified, not merely believed, to catch NOTHING real): grepping for a
literal `subprocess.run([` argv-list-opener across
src/partgraph/util/lifecycle.py and src/partgraph/cli.py returns NOTHING.
Every real call in this repo goes through a local wrapper —
`lifecycle.py`'s own `_run_capture(argv, *, timeout)`, whose body calls
`subprocess.run(argv, ...)` with `argv` as a bound PARAMETER, never a
literal — so a scanner that only recognised the builtin names `run`/
`Popen`/... as dangerous matched zero real call sites, while its own
docstring FALSELY claimed the opposite ("every `subprocess.run([...])` call
already in `src/partgraph/util/lifecycle.py` and `src/partgraph/cli.py`") —
a second fabricated, un-grepped claim in this PR's test docstrings, found and
corrected together with the phantom `cli.py` fixture-convention citation
(see CONTRIBUTING.md's "Test fixtures stay local to their file" paragraph).
Both were justifications written to make a weaker check look sufficient;
that is the pattern to distrust here, not just these two instances.
`_effective_dangerous_names` now also recognises a same-module wrapper
function BY NAME (fixed-point, any depth — "resolving the wrapper by name
within the same module is enough, full interprocedural analysis is not
wanted"), and `_scoped_assignment_fragments` resolves the MORE common real
shape — `argv = [...]` one line earlier, then `_run_capture(argv, ...)`,
the shape MOST of `_run_capture`'s own real callers use (e.g.
`_mounts_data_volume`'s S2 inspect, `unit_state`'s `systemctl show`,
`volume_exists`, and `_stop_instances`'s per-target `stop`) — not only the
INLINE-literal shape a minority of callers use instead (e.g.
`find_partgraph_instances`'s `ps --all` enumeration,
`_stop_unit_if_active`'s `systemctl stop`). Deliberately described here by
FUNCTION NAME and CALL SHAPE, never by line number: an earlier draft of
this paragraph cited specific line ranges, which were accurate at the
commit they were written against and became stale prose the very next time
`lifecycle.py` was edited (Gate 5 review) — a name is far more durable than
a line number, and
`test_lifecycle_still_uses_both_the_inline_and_the_separate_variable_run_capture_shape`
below makes the underlying CLAIM (both shapes are still real, current code)
something a test verifies on every run, not something prose merely asserts
once and line numbers can silently falsify. This resolution is SCOPE-PRECISE
(walked once per function, via `_walk_same_scope`, not once per whole
file): a bare Name is resolved only against an assignment DIRECTLY in the
SAME function (or the module level) as the call using it — a same-named
local in an unrelated function, or a wrapper's OWN parameter of the same
name (a parameter is never an `ast.Assign`), is never conflated with it.
An EARLIER draft of this fix used one flat, whole-file assignment map
instead, and a self-test (kept, not deleted, once the bug it caught was
fixed) demonstrated the resulting false attribution directly: `_run_capture`'s
own `argv` PARAMETER was wrongly resolved against an UNRELATED caller's
local `argv` variable purely because they share a name, reporting the same
violation twice at two unrelated lines. `_joined_div_chain_fragments`
additionally reconstructs a term split across pathlib's `/` operand chain
(`Path.home() / ".config" / "containers" / "systemd" / ...`), which
otherwise splits the compound term "containers/systemd" across separate
`ast.Constant` fragments that individually match nothing; it also
deliberately processes only the OUTERMOST `Div` BinOp of each chain (an
identical, still-disclosed reason: an earlier draft processed every nested
BinOp in the chain independently and reported OVERLAPPING PREFIXES of the
same chain as separate, redundant violations).

HONEST SCOPE LIMITATION (documented, not hidden — this file's own author
must not overclaim, and was corrected once already in this PR for
overclaiming): the assignment resolution above is deliberately SINGLE-HOP —
`a = [..., "daemon-reload"]; f(a)` is caught, but
`a = b; b = [..., "daemon-reload"]; f(a)` (aliasing through a SECOND
variable within the SAME scope) is not; `a` is never traced through `b`.
True cross-MODULE (interprocedural, across files) resolution remains
entirely out of scope, as explicitly requested. This is a known, disclosed
gap, not a claim of exhaustive proof.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess

import pytest

# ---------------------------------------------------------------------------
# The scanner itself (test-only tooling — not a production feature; mirrors
# tests/unit/test_lifecycle_architecture.py's own embedded regex scanner and
# tests/unit/test_repo_skeleton.py's embedded _HOME_PATH_PATTERN convention).
# ---------------------------------------------------------------------------

#: The three literals AC-B2 forbids inside executable code. Matched
#: case-insensitively so a capitalized variant ("Daemon-Reload") cannot slip
#: through by accident.
FORBIDDEN_TERMS: tuple[str, ...] = ("containers/systemd", "daemon-reload", "quadlet")

#: Process-execution call names (the last dotted attribute, or a bare name).
_EXEC_CALL_NAMES = frozenset({
    "run", "Popen", "call", "check_call", "check_output", "system", "popen",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "posix_spawn", "posix_spawnp",
})

#: Filesystem-mutating call names.
_MUTATE_CALL_NAMES = frozenset({
    "open", "write_text", "write_bytes", "replace", "rename", "remove", "unlink",
    "rmtree", "move", "copy", "copy2", "copyfile", "copytree", "mkdir", "makedirs",
    "rmdir", "symlink", "chmod", "truncate",
})

DANGEROUS_CALL_NAMES: frozenset[str] = _EXEC_CALL_NAMES | _MUTATE_CALL_NAMES


def _callee_name(node: ast.Call) -> str | None:
    """Return the call's own resolved name.

    Handles three shapes:
      - a plain dotted attribute (``subprocess.run(...)`` -> ``"run"``);
      - a bare name (``run(...)`` -> ``"run"``, also how a local wrapper
        function or an import-aliased name resolves);
      - [cheap defence-in-depth] dynamic dispatch via ``getattr(x, "name")``
        used AS the callee expression itself (``getattr(subprocess,
        "run")(...)``) — the literal string argument IS the resolved name.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    if (
        isinstance(func, ast.Call)
        and isinstance(func.func, ast.Name)
        and func.func.id == "getattr"
        and len(func.args) >= 2
        and isinstance(func.args[1], ast.Constant)
        and isinstance(func.args[1].value, str)
    ):
        return func.args[1].value
    return None


def _flatten_div_chain(node: ast.BinOp) -> list[ast.AST]:
    """Flatten a LEFT-associative chain of ``/`` (``ast.Div``) BinOps into
    its operands in left-to-right order, e.g. ``a / b / c`` (which parses as
    ``BinOp(BinOp(a, Div, b), Div, c)``) becomes ``[a, b, c]``.
    """
    if isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Div):
        return [*_flatten_div_chain(node.left), node.right]
    return [node.left, node.right]


def _joined_div_chain_fragments(node: ast.AST) -> list[str]:
    """[GATE 3A BLOCKING 1] Reconstruct a forbidden term split across
    pathlib's ``/`` operator, e.g. ``Path.home() / ".config" / "containers"
    / "systemd"``: each segment is its OWN separate ``ast.Constant``, so no
    SINGLE fragment contains "containers/systemd" — this joins each MAXIMAL
    run of >= 2 ADJACENT string-literal operands with ``'/'`` so the
    compound term becomes visible again, exactly as it would read on disk.
    A non-literal operand (``Path.home()``) breaks a run without discarding
    what came before it.

    Processes ONLY the OUTERMOST ``Div`` BinOp of each chain: a chain of N
    ``/`` operators parses as N-1 NESTED ``BinOp`` nodes (``a/b/c`` is
    ``BinOp(BinOp(a, Div, b), Div, c)``), and ``ast.walk`` visits every one
    of them individually — processing each independently would re-derive
    overlapping PREFIXES of the same chain (e.g. both ``"b/c"`` and
    ``"a/b/c"``) as separate, redundant fragments. A ``BinOp``/``Div`` node
    that is itself the ``.left`` of another ``Div`` BinOp is an interior
    link, not a chain's outermost node, and is skipped; the outermost node's
    own flatten already covers its full length.
    """
    div_nodes = [
        child for child in ast.walk(node)
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.Div)
    ]
    interior_ids = {
        id(child.left) for child in div_nodes
        if isinstance(child.left, ast.BinOp) and isinstance(child.left.op, ast.Div)
    }
    joined: list[str] = []
    for child in div_nodes:
        if id(child) in interior_ids:
            continue
        run: list[str] = []
        for operand in _flatten_div_chain(child):
            if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                run.append(operand.value)
                continue
            if len(run) >= 2:
                joined.append("/".join(run))
            run = []
        if len(run) >= 2:
            joined.append("/".join(run))
    return joined


def _string_fragments(node: ast.AST) -> list[str]:
    """Collect every literal string fragment anywhere inside *node*'s subtree.

    ``ast.walk`` recurses into EVERY descendant regardless of nesting depth,
    so this reaches string literals inside a nested list literal
    (``["a", "b"]``) and inside an f-string's literal segments (each
    non-interpolated chunk of an ``ast.JoinedStr`` is its own ``ast.Constant``
    child) without any special-casing. Also includes reconstructed
    ``/``-chain fragments (:func:`_joined_div_chain_fragments`).
    """
    plain = [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]
    return plain + _joined_div_chain_fragments(node)


def _import_aliases_of_dangerous_names(tree: ast.Module) -> set[str]:
    """[GATE 3A BLOCKING 1, cheap defence-in-depth] Return every LOCAL alias
    a ``from ... import <dangerous> as <alias>`` statement introduces for an
    already-known-dangerous name (e.g. ``from subprocess import Popen as
    P`` -> ``{"P"}``). A plain ``import subprocess as sp`` needs no special
    handling: ``sp.run(...)``'s own ``.attr`` is still ``"run"``, already
    matched by :func:`_callee_name` regardless of the module's own alias.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in DANGEROUS_CALL_NAMES and alias.asname:
                    aliases.add(alias.asname)
    return aliases


def _local_wrapper_function_names(tree: ast.Module, seed: frozenset[str]) -> set[str]:
    """[GATE 3A BLOCKING 1] Return the names of every function DEFINED in
    this module whose OWN body calls something already known dangerous —
    "resolving the wrapper BY NAME within the same module", exactly the
    scope this fix was asked for (no cross-module/interprocedural
    resolution). Iterated to a FIXED POINT so a wrapper-of-a-wrapper, at any
    depth, is still recognised (e.g. a future ``doctor()`` helper that calls
    ``_run_capture`` which calls ``subprocess.run`` — two hops).

    Deliberately NOT scope-precise: ``ast.walk(func)`` also descends into any
    function NESTED inside ``func``, so a call inside a nested function could
    be mis-attributed to the OUTER function's name too. This over-
    approximates rather than under-approximates — the safe direction for a
    guard whose job is to never silently miss a real violation.
    """
    func_defs = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    wrapper_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        current = seed | wrapper_names
        for func in func_defs:
            if func.name in wrapper_names:
                continue
            for call in ast.walk(func):
                if isinstance(call, ast.Call) and _callee_name(call) in current:
                    wrapper_names.add(func.name)
                    changed = True
                    break
    return wrapper_names


def _effective_dangerous_names(tree: ast.Module) -> frozenset[str]:
    """The full, file-scoped set of callee names this scanner treats as
    dangerous: the base builtin set, PLUS import aliases of any of them,
    PLUS every local wrapper function (at any fixed-point depth) whose own
    body calls something already in that combined set.
    """
    seed = frozenset(DANGEROUS_CALL_NAMES | _import_aliases_of_dangerous_names(tree))
    return frozenset(seed | _local_wrapper_function_names(tree, seed))


#: Node types that open a NEW lexical scope — a bare Name assignment on one
#: side of one of these never leaks into the other side, matching real
#: Python scoping (a function's own local variables are invisible to a
#: DIFFERENT function, even one defined earlier in the same file).
_SCOPE_BOUNDARY_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_same_scope(scope_node: ast.AST):
    """Yield every descendant of *scope_node* belonging to *scope_node*'s
    OWN lexical scope (a module body or a single function body): recurses
    through control-flow blocks (``if``/``for``/``while``/``with``/``try``/
    ``match``, which share the enclosing scope in Python) but does NOT
    descend past a nested function/lambda/class boundary — each of those
    opens its own separate scope, walked independently as its own entry in
    :func:`_find_violations`'s ``scopes`` list.
    """
    stack = list(ast.iter_child_nodes(scope_node))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, _SCOPE_BOUNDARY_TYPES):
            continue
        stack.extend(ast.iter_child_nodes(current))


def _scoped_assignment_fragments(scope_node: ast.AST) -> dict[str, list[str]]:
    """[GATE 3A BLOCKING 1, the MORE common real shape than an inline
    literal] Map every simple ``name = <expr>`` assignment DIRECTLY within
    *scope_node*'s own scope (never a NESTED function's) to the string/
    ``/``-chain fragments found in its right-hand side — this is what lets
    a bare-Name argument at a dangerous call site (e.g. ``argv = [...]`` one
    line earlier, then ``_run_capture(argv, timeout=...)`` — the shape MOST
    of ``_run_capture``'s own real callers use, e.g. ``_mounts_data_volume``,
    ``unit_state`` and ``volume_exists``) be resolved. Described by name,
    not by line number — see the module docstring's GATE 3A BLOCKING 1 note
    for why.

    SCOPE-PRECISE: a bare Name is resolved only against an assignment in the
    SAME function (or the module level, for module-level calls) — never a
    same-named local in a DIFFERENT, unrelated function, and never a
    same-named PARAMETER of the function containing the call (a parameter is
    never an ``ast.Assign`` at all, so it is correctly never present here).
    Still deliberately SINGLE-HOP, not transitive: ``a = [...]`` is
    resolved; ``a = b; b = [...]`` is not — ``a`` is never traced through
    ``b``. This stays intentionally short of "full interprocedural
    analysis", which was explicitly not wanted: it is a single function's
    (or the module's) own local variables only — no execution, no
    control-flow graph, no cross-function dataflow.
    """
    mapping: dict[str, list[str]] = {}
    for node in _walk_same_scope(scope_node):
        if isinstance(node, ast.Assign):
            fragments = _string_fragments(node.value)
            if not fragments:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    mapping.setdefault(target.id, []).extend(fragments)
    return mapping


def _resolved_call_fragments(node: ast.Call, assignment_map: dict[str, list[str]]) -> list[str]:
    """Return *node*'s own string fragments PLUS, for every bare ``Name``
    anywhere in its subtree (covering both a direct argument and a Name
    inside the callee expression, e.g. ``p.write_text(...)``'s ``p``), the
    fragments of any same-named assignment found in *assignment_map* (the
    CALLING scope's own local assignments — see :func:`_scoped_assignment_fragments`).
    """
    fragments = _string_fragments(node)
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in assignment_map:
            fragments.extend(assignment_map[child.id])
    return fragments


def _find_violations(source: str, label: str) -> list[str]:
    """Return one message per forbidden term found inside a dangerous call's
    subtree in *source* (labelled *label* for the message).

    "Dangerous" is FILE-SCOPED, not a fixed global set
    (:func:`_effective_dangerous_names`): the builtin exec/mutate names,
    their import aliases, and any LOCAL wrapper function (at any depth)
    whose own body calls something already dangerous — this repo's actual
    idiom is `subprocess.run` wrapped once by a local `_run_capture(argv,
    *, timeout)` helper, so recognising only the builtin names caught
    nothing real at all (Gate 3a BLOCKING 1).

    Scans the WHOLE matched ``Call`` node — including ``node.func`` — not
    just ``node.args``/``node.keywords``, so a chained callee expression
    (``Path("...").write_text("x")``, whose forbidden string lives inside
    ``node.func.value``) is still caught. A bare ``Name`` ANYWHERE in that
    subtree is additionally resolved against that SAME CALL's own enclosing
    SCOPE's local assignments (:func:`_scoped_assignment_fragments`,
    computed once per scope and reused for every call directly in it), so
    the more common ``argv = [...]; _run_capture(argv, ...)`` shape is
    caught too, not only an inline literal at the call site — while a
    same-named variable in a DIFFERENT function, or a wrapper's OWN
    parameter of the same name, is correctly never conflated with it.

    Iterates scope-by-scope (the module, then every function/async function
    def, regardless of nesting) rather than one flat whole-tree walk, which
    is what makes the scope precision above possible. De-duplicates the
    returned messages (``dict.fromkeys``) as a final safety net.
    """
    tree = ast.parse(source, filename=label)
    effective_dangerous = _effective_dangerous_names(tree)
    scopes: list[ast.AST] = [tree] + [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    violations: list[str] = []
    for scope in scopes:
        assignment_map = _scoped_assignment_fragments(scope)
        for node in _walk_same_scope(scope):
            if not isinstance(node, ast.Call):
                continue
            name = _callee_name(node)
            if name not in effective_dangerous:
                continue
            pieces = _resolved_call_fragments(node, assignment_map)
            for piece in pieces:
                lowered = piece.lower()
                for term in FORBIDDEN_TERMS:
                    if term in lowered:
                        violations.append(
                            f"{label}:{node.lineno}: forbidden term {term!r} found inside "
                            f"an argument to {name!r}() (directly, via a chained callee, "
                            f"or via a same-scope local variable): {piece!r}"
                        )
    return list(dict.fromkeys(violations))


# ---------------------------------------------------------------------------
# Self-test: prove the scanner has real teeth (positive controls) and does
# not false-positive on legitimate prose (negative controls) BEFORE trusting
# it to scan the real tree. Every snippet below is embedded source text, only
# ever passed to ast.parse() in-memory — none of it is written to disk, and
# none of it lives inside a Call this file itself would execute.
# ---------------------------------------------------------------------------


def test_scanner_flags_forbidden_term_inside_a_subprocess_exec_call() -> None:
    """[Positive control — the concrete example this AC names] Given a
    snippet containing ``subprocess.run(["systemctl", "--user",
    "daemon-reload"], check=False)``.
    When the scanner runs.
    Then it reports exactly one violation, naming 'daemon-reload'.
    """
    bad = (
        "import subprocess\n\n"
        "def bad():\n"
        '    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)\n'
    )
    violations = _find_violations(bad, "canary.py")
    assert len(violations) == 1, violations
    assert "daemon-reload" in violations[0]


def test_scanner_flags_forbidden_path_inside_a_file_mutating_call() -> None:
    """[Positive control] Given a snippet that WRITES into a path containing
    "containers/systemd" (``Path(...).write_text(...)``).
    When the scanner runs.
    Then it reports exactly one violation, naming 'containers/systemd'.
    """
    bad = (
        "from pathlib import Path\n\n"
        "def bad():\n"
        '    Path("$HOME/.config/containers/systemd/partgraph-dgraph.container")'
        '.write_text("x")\n'
    )
    violations = _find_violations(bad, "canary.py")
    assert len(violations) == 1, violations
    assert "containers/systemd" in violations[0]


def test_scanner_flags_forbidden_term_hidden_inside_an_f_string_argument() -> None:
    """[Positive control — evasion via f-string interpolation] Given a
    dangerous call whose argument is an f-string with an interpolated
    variable AND a literal 'quadlet' segment.
    When the scanner runs.
    Then it still reports the violation — ast.walk reaches an f-string's
    literal segments exactly like a plain string.
    """
    bad = (
        "import subprocess\n\n"
        "def bad(unit_name):\n"
        '    subprocess.run([f"systemctl --user status {unit_name} quadlet"])\n'
    )
    violations = _find_violations(bad, "canary.py")
    assert len(violations) == 1, violations
    assert "quadlet" in violations[0]


def test_scanner_flags_forbidden_term_reached_through_a_local_wrapper_function() -> None:
    """[Positive control — GATE 3A BLOCKING 1: this repo's OWN real idiom]
    Given a snippet shaped EXACTLY like every real subprocess call in
    src/partgraph/util/lifecycle.py: a module-local wrapper (mirroring
    ``_run_capture``) whose OWN body calls ``subprocess.run(argv, ...)``
    with ``argv`` as a bound PARAMETER — never a literal at that inner call
    site — and a caller that passes a forbidden literal INLINE at the
    WRAPPER's own call site (exactly the shape ``_stop_unit_if_active``'s
    ``systemctl stop`` call and ``find_partgraph_instances``'s ``ps --all``
    enumeration are written with for real — described by name, not line
    number, per the module docstring's GATE 3A BLOCKING 1 note).
    When the scanner runs.
    Then it reports the violation: the wrapper is recognised as dangerous
    because ITS OWN body calls a name already known dangerous.
    """
    bad = (
        "import subprocess\n\n"
        "def _run_capture(argv, *, timeout):\n"
        "    return subprocess.run(argv, capture_output=True, text=True, "
        "shell=False, timeout=timeout, check=False)\n\n"
        "def bad():\n"
        '    return _run_capture(["systemctl", "--user", "daemon-reload"], timeout=5.0)\n'
    )
    violations = _find_violations(bad, "canary.py")
    assert len(violations) == 1, violations
    assert "daemon-reload" in violations[0]


def test_scanner_flags_forbidden_term_reached_through_a_wrapper_call_sites_own_local_variable() -> None:
    """[Positive control — GATE 3A BLOCKING 1, the MORE common real shape]
    Given the SAME wrapper as above, but called the way MOST of
    ``_run_capture``'s own real callers actually write it (e.g.
    ``_mounts_data_volume``'s S2 inspect, ``unit_state``'s ``systemctl
    show``, and ``volume_exists`` — described by name, not line number, per
    the module docstring's GATE 3A BLOCKING 1 note): the argv list is built
    in its OWN assignment statement one line earlier, and the WRAPPER call
    site passes only a bare variable name — ``argv = [...];
    _run_capture(argv, timeout=...)`` — not an inline literal.
    When the scanner runs.
    Then it STILL reports the violation: a bare-Name argument to a
    recognised-dangerous call is resolved against that name's own
    same-scope assignment(s).
    """
    bad = (
        "import subprocess\n\n"
        "def _run_capture(argv, *, timeout):\n"
        "    return subprocess.run(argv, capture_output=True, text=True, "
        "shell=False, timeout=timeout, check=False)\n\n"
        "def bad():\n"
        '    argv = ["systemctl", "--user", "daemon-reload"]\n'
        "    return _run_capture(argv, timeout=5.0)\n"
    )
    violations = _find_violations(bad, "canary.py")
    assert len(violations) == 1, violations
    assert "daemon-reload" in violations[0]


def test_scanner_flags_forbidden_term_split_across_a_pathlib_slash_chain() -> None:
    """[Positive control — GATE 3A BLOCKING 1: component-wise path
    construction] Given a target path built as ``Path.home() / ".config" /
    "containers" / "systemd" / "x"`` — pathlib's ``/`` (``__truediv__``)
    operator — where each segment is its OWN separate ``ast.Constant``, so
    no single fragment contains "containers/systemd" on its own.
    When the scanner runs.
    Then it still reports the violation: adjacent string-literal operands
    of a ``/``-chain are reconstructed and matched jointly.
    """
    bad = (
        "from pathlib import Path\n\n"
        "def bad():\n"
        '    (Path.home() / ".config" / "containers" / "systemd" / "x")'
        '.write_text("y")\n'
    )
    violations = _find_violations(bad, "canary.py")
    assert len(violations) == 1, violations
    assert "containers/systemd" in violations[0]


def test_scanner_flags_forbidden_term_via_getattr_dynamic_dispatch() -> None:
    """[Positive control — cheap defence-in-depth] Given a call issued via
    ``getattr(subprocess, "run")(...)`` instead of ``subprocess.run(...)``
    — the callee is not a plain ``Name``/``Attribute`` at all.
    When the scanner runs.
    Then it still reports the violation: a ``getattr(x, "name")`` used as
    the callee expression is resolved to ``"name"``.
    """
    bad = (
        "import subprocess\n\n"
        "def bad():\n"
        '    getattr(subprocess, "run")(["systemctl", "--user", "daemon-reload"])\n'
    )
    violations = _find_violations(bad, "canary.py")
    assert len(violations) == 1, violations
    assert "daemon-reload" in violations[0]


def test_scanner_flags_forbidden_term_via_an_import_alias() -> None:
    """[Positive control — cheap defence-in-depth] Given
    ``from subprocess import Popen as P`` and a call site written ``P(...)``
    — the bare name at the call site is the ALIAS, never the string
    ``"Popen"`` that appears in ``DANGEROUS_CALL_NAMES``.
    When the scanner runs.
    Then it still reports the violation: an import alias of a known
    dangerous name is itself treated as dangerous.
    """
    bad = (
        'from subprocess import Popen as P\n\n'
        "def bad():\n"
        '    P(["systemctl", "--user", "daemon-reload"])\n'
    )
    violations = _find_violations(bad, "canary.py")
    assert len(violations) == 1, violations
    assert "daemon-reload" in violations[0]


def test_scanner_does_not_flag_a_docstring_mentioning_all_three_terms() -> None:
    """[Negative control — mirrors lifecycle.py's real, legitimate usage]
    Given a function whose ONLY mention of all three forbidden terms is its
    own docstring (a bare Expr statement — never inside any Call's argument
    subtree).
    When the scanner runs.
    Then it reports ZERO violations.
    """
    good = (
        "def good():\n"
        '    """Mentions quadlet, daemon-reload and containers/systemd only here."""\n'
        "    return 1\n"
    )
    assert _find_violations(good, "canary.py") == []


def test_scanner_does_not_flag_a_comment_mentioning_all_three_terms() -> None:
    """[Negative control] Given the ONLY mention of all three forbidden terms
    is inside a ``#`` comment.
    When the scanner runs.
    Then it reports ZERO violations — comments are not part of the AST at
    all, so this is automatic, and is asserted here as documentation.
    """
    good = (
        "def good():\n"
        "    # quadlet, daemon-reload, and containers/systemd: just words here.\n"
        "    return 1\n"
    )
    assert _find_violations(good, "canary.py") == []


def test_scanner_does_not_flag_display_text_passed_to_a_non_dangerous_call() -> None:
    """[Negative control — resolves the AC-B2 vs AC-B3 tension] Given `db
    doctor`'s OWN required remediation text: a ``console.print(...)``-shaped
    call (never inside DANGEROUS_CALL_NAMES) whose string argument names
    'WantedBy=', 'daemon-reload' AND a 'containers/systemd' path.
    When the scanner runs.
    Then it reports ZERO violations — printing instructions for a HUMAN to
    run is not executing them.
    """
    good = (
        "def good(console):\n"
        '    console.print("Remove WantedBy=default.target from '
        "~/.config/containers/systemd/partgraph-dgraph.container, then run "
        'systemctl --user daemon-reload.")\n'
    )
    assert _find_violations(good, "canary.py") == []


def test_scanner_dangerous_call_names_include_the_documented_minimum() -> None:
    """[Lock the scanner's own coverage against silent erosion] Given the
    module docstring's own enumerated minimum call surface.
    When DANGEROUS_CALL_NAMES is read directly.
    Then it still contains every name that minimum promises.
    """
    required = {
        "run", "Popen", "call", "check_call", "check_output", "system",
        "write_text", "write_bytes", "open", "replace", "rename", "remove", "unlink",
    }
    missing = required - DANGEROUS_CALL_NAMES
    assert not missing, f"scanner's dangerous-call set is missing: {missing}"


# ---------------------------------------------------------------------------
# The real scan: every tracked *.py file under src/.
# ---------------------------------------------------------------------------


def _tracked_python_files_under_src(repo_root: pathlib.Path) -> list[pathlib.Path]:
    """Return every tracked ``*.py`` file under ``src/`` via ``git ls-files``,
    or a plain glob fallback when git is unavailable (mirrors
    ``tests/unit/test_repo_skeleton.py``'s own fallback style).
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "src"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            return [
                repo_root / p
                for p in result.stdout.splitlines()
                if p.endswith(".py") and (repo_root / p).is_file()
            ]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return sorted((repo_root / "src").rglob("*.py"))


def test_no_tracked_src_python_file_executes_a_forbidden_lifecycle_mutation(
    repo_root: pathlib.Path,
) -> None:
    """AC-B2: Given every tracked ``*.py`` file under ``src/`` (the whole
    production tree — not just ``lifecycle.py``, since `db doctor`, AC-B3,
    is new CLI code in ``cli.py``).
    When the scanner runs against each file's real source text.
    Then it reports ZERO violations — no forbidden term ever appears inside
    a subprocess-exec or file-mutating call's own arguments anywhere in the
    tree, currently OR after PR-B1 lands.
    """
    violations: list[str] = []
    for path in _tracked_python_files_under_src(repo_root):
        text = path.read_text(encoding="utf-8")
        label = str(path.relative_to(repo_root))
        violations.extend(_find_violations(text, label))

    assert not violations, (
        "Forbidden lifecycle-mutation literal(s) found inside an EXECUTABLE call "
        "in src/ (the repo must only ever document/detect this, never execute "
        "it):\n" + "\n".join(violations)
    )


def test_lifecycle_still_uses_both_the_inline_and_the_separate_variable_run_capture_shape(
    repo_root: pathlib.Path,
) -> None:
    """[Docstring-accuracy guard — Gate 5 review] Given this file's own
    module docstring describes TWO real call shapes ``_run_capture`` (the
    module-local wrapper around ``subprocess.run`` every real call in
    ``lifecycle.py`` goes through) is actually invoked with: an INLINE list
    literal at the call site (e.g. ``find_partgraph_instances``'s ``ps
    --all`` enumeration), and a SEPARATE ``argv = [...]`` assignment one
    line earlier (e.g. ``_mounts_data_volume``'s ``container inspect``) — a
    claim an earlier draft pinned to SPECIFIC line numbers, which rotted the
    very next time ``lifecycle.py`` was edited and were found stale by
    review.
    When ``src/partgraph/util/lifecycle.py``'s real source is parsed and
    every ``_run_capture(...)`` call site is inspected.
    Then AT LEAST ONE call site uses each shape — so the docstring's CLAIM
    stays something THIS TEST verifies on every run, not merely something
    prose asserts once while line numbers silently drift out from under it.
    Skips cleanly (not a failure) if the module does not exist.
    """
    lifecycle_path = repo_root / "src" / "partgraph" / "util" / "lifecycle.py"
    if not lifecycle_path.exists():
        pytest.skip("src/partgraph/util/lifecycle.py does not exist yet (expected pre-PR-A).")
    tree = ast.parse(lifecycle_path.read_text(encoding="utf-8"), filename=str(lifecycle_path))

    inline_shape_found = False
    variable_shape_found = False
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_run_capture"
        ):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Name):
            variable_shape_found = True
        elif isinstance(first_arg, ast.List):
            inline_shape_found = True

    assert inline_shape_found, (
        "expected at least one `_run_capture([...])` call site with an INLINE "
        "list-literal argv in src/partgraph/util/lifecycle.py — the module "
        "docstring's claim about this shape existing would otherwise be "
        "stale prose, not a verified fact."
    )
    assert variable_shape_found, (
        "expected at least one `_run_capture(argv, ...)` call site where argv "
        "was assigned in an earlier, separate statement in "
        "src/partgraph/util/lifecycle.py — the module docstring's claim "
        "about this shape existing would otherwise be stale prose, not a "
        "verified fact."
    )


# ---------------------------------------------------------------------------
# Secondary, lighter-weight sweep over the repo's non-Python tracked source
# (shell scripts, systemd unit files) — a simple comment-stripped substring
# scan, since these are not Python and cannot be AST-parsed. Scoped narrowly
# (scripts/ and systemd/ only): both directories are small, and grep-level
# confirmation (during test authoring) showed neither currently mentions any
# of the three forbidden terms at all.
# ---------------------------------------------------------------------------


def _strip_shell_style_comments(text: str) -> str:
    """Return *text* with everything from the first unquoted '#' on each
    line removed. A simple, non-shell-parsing heuristic — adequate for this
    repo's own small, plain scripts/unit files, not a general shell parser.
    """
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_no_tracked_shell_or_systemd_unit_file_mentions_forbidden_terms_outside_comments(
    repo_root: pathlib.Path,
) -> None:
    """AC-B2: Given the repo's other tracked "source" that could in
    principle execute something (``scripts/*.sh``, ``systemd/*.service``,
    ``systemd/*.timer`` — the ``partgraph-refresh-all`` scheduling files, an
    UNRELATED feature to db-lifecycle, but tracked source text nonetheless).
    When each is scanned with '#'-comments stripped.
    Then none of the three forbidden terms appears at all.
    """
    candidates: list[pathlib.Path] = []
    scripts_dir = repo_root / "scripts"
    systemd_dir = repo_root / "systemd"
    if scripts_dir.is_dir():
        candidates.extend(sorted(scripts_dir.glob("*.sh")))
    if systemd_dir.is_dir():
        candidates.extend(sorted(systemd_dir.iterdir()))

    offenders: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        stripped = _strip_shell_style_comments(path.read_text(encoding="utf-8")).lower()
        for term in FORBIDDEN_TERMS:
            if term in stripped:
                offenders.append(f"{path.relative_to(repo_root)}: contains {term!r} outside a comment")

    assert not offenders, "\n".join(offenders)
