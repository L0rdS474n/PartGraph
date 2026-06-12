"""
Tests: pyproject.toml optional-dependencies contract for PR4.

Specifies that:
  - optional-dependencies.embed contains "sentence-transformers".
  - optional-dependencies.dev does NOT contain "sentence-transformers".
  - runtime dependencies (project.dependencies) contains "psutil".

These tests are structural contract tests on pyproject.toml. They will be
red until pyproject.toml is updated with the embed extra and psutil dependency.

NOTE: These tests parse pyproject.toml directly; they do NOT require
sentence_transformers or psutil to be installed.
"""

from __future__ import annotations

import pathlib
import tomllib

# Locate pyproject.toml: this file is at tests/unit/test_pyproject_extras.py,
# so the repo root is three levels up.
_TESTS_UNIT_DIR = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_UNIT_DIR.parent.parent
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    """Load and return the parsed pyproject.toml content."""
    assert _PYPROJECT_PATH.is_file(), (
        f"pyproject.toml not found at {_PYPROJECT_PATH}. "
        "Test must be run from within the PartGraph repository."
    )
    return tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))


def test_pyproject_embed_extra_contains_sentence_transformers() -> None:
    """Given pyproject.toml.
    When optional-dependencies.embed is read.
    Then it contains a dependency on "sentence-transformers" (the package name,
    possibly with a version specifier such as "sentence-transformers>=2.0").
    """
    data = _load_pyproject()
    optional_deps = data.get("project", {}).get("optional-dependencies", {})

    assert "embed" in optional_deps, (
        f"pyproject.toml must define [project.optional-dependencies.embed]. "
        f"Found optional-dependencies keys: {list(optional_deps.keys())}"
    )

    embed_deps = optional_deps["embed"]
    assert isinstance(embed_deps, list), (
        f"optional-dependencies.embed must be a list; got {type(embed_deps)!r}"
    )

    has_st = any(
        "sentence-transformers" in dep.lower()
        for dep in embed_deps
    )
    assert has_st, (
        f"optional-dependencies.embed must contain 'sentence-transformers'. "
        f"Got: {embed_deps!r}"
    )


def test_pyproject_dev_extra_does_not_contain_sentence_transformers() -> None:
    """Given pyproject.toml.
    When optional-dependencies.dev is read.
    Then it does NOT contain sentence-transformers.
    (sentence-transformers is heavy; it must only be in the [embed] extra,
    not pulled in for every developer who does pip install -e ".[dev]".)
    """
    data = _load_pyproject()
    optional_deps = data.get("project", {}).get("optional-dependencies", {})

    if "dev" not in optional_deps:
        # dev extra not defined: trivially satisfies the constraint.
        return

    dev_deps = optional_deps["dev"]
    assert isinstance(dev_deps, list), (
        f"optional-dependencies.dev must be a list; got {type(dev_deps)!r}"
    )

    has_st = any(
        "sentence-transformers" in dep.lower()
        for dep in dev_deps
    )
    assert not has_st, (
        f"optional-dependencies.dev must NOT contain 'sentence-transformers'. "
        f"sentence-transformers belongs only in [embed] extra. "
        f"Got dev deps: {dev_deps!r}"
    )


def test_pyproject_runtime_dependencies_contains_psutil() -> None:
    """Given pyproject.toml.
    When project.dependencies (runtime deps) is read.
    Then it contains "psutil" (required for adaptive resource controller).
    (psutil is a lightweight runtime dep — not optional — because the controller
    is always active during embed_write, even when sentence-transformers is not.)
    """
    data = _load_pyproject()
    runtime_deps = data.get("project", {}).get("dependencies", [])

    assert isinstance(runtime_deps, list), (
        f"project.dependencies must be a list; got {type(runtime_deps)!r}"
    )

    has_psutil = any(
        "psutil" in dep.lower()
        for dep in runtime_deps
    )
    assert has_psutil, (
        f"project.dependencies must contain 'psutil'. "
        f"Got runtime deps: {runtime_deps!r}"
    )
