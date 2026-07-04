"""
Tests: M-1 (Gate 3 security MUST) -- committed regression lock for
scripts/partgraph-refresh-all.sh.

Gate 3 security flagged M-1: the wrapper script exists and is correct, but
nothing COMMITTED pinned its security-relevant contract, so a future edit
(e.g. adding `|| true` around a phase call to "fix" a noisy exit code) would
pass CI silently. This file closes that gap.

It drives the REAL, already-committed wrapper via `subprocess` against a fake
`partgraph` stub written fresh into `tmp_path` for each test -- the same
stub-double technique proved out in the scratchpad `stub_harness` during the
PR-3 test-contract stage, now committed as an executable regression lock
instead of a throwaway dry run. No real database, no network, no container,
no ~1 GB download: the stub is a two-branch bash script whose exit code is
entirely controlled by environment variables the test sets, and it logs its
own argv so invocation order/count can be asserted.

This is a regression lock (GREEN against the wrapper as it exists today), not
a test-first/red-first spec -- the wrapper predates this file and is not
modified by it.

Pinned contract (read directly from scripts/partgraph-refresh-all.sh):
  - Both phases (`refresh`, then `refresh-links`) are ALWAYS attempted, in
    that order, regardless of phase 1's exit status.
  - The wrapper's own exit status is the FIRST non-zero phase status (never
    the last, never swallowed) -- the wrapper's own header comment states
    this explicitly ("the first non-zero phase status is propagated").
  - `--fetch` is appended to phase 1 if and only if PARTGRAPH_REFRESH_FETCH
    is set to a non-empty value.
  - A `${PARTGRAPH_BIN:-partgraph}` that does not resolve via `command -v`
    exits 127 with a path-free message, never a silent success.

Deterministic: no wall clock or randomness is asserted upon (the wrapper's
own `date -u` banners are ignored here); every outcome is driven purely by
the STUB_REFRESH_RC / STUB_LINKS_RC / PARTGRAPH_REFRESH_FETCH / PARTGRAPH_BIN
environment variables the test sets explicitly.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WRAPPER_REL = "scripts/partgraph-refresh-all.sh"
SUBPROCESS_TIMEOUT_S = 15

# Fake `partgraph` CLI double. NOT production code -- exists only inside a
# test's tmp_path, never installed, never on the real PATH outside a test
# process. Logs its full argv to $STUB_LOG (one line per call, if set) and
# exits with a code selected by argv[1]:
#   "refresh"        -> exit ${STUB_REFRESH_RC:-0}
#   "refresh-links"  -> exit ${STUB_LINKS_RC:-0}
#   anything else    -> exit 0
# No wall clock, no RNG, no network -- the only inputs are argv and the two
# STUB_*_RC env vars, so behaviour is fully reproducible.
_STUB_SCRIPT = r"""#!/usr/bin/env bash
set -uo pipefail
subcommand="${1:-}"
if [[ -n "${STUB_LOG:-}" ]]; then
    printf '%s\n' "$*" >> "$STUB_LOG"
fi
case "$subcommand" in
    refresh) exit "${STUB_REFRESH_RC:-0}" ;;
    refresh-links) exit "${STUB_LINKS_RC:-0}" ;;
    *) exit 0 ;;
esac
"""


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def bash_path() -> str:
    """Resolve `bash` on PATH, or skip cleanly (never hard-fail) if absent.

    Given: the runner executing this suite may not have bash on PATH.
    When: any test in this module requests this fixture.
    Then: the resolved absolute bash path is returned, or the test is
    skipped with a clear reason -- it must never fail on a non-bash runner.
    """
    resolved = shutil.which("bash")
    if resolved is None:
        pytest.skip("bash is not available on PATH; cannot exercise the wrapper script.")
    return resolved


def _write_stub(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write the executable fake `partgraph` stub into *tmp_path*; return its path."""
    path = tmp_path / "partgraph"
    path.write_text(_STUB_SCRIPT, encoding="utf-8")
    path.chmod(0o755)
    return path


def _logged_subcommands(log_path: pathlib.Path) -> list[str]:
    """Return the subcommand (argv[0]) of each logged stub invocation, in order."""
    if not log_path.exists():
        return []
    return [
        line.split(" ", 1)[0]
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run_wrapper(  # noqa: PLR0913 -- one keyword-only seam per scenario knob, mirroring cli.py's _refresh_all_pages.
    repo_root: pathlib.Path,
    bash_path: str,
    tmp_path: pathlib.Path,
    *,
    refresh_rc: int = 0,
    links_rc: int = 0,
    fetch_env: str | None = None,
) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
    """Run the REAL, committed wrapper script against a fresh fake stub.

    Returns (completed_process, log_path): the caller inspects the wrapper's
    own exit/stdout/stderr via the former and the stub's logged invocations
    via the latter (see _logged_subcommands).
    """
    stub = _write_stub(tmp_path)
    log_path = tmp_path / "stub_invocations.log"
    wrapper_path = repo_root / WRAPPER_REL
    assert wrapper_path.is_file(), f"{WRAPPER_REL} does not exist at {wrapper_path}"

    env = dict(os.environ)
    env["PARTGRAPH_BIN"] = str(stub)
    env["STUB_LOG"] = str(log_path)
    env["STUB_REFRESH_RC"] = str(refresh_rc)
    env["STUB_LINKS_RC"] = str(links_rc)
    if fetch_env is None:
        env.pop("PARTGRAPH_REFRESH_FETCH", None)
    else:
        env["PARTGRAPH_REFRESH_FETCH"] = fetch_env

    result = subprocess.run(
        [bash_path, str(wrapper_path)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=env,
        cwd=str(repo_root),
    )
    return result, log_path


# ---------------------------------------------------------------------------
# Order + both-always-attempted + aggregate exit ("first non-zero wins")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("refresh_rc", "links_rc", "expected_wrapper_rc"),
    [
        pytest.param(0, 0, 0, id="both_phases_succeed"),
        pytest.param(5, 0, 5, id="refresh_fails_wrapper_propagates_5"),
        pytest.param(0, 7, 7, id="links_fails_wrapper_propagates_7"),
        pytest.param(5, 7, 5, id="both_fail_wrapper_propagates_first_nonzero_not_last"),
    ],
)
def test_both_phases_always_run_in_order_and_exit_aggregates(  # noqa: PLR0913 -- repo_root/bash_path/tmp_path fixtures plus one param per parametrize case.
    repo_root: pathlib.Path,
    bash_path: str,
    tmp_path: pathlib.Path,
    refresh_rc: int,
    links_rc: int,
    expected_wrapper_rc: int,
) -> None:
    """Given the real, committed wrapper and a stub CLI whose two phases exit
    with the given codes, When the wrapper is run, Then `refresh` and
    `refresh-links` are each invoked EXACTLY ONCE, in that order, regardless
    of phase 1's outcome, and the wrapper's own exit status is the FIRST
    non-zero phase status -- never the last, never swallowed. The
    both_fail case is the load-bearing one: it distinguishes "first
    non-zero" from "last non-zero" and from a blanket `exit 1`.
    """
    result, log_path = _run_wrapper(
        repo_root, bash_path, tmp_path, refresh_rc=refresh_rc, links_rc=links_rc
    )

    assert result.returncode == expected_wrapper_rc, (
        f"wrapper exit={result.returncode}, expected={expected_wrapper_rc}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    subcommands = _logged_subcommands(log_path)
    assert subcommands == ["refresh", "refresh-links"], (
        "expected refresh then refresh-links, each exactly once; "
        f"got {subcommands!r}"
    )


# ---------------------------------------------------------------------------
# --fetch gating via PARTGRAPH_REFRESH_FETCH
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fetch_env", "expect_fetch_flag"),
    [
        pytest.param(None, False, id="fetch_env_unset"),
        pytest.param("", False, id="fetch_env_empty_string"),
        pytest.param("1", True, id="fetch_env_set_to_1"),
        pytest.param("anything", True, id="fetch_env_set_to_arbitrary_nonempty"),
    ],
)
def test_fetch_flag_gated_by_env_var(
    repo_root: pathlib.Path,
    bash_path: str,
    tmp_path: pathlib.Path,
    fetch_env: str | None,
    expect_fetch_flag: bool,
) -> None:
    """Given PARTGRAPH_REFRESH_FETCH is unset, empty, or a non-empty value,
    When phase 1 (refresh) runs, Then `--fetch` is present in ITS logged argv
    if and only if the env var is non-empty -- unset and the empty string
    are both treated as opt-out, and any non-empty value (not only "1") is
    opt-in.
    """
    result, log_path = _run_wrapper(repo_root, bash_path, tmp_path, fetch_env=fetch_env)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    refresh_lines = [
        line for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.split(" ", 1)[0] == "refresh"
    ]
    assert len(refresh_lines) == 1, f"expected exactly one refresh call, got {refresh_lines!r}"
    has_fetch = "--fetch" in refresh_lines[0].split()
    assert has_fetch is expect_fetch_flag, (
        f"refresh argv={refresh_lines[0]!r}, expected --fetch present={expect_fetch_flag}"
    )


# ---------------------------------------------------------------------------
# Missing binary -- fails loudly, path-free, never silently succeeds
# ---------------------------------------------------------------------------


def test_missing_binary_exits_nonzero_path_free(
    repo_root: pathlib.Path,
    bash_path: str,
) -> None:
    """Given PARTGRAPH_BIN names a binary that does not exist anywhere on
    PATH, When the wrapper runs, Then it exits 127 with a path-free message
    (no operator home-directory path is echoed in its output) -- it must
    never silently succeed on a broken installation.
    """
    wrapper_path = repo_root / WRAPPER_REL
    assert wrapper_path.is_file(), f"{WRAPPER_REL} does not exist at {wrapper_path}"

    nonexistent = "partgraph-refresh-all-test-nonexistent-binary-zzz"
    assert shutil.which(nonexistent) is None, (
        f"test setup invalid: {nonexistent!r} unexpectedly resolves on PATH"
    )

    env = dict(os.environ)
    env["PARTGRAPH_BIN"] = nonexistent
    env.pop("PARTGRAPH_REFRESH_FETCH", None)

    result = subprocess.run(
        [bash_path, str(wrapper_path)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=env,
        cwd=str(repo_root),
    )

    assert result.returncode == 127, (
        f"expected exit 127 for a missing binary, got {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "not found" in combined.lower(), f"expected a 'not found' hint, got: {combined!r}"
    assert "/home/" not in combined, f"operator home path leaked into wrapper output: {combined!r}"


# ---------------------------------------------------------------------------
# ${PARTGRAPH_BIN:-partgraph} default-name resolution via PATH (not only the
# absolute-path override style exercised by the tests above)
# ---------------------------------------------------------------------------


def test_stub_resolves_via_path_prepend_not_only_absolute_bin(
    repo_root: pathlib.Path,
    bash_path: str,
    tmp_path: pathlib.Path,
) -> None:
    """Given PARTGRAPH_BIN is left UNSET (so the wrapper's own default
    `${PARTGRAPH_BIN:-partgraph}` applies) and the stub's directory is
    prepended to PATH, When the wrapper runs, Then `command -v partgraph`
    resolves to the stub and the phase-invocation contract still holds --
    proving the bare-name/PATH resolution style, not only the
    absolute-PARTGRAPH_BIN override style used elsewhere in this file.
    """
    wrapper_path = repo_root / WRAPPER_REL
    stub = _write_stub(tmp_path)
    log_path = tmp_path / "stub_invocations.log"
    assert stub.name == "partgraph"

    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env.pop("PARTGRAPH_BIN", None)
    env["STUB_LOG"] = str(log_path)
    env["STUB_REFRESH_RC"] = "0"
    env["STUB_LINKS_RC"] = "0"
    env.pop("PARTGRAPH_REFRESH_FETCH", None)

    result = subprocess.run(
        [bash_path, str(wrapper_path)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=env,
        cwd=str(repo_root),
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert _logged_subcommands(log_path) == ["refresh", "refresh-links"]
