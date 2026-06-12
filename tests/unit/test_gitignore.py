"""
Tests: R4

Verifies that .gitignore correctly ignores the required paths and that no
files under data/ or matching *.sqlite3 are tracked by git.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _git_check_ignore(repo_root: pathlib.Path, rel_path: str) -> bool:
    """Return True if git considers rel_path ignored."""
    result = _run_git(["check-ignore", "-q", rel_path], cwd=repo_root)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# R4 — git check-ignore confirms required paths are ignored
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel_path",
    [
        "data/anything",
        "foo.sqlite3",
        ".remember/anything",
        # Security review additions: secrets, generated outputs, and cache dirs.
        ".env",
        ".env.local",
        "staging/anything",
        "exports/anything",
        "models/anything",
        "__pycache__/anything",
        "app.log",
        # R4 additions: staged and state directories must not be tracked.
        "data/staged/anything",
        "data/state/anything",
    ],
)
def test_path_is_ignored_by_git(repo_root: pathlib.Path, rel_path: str) -> None:
    """Given a .gitignore that covers data/, *.sqlite3, .remember/,
    .env, .env.local, staging/, exports/, models/, __pycache__/, and *.log.
    When we run `git check-ignore` against each required path.
    Then git must report the path as ignored (exit code 0).
    """
    result = _run_git(["check-ignore", "-q", rel_path], cwd=repo_root)
    assert result.returncode == 0, (
        f"Expected '{rel_path}' to be ignored by git, but it is not.\n"
        f"Ensure .gitignore contains the appropriate rule.\n"
        f"git check-ignore stderr: {result.stderr.strip()}"
    )


# ---------------------------------------------------------------------------
# R4 — git status shows no tracked files under data/ or *.sqlite3
# ---------------------------------------------------------------------------

def test_no_tracked_files_under_data_directory(repo_root: pathlib.Path) -> None:
    """Given the repository has been initialised and .gitignore is applied.
    When we list all tracked files.
    Then no tracked file path must start with 'data/'.
    """
    result = _run_git(["ls-files", "data/"], cwd=repo_root)
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    assert not tracked, (
        f"Found tracked files under data/: {tracked}\n"
        "These must be excluded by .gitignore or never added to the index."
    )


def test_no_tracked_sqlite3_files(repo_root: pathlib.Path) -> None:
    """Given the repository has been initialised and .gitignore is applied.
    When we list all tracked files matching *.sqlite3.
    Then no such file must exist in the index.
    """
    result = _run_git(["ls-files", "*.sqlite3"], cwd=repo_root)
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    assert not tracked, (
        f"Found tracked *.sqlite3 files: {tracked}\n"
        "These must be excluded by .gitignore."
    )


def test_git_status_porcelain_shows_no_data_files(repo_root: pathlib.Path) -> None:
    """Given the working tree may have data/ files present on disk.
    When we run `git status --porcelain`.
    Then the output must contain no entry whose path starts with 'data/'.
    """
    result = _run_git(["status", "--porcelain"], cwd=repo_root)
    data_entries = [
        line for line in result.stdout.splitlines()
        if line[3:].startswith("data/") or line[3:].startswith('"data/')
    ]
    assert not data_entries, (
        f"git status --porcelain shows data/ entries: {data_entries}"
    )


def test_git_status_porcelain_shows_no_sqlite3_files(repo_root: pathlib.Path) -> None:
    """Given the working tree may have *.sqlite3 files present on disk.
    When we run `git status --porcelain`.
    Then the output must contain no entry whose path ends with '.sqlite3'.
    """
    result = _run_git(["status", "--porcelain"], cwd=repo_root)
    sqlite_entries = [
        line for line in result.stdout.splitlines()
        if line.rstrip().endswith(".sqlite3")
    ]
    assert not sqlite_entries, (
        f"git status --porcelain shows *.sqlite3 entries: {sqlite_entries}"
    )
