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
ANYWHERE in `src/` **except** inside the argument list of a call this
process would itself EXECUTE or a filesystem WRITE this process would itself
PERFORM — subprocess/process-exec calls (`subprocess.run`/`Popen`/`call`/
`check_call`/`check_output`, `os.system`/`os.popen`/`os.exec*`/`os.spawn*`)
and file-mutating calls (`open`/`write_text`/`write_bytes`/`replace`/
`rename`/`remove`/`unlink`/`rmtree`/`move`/`copy*`/`mkdir`/`makedirs`/
`rmdir`/`symlink`/`chmod`/`truncate`). Everything else — a docstring, a `#`
comment (never even reaches the AST, so it is automatically exempt), a
`console.print(...)` remediation message, a module-level constant used only
for display — is legitimate prose and is deliberately left alone. This is
also EXACTLY the "concrete example" this file's own author was asked to
defend against: "genuinely fail if someone later adds a
`subprocess.run([... "daemon-reload"])`" — that shape (a forbidden literal
inside a recognised dangerous call's own argument subtree, including nested
list/f-string literals) is precisely what `_find_violations` below detects,
and the self-test section proves it does, with both a positive control (must
flag) and negative controls (must NOT flag: docstring, comment, print/display
text).

HONEST SCOPE LIMITATION (documented, not hidden — this file's own author
must not overclaim): the scanner inspects only the ARGUMENT SUBTREE of each
recognised dangerous call. It does NOT perform cross-statement dataflow
analysis, so `argv = [engine, "daemon-reload"]; subprocess.run(argv)` (the
forbidden literal built in one statement, then passed to the dangerous call
via a bare variable in a LATER statement) would NOT be caught. This is a
known, disclosed gap, not a claim of exhaustive proof — it directly catches
the concrete evasion shape this AC names, and the repo's existing style
(inline list-literal argv, e.g. every `subprocess.run([...])` call already in
`src/partgraph/util/lifecycle.py` and `src/partgraph/cli.py`) makes the
indirect form both unidiomatic here and, if it ever appeared, a strong
independent signal worth a human's attention regardless of this scanner.
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
    """Return the call's own name: the last dotted attribute, or a bare name."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _string_fragments(node: ast.AST) -> list[str]:
    """Collect every literal string fragment anywhere inside *node*'s subtree.

    ``ast.walk`` recurses into EVERY descendant regardless of nesting depth,
    so this reaches string literals inside a nested list literal
    (``["a", "b"]``) and inside an f-string's literal segments (each
    non-interpolated chunk of an ``ast.JoinedStr`` is its own ``ast.Constant``
    child) without any special-casing.
    """
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _find_violations(source: str, label: str) -> list[str]:
    """Return one message per forbidden term found inside a dangerous call's
    subtree in *source* (labelled *label* for the message).

    Scans the WHOLE matched ``Call`` node — including ``node.func`` — not
    just ``node.args``/``node.keywords``. This is REQUIRED for a common,
    realistic pattern this scanner must not miss:
    ``Path("...containers/systemd...").write_text("x")``. There, the
    dangerous verb is ``write_text``, but the forbidden path string lives
    inside the *callee* expression's own nested ``Path(...)`` call — i.e.
    inside ``node.func.value``, not inside ``node.args``. Including
    ``node.func`` in the scan is SAFE: a call's own name (e.g. ``"run"``,
    ``"write_text"``) is an ``ast.Name``/``ast.Attribute`` node, never an
    ``ast.Constant`` string, so it can never be picked up by
    :func:`_string_fragments` regardless. (An earlier version of this
    scanner excluded ``node.func`` on exactly that "the call name itself
    could never match" reasoning, but then missed this call chain entirely —
    the reasoning was right, the exclusion was an unrelated and unnecessary
    scope-narrowing that cost real coverage; a positive-control self-test
    below pins this exact chained-call shape so a regression here is
    caught immediately, not just asserted away in prose.)
    """
    tree = ast.parse(source, filename=label)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name not in DANGEROUS_CALL_NAMES:
            continue
        pieces = _string_fragments(node)
        for piece in pieces:
            lowered = piece.lower()
            for term in FORBIDDEN_TERMS:
                if term in lowered:
                    violations.append(
                        f"{label}:{node.lineno}: forbidden term {term!r} found inside "
                        f"an argument to {name!r}(): {piece!r}"
                    )
    return violations


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
