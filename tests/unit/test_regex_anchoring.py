"""
Tests: source-level meta-scan — no regex literal in ``src/`` is ``$``-anchored
where end-of-string is meant.

Why this exists (Gate 5 finding, "something bigger" than the per-pattern
fix): PR-A's own security-hardening commit introduced
``partgraph.util.lifecycle._IDENTIFIER_GRAMMAR = re.compile(r"^[a-zA-Z0-9]"
r"[a-zA-Z0-9_.-]*$")``, used through ``.match()``. In Python, an UNANCHORED-
at-the-end ``$`` matches at end of string **or just before a single trailing
newline** — so ``_accepted_identifier("partgraph-dgraph\\n")`` returned the
value verbatim (ACCEPTED) rather than ``None``. That defect survived three
security reviews because the test suite's own hostile-newline control put
the newline in the MIDDLE of the string (``"bad\\nname"``), which never
exercises the quirk at all — a control that looked protective and was not,
the same species as the previously-deleted ``_which_nothing_present`` dead
fixture and the ``_SAFE_IDENTIFIER_RE`` shadow copy this same review round
also removed (``tests/unit/test_jlcparts_adapter.py``).

Once the reviewer reverted the implementer's ``\\Z`` fix back to ``$`` ONE
PATTERN AT A TIME (via a plugin patching module globals, never the tracked
tree) and ran the full suite after each reversion, only
``lifecycle._IDENTIFIER_GRAMMAR`` produced a failure. The other NINE swept
patterns — ``lifecycle._HOST_PORT_GRAMMAR``, ``jlcparts._SAFE_IDENTIFIER_RE``,
``links._UID_SAFE_RE``, ``dql_builder._PACKAGE_VALID_RE``,
``parser._PACKAGE_VALID_RE``, ``parser._NUMERIC_PACKAGE_RE``, ``cli._UID_RE``,
``cli._REFRESH_UID_RE``, and ``cli._REFRESH_STOCK_UID_RE`` — including the one
guarding SQL identifier interpolation and three guarding DQL ``after:``
cursor clauses, had NO regression coverage at all. A new allow-list, a
merge-conflict resolution, or an unrelated refactor could silently restore
``$`` to any of them (``$`` is the idiomatic anchor; it is what all ten
originally used) and the defect that survived three reviews would be back
with a clean suite and clean ruff.

Nine PER-PATTERN negative tests would only ever defend the nine patterns that
exist TODAY. This file is the general fix: one AST-level meta-test scanning
every static regex-pattern literal in ``src/`` for an unescaped, un-classed
``$`` — the ONE property that is always wrong for a Python ``re`` pattern
compiled/matched without ``re.MULTILINE`` (verified: grep found zero
``re.MULTILINE``/``re.M`` anywhere in ``src/``) when the pattern is meant to
anchor at end-of-string. This covers all ten patterns above AND every regex
this repository adds in the future — the property per-pattern tests can never
provide.

Mirrors this repository's own established idiom for a mechanical, AST-based
repo-wide scan: ``tests/unit/test_lifecycle_architecture.py`` (regex/AST
source scan), ``tests/unit/test_autostart_hermeticity.py`` (AST scan for an
autouse fixture) and, most directly,
``tests/unit/test_repo_never_executes_lifecycle_mutations.py`` (AST scan with
positive/negative self-test controls proving the scanner has real teeth
BEFORE it is trusted against the real tree — GATE 3A BLOCKING 1 there found a
scanner that was "verified, not merely believed, to catch NOTHING real";
this file's self-test section exists for the identical reason).

HANDLING THE LEGITIMATE EXCEPTIONS PROPERLY, NOT BY ALLOWLISTING NAMES: this
scan's rule is narrower than "every pattern must be anchored" — it flags ONLY
an unescaped ``$`` actually present in a pattern's text. Verified directly
against the real tree while writing this file: ``src/`` holds exactly 35
regex-pattern literals passed to a module-level ``re.compile``/``re.match``/
``re.fullmatch``/``re.search``/``re.findall``/``re.finditer``/``re.sub``/
``re.subn``/``re.split`` call, every one a plain string ``Constant`` (zero
f-string, concatenated, or variable patterns — confirmed by
``test_every_regex_pattern_argument_in_src_is_a_static_string_literal``
below, which is what makes this Constant-only AST pass EXHAUSTIVE rather than
merely convenient), and zero of the 35 currently contain a ``$`` at all. The
three exception SHAPES the reviewer named never needed a carve-out by name,
because none of them contains a ``$`` in the first place, and this scan never
claims "must be anchored" — only "must not use ``$`` as the anchor":
  - ``.fullmatch()`` users (e.g. ``dql_builder.py``'s ``_FLOAT_LITERAL_RE``/
    ``_INT_LITERAL_RE``, patterns ``"[0-9.eE+-]+"``/``"[0-9]+"``) are immune
    structurally: ``.fullmatch()`` anchors both ends itself, so these
    patterns were never written with ``^``/``$``/``\\Z`` at all.
  - ``_LEADING_NUMBER_RE`` (``normalize/units.py`` AND ``query/parser.py``,
    identical: ``r"^[+-]?\\d+(?:\\.\\d+)?"``) is a genuine PREFIX matcher —
    ``.match()`` is called and ``match.end()`` is used to split the
    remainder (verified: both real call sites do exactly this). It is
    deliberately unanchored at the end and carries no ``$``/``\\Z`` either.
  - ``dql_builder.py``'s ``_related_prefix()`` (line ~830,
    ``re.match(r"[A-Z]+", upper)``) extracts a leading letter run bound as a
    Dgraph ``$``-variable, never inlined — also carries no ``$``/``\\Z``.
Negative self-test controls below reproduce all three shapes directly
(embedded snippets, never the real tree) to prove the scanner does not fire
on any of them, alongside controls for an escaped literal dollar sign and a
``$`` inside a character class (neither of which exists in ``src/`` today,
but both are real, general ways a legitimate ``$`` could appear in a pattern
without being the anchor metacharacter, and the scanner must not fire on
either).
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
from dataclasses import dataclass

import pytest

# ---------------------------------------------------------------------------
# The scanner itself (test-only tooling — not a production feature; mirrors
# tests/unit/test_repo_never_executes_lifecycle_mutations.py's own embedded
# AST scanner convention).
# ---------------------------------------------------------------------------

#: ``re`` module-level functions whose FIRST positional argument is a regex
#: pattern (as opposed to a compiled ``Pattern`` object's OWN methods of the
#: same names, e.g. ``_SOME_RE.match(text)`` — there the pattern is not an
#: argument at all, it is baked into the object from its own ``re.compile()``
#: call elsewhere, which this scan reaches separately). Restricting to these
#: module-level calls is what keeps this scan from mistaking an unrelated
#: ``str.split(",")`` or a compiled pattern's own ``.sub(repl, text)`` (whose
#: first argument is a REPLACEMENT string, never a pattern) for a regex
#: literal declaration.
_RE_PATTERN_FUNCS = frozenset({
    "compile", "match", "fullmatch", "search", "findall", "finditer",
    "sub", "subn", "split",
})


@dataclass(frozen=True)
class _RegexLiteral:
    """One ``re.<func>(pattern, ...)`` call site found by the scanner.

    Attributes:
        lineno: Source line of the call.
        func_name: The ``re.<func_name>`` that was called (e.g. ``"compile"``).
        pattern: The pattern string, or ``""`` when ``is_dynamic`` is True (the
            real value is then unknowable from the AST alone).
        is_dynamic: True iff the pattern argument was NOT a plain string
            ``Constant`` — an f-string, a concatenation, or a variable. This
            scan's ``$``-anchor check can only ever inspect ``Constant``
            patterns; a dynamic one is reported separately, never silently
            skipped (see
            ``test_every_regex_pattern_argument_in_src_is_a_static_string_literal``).
    """

    lineno: int
    func_name: str
    pattern: str
    is_dynamic: bool


def _re_module_aliases(tree: ast.Module) -> frozenset[str]:
    """Return every local name this file's own imports bind to the ``re``
    module — ordinarily just ``{"re"}``, but resolved properly (not
    hard-coded) so an ``import re as _re`` would still be recognised rather
    than silently missed.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "re":
                    aliases.add(alias.asname or alias.name)
    return frozenset(aliases)


def _iter_regex_literals(tree: ast.Module) -> list[_RegexLiteral]:
    """Return every module-level ``re.<func>(pattern, ...)`` call in *tree*.

    Deliberately restricted to calls whose callee's OWN base resolves to the
    ``re`` module (via :func:`_re_module_aliases`) — never a bare method call
    on some other object of the same name, which is exactly the false-positive
    ``str.split(",")`` / compiled-pattern ``.sub(repl, text)`` shape a naive
    "any ``.compile``/``.match``/... attribute" scan would wrongly count as a
    fresh regex-pattern declaration.
    """
    aliases = _re_module_aliases(tree)
    literals: list[_RegexLiteral] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in _RE_PATTERN_FUNCS
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases
        ):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            literals.append(_RegexLiteral(node.lineno, func.attr, first.value, is_dynamic=False))
        else:
            literals.append(_RegexLiteral(node.lineno, func.attr, "", is_dynamic=True))
    return literals


def _is_dollar_anchored(pattern: str) -> bool:
    """Return True iff *pattern* contains an unescaped ``$`` OUTSIDE a
    character class — the anchor metacharacter Python's ``re`` treats as "end
    of string, OR just before a single trailing newline" without
    ``re.MULTILINE`` (verified: no pattern in ``src/`` passes that flag).

    A ``$`` is NOT flagged when it is:
      - escaped (``\\$``, a literal dollar sign — an ODD number of
        immediately preceding backslashes), or
      - inside a character class (``[...]``), where ``$`` has no special
        meaning at all and matches a literal dollar sign without needing to
        be escaped.

    This is a purpose-built scanner over regex syntax, not a general regex
    parser: it tracks only backslash-escaping and character-class nesting,
    the two things that change what a literal ``$`` character means. It does
    not need to understand alternation, groups, or any other regex
    construct, because ``$`` means "anchor" in ALL of those contexts too —
    the only two things that change its meaning are escaping and character
    classes.
    """
    in_class = False
    backslash_run = 0
    for ch in pattern:
        if ch == "\\":
            backslash_run += 1
            continue
        escaped = backslash_run % 2 == 1
        backslash_run = 0
        if ch == "[" and not in_class and not escaped:
            in_class = True
        elif ch == "]" and in_class and not escaped:
            in_class = False
        elif ch == "$" and not in_class and not escaped:
            return True
    return False


# ---------------------------------------------------------------------------
# Self-test: prove the scanner has real teeth (positive controls) and does
# not false-positive on the genuinely intentional shapes (negative controls)
# BEFORE trusting it to scan the real tree. Every snippet below is embedded
# source text, only ever passed to ast.parse() in-memory — none of it is
# written to disk or executed.
# ---------------------------------------------------------------------------


def _literals_in_source(source: str) -> list[_RegexLiteral]:
    """Parse *source* and return every regex literal :func:`_iter_regex_literals`
    finds in it — the single seam every self-test control below drives.
    """
    return _iter_regex_literals(ast.parse(source, filename="canary.py"))


def test_scanner_flags_a_reintroduced_dollar_anchored_compile_pattern() -> None:
    """[Positive control — the exact regression this file exists to catch]
    Given a snippet reintroducing ``re.compile(r"^[a-zA-Z0-9_.-]*$")`` —
    ``lifecycle._IDENTIFIER_GRAMMAR`` restored to its pre-fix, vulnerable form.
    When the scanner runs.
    Then it reports the literal as dollar-anchored.
    """
    bad = 'import re\n\n_RE = re.compile(r"^[a-zA-Z0-9_.-]*$")\n'
    literals = _literals_in_source(bad)
    assert len(literals) == 1, literals
    assert not literals[0].is_dynamic
    assert _is_dollar_anchored(literals[0].pattern), (
        f"scanner failed to flag a reintroduced $-anchor: {literals[0]!r}"
    )


def test_scanner_flags_a_dollar_anchor_passed_directly_to_re_match() -> None:
    """[Positive control — the inline-call shape, never pre-compiled] Given
    a pattern passed directly to a module-level ``re.match(...)`` call
    (never ``re.compile()`` first) that ends in an unanchored ``$``.
    When the scanner runs.
    Then it still reports the literal as dollar-anchored — the scan is not
    ``re.compile``-only.
    """
    bad = 'import re\n\ndef f(name):\n    return re.match(r"^foo$", name)\n'
    literals = _literals_in_source(bad)
    assert len(literals) == 1, literals
    assert literals[0].func_name == "match"
    assert _is_dollar_anchored(literals[0].pattern)


def test_scanner_flags_a_dollar_anchor_reached_via_an_import_alias() -> None:
    """[Positive control — cheap defence-in-depth, mirrors
    test_repo_never_executes_lifecycle_mutations.py's identical control for
    its own dangerous-call scan] Given ``import re as _re`` and a call site
    written ``_re.compile(...)`` — the bound name at the call site is the
    ALIAS, never the literal string ``"re"``.
    When the scanner runs.
    Then it still reports the literal as dollar-anchored: an import alias of
    the ``re`` module is itself recognised.
    """
    bad = 'import re as _re\n\n_RE = _re.compile(r"^foo$")\n'
    literals = _literals_in_source(bad)
    assert len(literals) == 1, literals
    assert _is_dollar_anchored(literals[0].pattern)


def test_scanner_does_not_flag_a_backslash_Z_anchored_pattern() -> None:
    """[Negative control — the fix's own shape] Given the implementer's
    actual fix: ``\\Z`` (end of string only, never before a trailing
    newline) instead of ``$``.
    When the scanner runs.
    Then it reports the literal as present but NOT dollar-anchored.
    """
    good = r'import re' + "\n\n" + r'_RE = re.compile(r"^[a-zA-Z0-9_.-]*\Z")' + "\n"
    literals = _literals_in_source(good)
    assert len(literals) == 1, literals
    assert not _is_dollar_anchored(literals[0].pattern), (
        f"a \\Z-anchored pattern must never be flagged: {literals[0]!r}"
    )


def test_scanner_does_not_flag_an_unanchored_prefix_matcher() -> None:
    """[Negative control — reproduces ``_LEADING_NUMBER_RE`` exactly, the
    genuine "deliberate prefix matcher" the review named] Given
    ``r"^[+-]?\\d+(?:\\.\\d+)?"`` — no ``$``/``\\Z`` at all, by design: the
    real code calls ``.match()`` and uses ``match.end()`` to split the
    remainder, which requires the pattern to stop wherever the number ends,
    never to consume the whole string.
    When the scanner runs.
    Then it reports the literal as present but NOT dollar-anchored — this
    scan never demands "every pattern is anchored", only "no pattern uses
    ``$`` as the anchor", so a genuinely, deliberately unanchored pattern is
    never mistaken for a regression.
    """
    good = r'import re' + "\n\n" + r'_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?")' + "\n"
    literals = _literals_in_source(good)
    assert len(literals) == 1, literals
    assert not _is_dollar_anchored(literals[0].pattern)


def test_scanner_does_not_flag_the_prefix_extraction_shape_bound_as_a_query_variable() -> None:
    """[Negative control — reproduces ``dql_builder.py``'s ``_related_prefix()``
    exactly] Given ``re.match(r"[A-Z]+", upper)`` — a direct module-level
    ``re.match()`` call (never pre-compiled) extracting a leading letter run,
    bound as a query variable rather than inlined.
    When the scanner runs.
    Then it reports the literal as present but NOT dollar-anchored.
    """
    good = (
        "import re\n\n"
        "def _related_prefix(upper):\n"
        '    letters = re.match(r"[A-Z]+", upper)\n'
        "    return letters.group(0) if letters else upper[:3]\n"
    )
    literals = _literals_in_source(good)
    assert len(literals) == 1, literals
    assert not _is_dollar_anchored(literals[0].pattern)


def test_scanner_does_not_flag_fullmatch_users_with_no_anchor_at_all() -> None:
    """[Negative control — reproduces ``_FLOAT_LITERAL_RE``/``_INT_LITERAL_RE``]
    Given a bare charset pattern (``"[0-9.eE+-]+"``) with no ``^``/``$``/
    ``\\Z`` at all, because the real code calls ``.fullmatch()`` on it — which
    anchors both ends itself.
    When the scanner runs.
    Then it reports the literal as present but NOT dollar-anchored.
    """
    good = 'import re\n\n_RE = re.compile(r"[0-9.eE+-]+")\n'
    literals = _literals_in_source(good)
    assert len(literals) == 1, literals
    assert not _is_dollar_anchored(literals[0].pattern)


def test_scanner_does_not_flag_an_escaped_literal_dollar_sign() -> None:
    """[Negative control — a real, general exception, even though nothing in
    ``src/`` currently needs it] Given a pattern matching a literal price
    sign, ``r"\\$\\d+(?:\\.\\d+)?"`` — the ``$`` is ESCAPED, so it is a literal
    character to match, never the end-of-string anchor.
    When the scanner runs.
    Then it reports the literal as present but NOT dollar-anchored — the scan
    distinguishes anchor-``$`` from literal-``\\$``.
    """
    good = r'import re' + "\n\n" + r'_RE = re.compile(r"\$\d+(?:\.\d+)?")' + "\n"
    literals = _literals_in_source(good)
    assert len(literals) == 1, literals
    assert not _is_dollar_anchored(literals[0].pattern)


def test_scanner_does_not_flag_a_dollar_sign_inside_a_character_class() -> None:
    """[Negative control — a real, general exception, even though nothing in
    ``src/`` currently needs it] Given ``r"[$]"`` — inside a character class,
    ``$`` has no special meaning at all and matches a literal dollar sign
    WITHOUT needing to be escaped.
    When the scanner runs.
    Then it reports the literal as present but NOT dollar-anchored.
    """
    good = 'import re\n\n_RE = re.compile(r"[$]")\n'
    literals = _literals_in_source(good)
    assert len(literals) == 1, literals
    assert not _is_dollar_anchored(literals[0].pattern)


def test_scanner_does_not_confuse_a_plain_string_split_for_a_regex_literal() -> None:
    """[Negative control — the false-positive this scan's ``re``-alias
    restriction exists to avoid] Given ``"a,b".split(",")`` — a plain
    ``str.split()`` call, which happens to share a name
    (``"split"`` is in ``_RE_PATTERN_FUNCS``) with ``re.split()`` but is
    called on a plain string, never on the ``re`` module or an aliased name
    of it.
    When the scanner runs.
    Then it reports ZERO regex literals — the comma is not a regex pattern.
    """
    good = 'value = "a,b".split(",")\n'
    assert _literals_in_source(good) == []


def test_scanner_flags_a_dynamic_pattern_as_dynamic_never_as_a_missing_dollar() -> None:
    """[Self-test of the exhaustiveness guard] Given a pattern built from an
    f-string (``f"^{prefix}$"``) — this scan's ``$``-anchor check can only
    ever inspect a plain string ``Constant``.
    When the scanner runs.
    Then the literal is reported ``is_dynamic=True`` with an empty
    ``pattern`` — never silently coerced into "no ``$`` found, therefore
    safe". A dynamic pattern is a DIFFERENT finding
    (``test_every_regex_pattern_argument_in_src_is_a_static_string_literal``
    below), never a false "clean".
    """
    bad = 'import re\n\ndef f(prefix):\n    return re.compile(f"^{prefix}$")\n'
    literals = _literals_in_source(bad)
    assert len(literals) == 1, literals
    assert literals[0].is_dynamic is True
    assert literals[0].pattern == ""


# ---------------------------------------------------------------------------
# The real scan: every tracked *.py file under src/.
# ---------------------------------------------------------------------------


def _tracked_python_files_under_src(repo_root: pathlib.Path) -> list[pathlib.Path]:
    """Return every tracked ``*.py`` file under ``src/`` via ``git ls-files``,
    or a plain glob fallback when git is unavailable (mirrors
    ``tests/unit/test_repo_never_executes_lifecycle_mutations.py``'s own
    identical helper — kept as a local copy per CONTRIBUTING.md's "test
    fixtures stay local to their file" policy).
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


def _all_real_literals(repo_root: pathlib.Path) -> list[tuple[str, _RegexLiteral]]:
    """Return ``(relative_path, literal)`` for every regex literal found
    across every tracked ``*.py`` file under ``src/``.
    """
    found: list[tuple[str, _RegexLiteral]] = []
    for path in _tracked_python_files_under_src(repo_root):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        label = str(path.relative_to(repo_root))
        found.extend((label, literal) for literal in _iter_regex_literals(tree))
    return found


def test_no_regex_literal_in_src_is_dollar_anchored(repo_root: pathlib.Path) -> None:
    """BLOCKING (Gate 5): Given every tracked ``*.py`` file under ``src/``.
    When every module-level ``re.compile``/``re.match``/``re.fullmatch``/
    ``re.search``/``re.findall``/``re.finditer``/``re.sub``/``re.subn``/
    ``re.split`` call's static string pattern is scanned.
    Then NONE of them contains an unescaped, un-classed ``$`` — currently OR
    after any future allow-list, merge-conflict resolution, or refactor that
    might otherwise silently restore the idiomatic-but-vulnerable ``$``
    anchor to any of them.

    This is the ONE test that would have failed for
    ``lifecycle._IDENTIFIER_GRAMMAR`` before PR-A's fix, and is the ONLY
    coverage (before this file existed) for the other nine patterns the
    reviewer's revert-and-run sweep found completely undefended:
    ``lifecycle._HOST_PORT_GRAMMAR``, ``jlcparts._SAFE_IDENTIFIER_RE``,
    ``links._UID_SAFE_RE``, ``dql_builder._PACKAGE_VALID_RE``,
    ``parser._PACKAGE_VALID_RE``, ``parser._NUMERIC_PACKAGE_RE``,
    ``cli._UID_RE``, ``cli._REFRESH_UID_RE``, ``cli._REFRESH_STOCK_UID_RE``.
    """
    static_literals = [
        (label, literal) for label, literal in _all_real_literals(repo_root)
        if not literal.is_dynamic
    ]
    dollar_anchored = [
        f"{label}:{literal.lineno} (re.{literal.func_name}) pattern={literal.pattern!r}"
        for label, literal in static_literals
        if _is_dollar_anchored(literal.pattern)
    ]
    assert dollar_anchored == [], (
        "regex pattern(s) anchored with a bare '$' found in src/ — in Python, "
        "an unanchored-at-the-end '$' also matches just before a trailing "
        "newline (without re.MULTILINE, which nothing in src/ uses), so a "
        "value ending in exactly one '\\n' slips past a "
        "'^...$'-anchored .match()/.fullmatch() even though the pattern's "
        "own charset never lists a newline as accepted. Use '\\Z' (end of "
        "string only) instead:\n" + "\n".join(dollar_anchored)
    )
    # A scan that silently matched almost nothing would trivially "pass"
    # without having scanned anything real — GATE 3A BLOCKING 1's own lesson
    # in test_repo_never_executes_lifecycle_mutations.py ("verified, not
    # merely believed, to catch NOTHING real"), applied here. 30 is a loose
    # floor, deliberately not an exact-equality pin: src/ held 35 regex
    # literals when this test was written, and legitimate future patterns
    # should never fail this test for a reason unrelated to '$'-anchoring.
    assert len(static_literals) >= 30, (
        f"only found {len(static_literals)} static regex pattern literal(s) "
        "in src/ — expected at least 30; either src/ genuinely lost most of "
        "its regex patterns, or this scan's own AST walk has regressed and "
        "is silently matching almost nothing."
    )


def test_every_regex_pattern_argument_in_src_is_a_static_string_literal(
    repo_root: pathlib.Path,
) -> None:
    """[Exhaustiveness guard] Given every tracked ``*.py`` file under ``src/``.
    When every module-level ``re.<func>(...)`` call's pattern argument is
    inspected.
    Then EVERY one is a plain string ``Constant`` — zero f-string,
    concatenated, or variable patterns anywhere.

    This is what makes ``test_no_regex_literal_in_src_is_dollar_anchored``'s
    scan EXHAUSTIVE rather than merely convenient: that scan can only ever
    read a ``Constant`` pattern's text. If a dynamic pattern is ever
    introduced, this test fails LOUDLY here — the scan's own coverage would
    otherwise silently narrow around it with no signal at all, which is
    precisely the failure mode a Constant-only AST pass must never produce
    without saying so.
    """
    dynamic = [
        f"{label}:{literal.lineno} (re.{literal.func_name})"
        for label, literal in _all_real_literals(repo_root)
        if literal.is_dynamic
    ]
    assert dynamic == [], (
        "found a DYNAMIC regex pattern argument (an f-string, a "
        "concatenation, or a variable) passed to a re.* call in src/ — "
        "test_no_regex_literal_in_src_is_dollar_anchored's scan is "
        "Constant-only and CANNOT inspect a dynamic pattern's actual text, "
        "so its '$'-anchoring guarantee silently narrows the moment one "
        "exists. Review the pattern(s) below by hand for the identical "
        "trailing-newline quirk, then extend this scanner to cover them "
        "explicitly — never leave them silently unexamined:\n"
        + "\n".join(dynamic)
    )


@pytest.mark.parametrize(
    "swept_pattern_source",
    [
        pytest.param(
            "src/partgraph/util/lifecycle.py::_HOST_PORT_GRAMMAR", id="lifecycle-host-port"
        ),
        pytest.param(
            "src/partgraph/sources/jlcparts.py::_SAFE_IDENTIFIER_RE", id="jlcparts-identifier"
        ),
        pytest.param("src/partgraph/refresh/links.py::_UID_SAFE_RE", id="links-uid"),
        pytest.param(
            "src/partgraph/query/dql_builder.py::_PACKAGE_VALID_RE", id="dql-builder-package"
        ),
        pytest.param("src/partgraph/query/parser.py::_PACKAGE_VALID_RE", id="parser-package"),
        pytest.param(
            "src/partgraph/query/parser.py::_NUMERIC_PACKAGE_RE", id="parser-numeric-package"
        ),
        pytest.param("src/partgraph/cli.py::_UID_RE", id="cli-uid"),
        pytest.param("src/partgraph/cli.py::_REFRESH_UID_RE", id="cli-refresh-uid"),
        pytest.param("src/partgraph/cli.py::_REFRESH_STOCK_UID_RE", id="cli-refresh-stock-uid"),
    ],
)
def test_each_swept_pattern_still_exists_and_is_covered_by_the_scan(
    repo_root: pathlib.Path, swept_pattern_source: str,
) -> None:
    """[Regression-review guard — proves the nine "no coverage" patterns from
    the reviewer's revert-and-run sweep are not merely ASSUMED to be caught by
    the general scan above, but genuinely still exist in the tree and were
    genuinely reached by it]
    Given one of the nine file/name pairs the reviewer's sweep exercised (a
    tenth, ``lifecycle._IDENTIFIER_GRAMMAR``, is pinned by this file's own
    docstring reference and by the pre-existing per-pattern negative tests in
    ``tests/unit/test_lifecycle.py``).
    When the named file's source is parsed for a module-level assignment
    ``<NAME> = re.compile(...)`` at that qualified name.
    Then the assignment exists, its pattern is a static string ``Constant``,
    and it is NOT dollar-anchored — proving the general scan's non-empty
    result set genuinely includes this exact pattern rather than the scan
    having silently missed the whole file.
    """
    rel_path, name = swept_pattern_source.split("::")
    path = repo_root / rel_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    found: ast.Constant | None = None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "compile"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            found = value.args[0]
            break

    assert found is not None, (
        f"expected a module-level '{name} = re.compile(<string literal>)' "
        f"assignment in {rel_path} — it may have been renamed, removed, or "
        f"restructured; update this control to match."
    )
    assert not _is_dollar_anchored(found.value), (
        f"{rel_path}::{name} is dollar-anchored ({found.value!r}) — this "
        "should already have been caught by "
        "test_no_regex_literal_in_src_is_dollar_anchored above; if THIS "
        "assertion is what caught it instead, that test's own scan has a gap."
    )
