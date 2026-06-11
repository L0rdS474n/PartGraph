"""
Tests: R1, R3, AC-PRIV

R1  — required repository skeleton files must exist and be non-empty.
R3  — LICENSE and README.md contain the required legal and attribution text.
AC-PRIV — no tracked/staged text file may contain operator home-directory paths.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Paths allowed to contain substrings that look like home dirs but are clearly
# generic placeholders.
_HOME_PATH_PATTERN = re.compile(
    r"(/home/(?!(?:user|operator|dev|test|example|you|me|username|admin|vagrant)/)[^/\s\"']+/"
    r"|/Users/(?!(?:user|operator|dev|test|example|you|me|username|admin|vagrant)/)[^/\s\"']+/"
    r"|C:\\Users\\(?!(?:user|operator|dev|test|example|you|me|username|admin|vagrant)\\)[^\\\s\"']+)"
)


def _file_is_text(path: pathlib.Path) -> bool:
    """Heuristic: treat files with no null bytes in first 8 KB as text."""
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" not in chunk
    except OSError:
        return False


# ---------------------------------------------------------------------------
# R1 — required files exist and are non-empty
# ---------------------------------------------------------------------------

REQUIRED_FILES = [
    "src/partgraph/__init__.py",
    "src/partgraph/cli.py",
    "schema/partgraph.dql",
    "docker/docker-compose.yml",
    "tests/",               # directory — checked separately
    "docs/superpowers/specs/2026-06-11-partgraph-design.md",
    ".github/workflows/ci.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "CODEOWNERS",
    "CONTRIBUTING.md",
    "README.md",
    ".gitignore",
    "LICENSE",
    "pyproject.toml",
    "environment.yml",
    ".env.example",         # placeholder env — must exist, non-empty, no real secrets
]

# Files that must be non-empty (directories excluded).
REQUIRED_NONEMPTY_FILES = [f for f in REQUIRED_FILES if not f.endswith("/")]


@pytest.mark.parametrize("rel_path", REQUIRED_NONEMPTY_FILES)
def test_required_file_exists_and_nonempty(repo_root: pathlib.Path, rel_path: str) -> None:
    """Given the repo skeleton has been set up.
    When we check each required file path.
    Then the file must exist and contain at least one byte of content.
    """
    full = repo_root / rel_path
    assert full.exists(), f"Required file missing: {rel_path}"
    assert full.is_file(), f"Expected a file, found something else: {rel_path}"
    assert full.stat().st_size > 0, f"Required file is empty: {rel_path}"


def test_required_tests_directory_exists(repo_root: pathlib.Path) -> None:
    """Given the repo skeleton has been set up.
    When we look for the tests/ directory.
    Then it must exist as a directory.
    """
    tests_dir = repo_root / "tests"
    assert tests_dir.exists(), "tests/ directory is missing"
    assert tests_dir.is_dir(), "tests/ is not a directory"


def test_env_example_contains_only_placeholder_values(repo_root: pathlib.Path) -> None:
    """Given .env.example exists and is non-empty.
    When we read its contents.
    Then every value assignment must use an obvious placeholder (angle-bracket
    token, the literal word 'changeme', 'your_*', 'replace_*', or a blank
    right-hand side) — never a real secret or real path.

    Specifically, the file must NOT contain:
    - Password/token strings longer than 16 chars that look random (all hex or
      base64-ish) — heuristic: 20+ consecutive [A-Za-z0-9+/=_-] chars on a
      value line.
    - Absolute paths that contain a real home directory segment (delegated to
      _HOME_PATH_PATTERN).
    """
    env_example = repo_root / ".env.example"
    assert env_example.exists(), ".env.example is missing from the repository root."
    text = env_example.read_text(encoding="utf-8")
    assert text.strip(), ".env.example must not be empty — it must contain placeholder variable declarations."

    import re as _re  # already imported at module level; alias for clarity
    # Detect suspiciously long opaque token values (heuristic for leaked secrets).
    long_token = _re.compile(r'=\s*[A-Za-z0-9+/=_\-]{20,}')
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        # Skip lines where the value looks like an explicit placeholder.
        _, _, value_part = stripped.partition("=")
        value = value_part.strip()
        if (
            not value                                    # blank value
            or value.startswith("<")                     # <PLACEHOLDER>
            or value.lower().startswith("your")          # your_token_here
            or value.lower().startswith("replace")       # replace_me
            or "changeme" in value.lower()
            or value.lower().startswith("example")
            or value.lower() in {"true", "false", "0", "1", "localhost", "127.0.0.1"}
        ):
            continue
        assert not long_token.search(stripped), (
            f".env.example line {lineno} looks like a real secret (long opaque token): "
            f"{stripped!r}\n"
            "Use placeholder values such as <YOUR_TOKEN_HERE> or 'changeme'."
        )
        assert not _HOME_PATH_PATTERN.search(value), (
            f".env.example line {lineno} contains a real home-directory path: {stripped!r}"
        )


def test_windows_home_path_pattern_self_test() -> None:
    """Self-test: verify the Windows branch of _HOME_PATH_PATTERN is compiled
    correctly and matches/rejects the expected inputs.

    Given the compiled _HOME_PATH_PATTERN regex.
    When we test it against real-name and placeholder-name Windows paths.
    Then it must match a Windows home path with a real username and NOT match
    one whose username is a generic placeholder.

    Note: Windows sample paths are assembled at runtime from ``chr(92)`` so that
    no contiguous ``<drive>:<bs>Users<bs><name><bs>`` literal ever appears as
    file text (which would trip the repository's private-data scanner).
    """
    sep = chr(92)  # backslash — kept out of any contiguous path literal

    def win_path(name: str) -> str:
        """Build ``C:\\Users\\<name>\\x`` without a contiguous source literal."""
        return f"C:{sep}Users{sep}{name}{sep}x"

    # Must match a real (non-placeholder) Windows home path.
    real_sample = win_path("realname")
    assert _HOME_PATH_PATTERN.search(real_sample), (
        f"_HOME_PATH_PATTERN must match {real_sample!r} (real username)."
    )
    # Must NOT match placeholder names.
    for placeholder in ("example", "user", "operator", "dev", "test",
                         "you", "me", "username", "admin", "vagrant"):
        path = win_path(placeholder)
        assert not _HOME_PATH_PATTERN.search(path), (
            f"_HOME_PATH_PATTERN must NOT match {path!r} (placeholder username)."
        )


def test_github_issue_templates_directory_has_at_least_one_template(
    repo_root: pathlib.Path,
) -> None:
    """Given the repo skeleton has been set up.
    When we inspect .github/ISSUE_TEMPLATE/.
    Then the directory must exist and contain at least one file.
    """
    tmpl_dir = repo_root / ".github" / "ISSUE_TEMPLATE"
    assert tmpl_dir.exists(), ".github/ISSUE_TEMPLATE/ directory is missing"
    assert tmpl_dir.is_dir(), ".github/ISSUE_TEMPLATE/ is not a directory"
    templates = list(tmpl_dir.iterdir())
    assert len(templates) >= 1, (
        ".github/ISSUE_TEMPLATE/ exists but contains no templates"
    )


# ---------------------------------------------------------------------------
# R3 — LICENSE content
# ---------------------------------------------------------------------------

def test_license_contains_mit(repo_root: pathlib.Path) -> None:
    """Given a LICENSE file exists.
    When we read its contents.
    Then the word "MIT" must appear.
    """
    lic = (repo_root / "LICENSE").read_text(encoding="utf-8")
    assert "MIT" in lic, "LICENSE does not contain 'MIT'"


def test_license_contains_author_handle(repo_root: pathlib.Path) -> None:
    """Given a LICENSE file exists.
    When we read its contents.
    Then 'L0rdS474n' must appear as the copyright holder.
    """
    lic = (repo_root / "LICENSE").read_text(encoding="utf-8")
    assert "L0rdS474n" in lic, "LICENSE does not contain 'L0rdS474n'"


def test_license_contains_year_2026(repo_root: pathlib.Path) -> None:
    """Given a LICENSE file exists.
    When we read its contents.
    Then '2026' must appear (copyright year).
    """
    lic = (repo_root / "LICENSE").read_text(encoding="utf-8")
    assert "2026" in lic, "LICENSE does not contain '2026'"


# ---------------------------------------------------------------------------
# R3 — README.md content
# ---------------------------------------------------------------------------

def test_readme_contains_mit_mention(repo_root: pathlib.Path) -> None:
    """Given a README.md file exists.
    When we read its contents.
    Then the text 'MIT' must appear (license mention).
    """
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    assert "MIT" in readme, "README.md does not mention MIT license"


def test_readme_contains_jlcparts_attribution(repo_root: pathlib.Path) -> None:
    """Given a README.md file exists.
    When we read its contents.
    Then 'jlcparts' must appear as a data attribution.
    """
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    assert "jlcparts" in readme, "README.md missing jlcparts attribution"


def test_readme_contains_kicad_cc_by_sa(repo_root: pathlib.Path) -> None:
    """Given a README.md file exists.
    When we read its contents.
    Then 'KiCad' and 'CC-BY-SA' (or 'CC BY-SA') must appear.
    """
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    assert "KiCad" in readme, "README.md missing KiCad attribution"
    has_cc = "CC-BY-SA" in readme or "CC BY-SA" in readme
    assert has_cc, "README.md missing CC-BY-SA 4.0 license mention for KiCad data"


def test_readme_contains_tme_attribution(repo_root: pathlib.Path) -> None:
    """Given a README.md file exists.
    When we read its contents.
    Then 'TME.eu' must appear (data provider attribution).
    """
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    assert "TME.eu" in readme, "README.md missing TME attribution ('powered by TME.eu Data')"


def test_readme_contains_data_not_redistributed_policy(repo_root: pathlib.Path) -> None:
    """Given a README.md file exists.
    When we read its contents.
    Then there must be a statement that data is NOT redistributed / must be
    built locally.  We check for the key concepts: a negation near 'redistribut'
    or phrasing about building locally.
    """
    readme = (repo_root / "README.md").read_text(encoding="utf-8").lower()
    # Accept "not redistributed", "is not redistributed", "not re-distributed",
    # "built locally", or "build locally".
    has_no_redist = re.search(r"not\s+re.?distribut", readme) is not None
    has_build_locally = "built locally" in readme or "build locally" in readme
    assert has_no_redist or has_build_locally, (
        "README.md must state that data is NOT redistributed and must be built locally"
    )


# ---------------------------------------------------------------------------
# AC-PRIV — no committed/staged file contains operator home paths
# ---------------------------------------------------------------------------

# This very module defines and documents the home-path patterns it scans for,
# so it would match its own ruleset. A security scanner must not flag its own
# pattern definitions; exclude this file from the deliverable-file scan.
_SELF_FILENAME = pathlib.Path(__file__).name


def _collect_tracked_text_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    """Return a list of tracked text files via git ls-files (or all text files
    when git is not available / not a git repo).

    The module defining ``_HOME_PATH_PATTERN`` is excluded because it
    intentionally contains the very substrings the pattern matches.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            rel_paths = result.stdout.splitlines()
            return [
                repo_root / p for p in rel_paths
                if p
                and (repo_root / p).is_file()
                and pathlib.Path(p).name != _SELF_FILENAME
            ]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Fallback: walk the tree, skip noise/cache directories.
    _SKIP_DIRS = frozenset({
        ".remember",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".git",
    })
    collected: list[pathlib.Path] = []
    for p in repo_root.rglob("*"):
        if (
            p.is_file()
            and not _SKIP_DIRS.intersection(p.parts)
            and p.name != _SELF_FILENAME
        ):
            collected.append(p)
    return collected


def test_no_operator_home_paths_in_tracked_files(repo_root: pathlib.Path) -> None:
    """Given the repository contains tracked text files.
    When we scan every tracked text file for operator-specific home paths.
    Then no file may contain /home/<realname>/, /Users/<realname>/, or
         C:\\Users\\<realname>\\ where <realname> is not a generic placeholder.

    Generic placeholders allowed: user, operator, dev, test, example, you,
    me, username, admin, vagrant.
    """
    offenders: list[tuple[str, int, str]] = []
    files = _collect_tracked_text_files(repo_root)

    for path in files:
        if not _file_is_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _HOME_PATH_PATTERN.search(line):
                rel = path.relative_to(repo_root)
                offenders.append((str(rel), lineno, line.strip()))

    assert not offenders, (
        "Operator home-directory paths found in tracked files:\n"
        + "\n".join(f"  {f}:{ln}: {snippet}" for f, ln, snippet in offenders)
    )
