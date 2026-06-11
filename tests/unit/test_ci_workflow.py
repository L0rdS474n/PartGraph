"""
Tests: R8

Verifies that .github/workflows/ci.yml:
- Parses as valid YAML.
- Contains a ruff linting step.
- Contains a pytest step that excludes integration tests
  (uses -m "not integration" or equivalent).
- Specifies python-version 3.12.
- Has a job or check named "CI" (for branch-protection required checks).
- Does NOT run docker or integration tests.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

CI_WORKFLOW_REL = ".github/workflows/ci.yml"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ci_workflow(repo_root: pathlib.Path) -> dict:
    """Parse and return the CI workflow YAML.

    Given .github/workflows/ci.yml exists.
    When we load it with yaml.safe_load.
    Then we return the parsed dict for subsequent assertions.
    """
    path = repo_root / CI_WORKFLOW_REL
    assert path.exists(), f"{CI_WORKFLOW_REL} does not exist."
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ci_workflow_text(repo_root: pathlib.Path) -> str:
    """Return raw text of the CI workflow for string-level checks."""
    path = repo_root / CI_WORKFLOW_REL
    assert path.exists(), f"{CI_WORKFLOW_REL} does not exist."
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# R8 — basic structure
# ---------------------------------------------------------------------------

def test_ci_workflow_parses_as_valid_yaml(repo_root: pathlib.Path) -> None:
    """Given .github/workflows/ci.yml exists.
    When we attempt to parse it with yaml.safe_load.
    Then it must parse without error and return a non-empty dict.
    """
    path = repo_root / CI_WORKFLOW_REL
    assert path.exists(), f"{CI_WORKFLOW_REL} does not exist."
    content = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    assert isinstance(parsed, dict), (
        f"{CI_WORKFLOW_REL} did not parse as a YAML mapping."
    )
    assert parsed, f"{CI_WORKFLOW_REL} parsed as an empty mapping."


def test_ci_workflow_has_job_or_check_named_CI(ci_workflow: dict) -> None:
    """Given the CI workflow defines one or more jobs.
    When we inspect job names.
    Then at least one job must be named 'CI' (exact, for branch protection).
    """
    jobs = ci_workflow.get("jobs", {})
    assert "CI" in jobs, (
        f"No job named 'CI' found in {CI_WORKFLOW_REL}. "
        f"Branch protection requires this exact name. Jobs present: {list(jobs.keys())}"
    )


# ---------------------------------------------------------------------------
# R8 — python-version 3.12
# ---------------------------------------------------------------------------

def test_ci_workflow_uses_python_312(ci_workflow_text: str) -> None:
    """Given the CI workflow sets up Python.
    When we scan the workflow text.
    Then 'python-version' must be set to '3.12' (not 3.11 or 3.13).
    """
    # Accept "3.12" as a string value anywhere in the file.
    assert re.search(r"python-version['\"]?\s*[:=]\s*['\"]?3\.12", ci_workflow_text), (
        f"python-version 3.12 not found in {CI_WORKFLOW_REL}. "
        "Check the setup-python step."
    )


# ---------------------------------------------------------------------------
# R8 — ruff step
# ---------------------------------------------------------------------------

def test_ci_workflow_contains_ruff_step(ci_workflow_text: str) -> None:
    """Given the CI workflow runs linting.
    When we scan the workflow text.
    Then 'ruff' must appear (as a run command or uses action).
    """
    assert "ruff" in ci_workflow_text, (
        f"No ruff step found in {CI_WORKFLOW_REL}. "
        "A ruff linting step is required."
    )


# ---------------------------------------------------------------------------
# R8 — pytest step that excludes integration tests
# ---------------------------------------------------------------------------

def test_ci_workflow_contains_pytest_step(ci_workflow_text: str) -> None:
    """Given the CI workflow runs tests.
    When we scan the workflow text.
    Then 'pytest' must appear as a run command.
    """
    assert "pytest" in ci_workflow_text, (
        f"No pytest step found in {CI_WORKFLOW_REL}."
    )


def test_ci_workflow_pytest_excludes_integration_marker(ci_workflow_text: str) -> None:
    """Given the CI workflow runs pytest.
    When we inspect the pytest invocation.
    Then it must exclude integration-marked tests via
    `-m "not integration"` or equivalent.

    This ensures the CI job does not require Docker / a running Dgraph.
    """
    # Look for -m "not integration" or -m 'not integration'
    has_marker_exclusion = re.search(
        r'pytest.*-m\s+["\']not\s+integration["\']',
        ci_workflow_text,
        re.DOTALL,
    )
    assert has_marker_exclusion, (
        f"CI pytest step does not exclude integration tests with "
        f'`-m "not integration"`. Found in {CI_WORKFLOW_REL}:\n'
        + "\n".join(
            line for line in ci_workflow_text.splitlines() if "pytest" in line
        )
    )


# ---------------------------------------------------------------------------
# R8 — no docker or integration tests in CI
# ---------------------------------------------------------------------------

def test_ci_workflow_does_not_run_docker_compose(ci_workflow_text: str) -> None:
    """Given the CI workflow must run without Docker.
    When we scan the workflow text for docker compose invocations.
    Then 'docker compose' or 'docker-compose' must not appear.
    """
    has_docker_compose = (
        "docker compose" in ci_workflow_text
        or "docker-compose" in ci_workflow_text
    )
    assert not has_docker_compose, (
        f"CI workflow must not invoke docker compose (no integration tests in CI). "
        f"Found docker compose reference in {CI_WORKFLOW_REL}."
    )


def test_ci_workflow_does_not_run_integration_tests(ci_workflow_text: str) -> None:
    """Given the CI workflow must run without infrastructure.
    When we scan the workflow text.
    Then 'integration' must not appear in any pytest invocation without the
    'not' negation (i.e. we must not run integration tests).
    """
    # Find all pytest invocations and ensure none run integration tests positively.
    # We allow the word 'integration' only in the context of 'not integration'.
    for line in ci_workflow_text.splitlines():
        stripped = line.strip()
        if "pytest" in stripped and "integration" in stripped:
            # Acceptable: "not integration"
            if "not integration" not in stripped:
                pytest.fail(
                    f"CI workflow appears to run integration tests on this line:\n"
                    f"  {stripped}\n"
                    f"Only `not integration` exclusion is acceptable."
                )


# ---------------------------------------------------------------------------
# Security review additions — permissions and action pinning
# ---------------------------------------------------------------------------

def test_ci_workflow_has_explicit_permissions_block(ci_workflow: dict) -> None:
    """Given the CI workflow must follow least-privilege principles.
    When we inspect the top-level 'permissions' key or every job's 'permissions'.
    Then either the workflow-level permissions block must exist with
    contents:read, OR every job must declare its own permissions block that
    includes contents:read.

    An absent permissions block defaults to write-all on classic repos, which
    violates the supply-chain hardening requirement.
    """
    top_permissions = ci_workflow.get("permissions")
    if top_permissions is not None:
        # Top-level block present — must contain contents: read (or read-all shorthand).
        if isinstance(top_permissions, str):
            # 'read-all' shorthand is acceptable.
            assert top_permissions == "read-all", (
                f"Top-level permissions shorthand must be 'read-all', got '{top_permissions}'"
            )
        else:
            assert isinstance(top_permissions, dict), (
                f"Top-level permissions must be a mapping or 'read-all', got: {top_permissions!r}"
            )
            assert top_permissions.get("contents") in ("read", "read-all"), (
                f"Top-level permissions must include 'contents: read', got: {top_permissions}"
            )
        return

    # No top-level block — every job must have its own.
    jobs = ci_workflow.get("jobs", {})
    assert jobs, f"No jobs defined in {CI_WORKFLOW_REL}"
    jobs_missing_permissions = [
        job_name for job_name, job_body in jobs.items()
        if "permissions" not in (job_body or {})
    ]
    assert not jobs_missing_permissions, (
        f"No top-level 'permissions' block found in {CI_WORKFLOW_REL}, and the "
        f"following jobs also lack a 'permissions' block: {jobs_missing_permissions}. "
        "Add 'permissions: contents: read' at the workflow or job level."
    )


def test_ci_workflow_actions_not_pinned_to_mutable_branch(ci_workflow_text: str) -> None:
    """Given the CI workflow must use immutable action references.
    When we scan every 'uses:' line in the workflow.
    Then no action reference may end with @main or @master (mutable branch tags).

    Mutable branch pins allow upstream supply-chain attacks; all actions must
    be pinned to a tag (e.g. @v4) or a full commit SHA.
    """
    mutable_refs: list[str] = []
    for line in ci_workflow_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("uses:"):
            ref_part = stripped[len("uses:"):].strip()
            # Strip trailing inline comment if any.
            ref_part = ref_part.split("#")[0].strip()
            if ref_part.endswith("@main") or ref_part.endswith("@master"):
                mutable_refs.append(stripped)

    assert not mutable_refs, (
        f"The following action references in {CI_WORKFLOW_REL} are pinned to a "
        f"mutable branch (@main or @master) which is forbidden:\n"
        + "\n".join(f"  {r}" for r in mutable_refs)
        + "\nPin to a version tag (e.g. @v4) or a full commit SHA instead."
    )
