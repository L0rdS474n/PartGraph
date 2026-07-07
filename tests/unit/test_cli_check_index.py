"""
Tests: AC-IDX-23..27 — `partgraph db check-index` CLI command (ADR-0019).

Specifies the CLI wiring for the NEW `db check-index` sub-command (Gate 4;
leaf contract in partgraph.util.index_health, tested hermetically in
tests/unit/test_index_health.py). Kept in its OWN file rather than added to
tests/unit/test_cli.py so that file — and its many unrelated `db`/search/embed
regression pins — stays completely stable while this feature is under
construction (mirrors the existing precedent set by test_cli_refresh_links.py
/ test_cli_reembed.py: new commands get their own CLI test file).

Pinned contract (Gate 4 wires this; NOT YET IMPLEMENTED):
  - `partgraph db check-index` calls
    `check_index_integrity(schema_text=load_schema(SCHEMA_FILE))` with ZERO
    overrides (no url/timeout/http_post kwargs — the real network/lazy-
    requests defaults are always used).
  - Prints `result.message` (markup=False — untrusted for Rich markup, same
    discipline as `db status`).
  - `raise typer.Exit(code=0 if (reachable and schema_ok and
    self_similarity_ok in (True, None)) else 1)`.
  - Engine-independent: NEVER calls `compose_command()` / `subprocess.run`
    (same architectural guarantee ADR-0018 established for `db status`).

Test-double design note: these tests patch `partgraph.cli.check_index_integrity`
(a target that does not exist on `partgraph.cli` until Gate 4 wires it), so
`patch(...)` itself raises AttributeError at test-setup time — the correct
RED state before implementation. The mocked return values below are built
with `types.SimpleNamespace` (duck-typing the four fields cli.py will read:
`reachable`/`schema_ok`/`self_similarity_ok`/`message`) rather than the real
`partgraph.util.index_health.IndexIntegrityResult` dataclass, DELIBERATELY: it
keeps this file's collection independent of whether
`partgraph.util.index_health` exists yet, so a test that needs NO patch at all
— AC-IDX-27, the `db status` regression pin below — stays collectible and
GREEN right now, rather than the whole file failing to collect via a stray
module-level `ModuleNotFoundError` import. (Mirrors how test_cli_refresh_links.py
"deliberately does NOT import partgraph.refresh.links ... so its collection/RED
failures are attributable strictly to the CLI layer".)

Expected RED right now (before Gate 4):
  - AC-IDX-23/24/25 ERROR at `patch("partgraph.cli.check_index_integrity", ...)`
    (AttributeError: module 'partgraph.cli' has no attribute
    'check_index_integrity').
  - AC-IDX-26 (`--help`) FAILS: `check-index` is not yet a registered
    sub-command, so Click reports "No such command" (a non-zero exit code
    instead of the expected 0).
  - AC-IDX-27 (regression pin) is GREEN right now and MUST STAY GREEN: it
    exercises only the ALREADY-MERGED `db status` / `probe_health` contract
    (ADR-0018, PR #21) and needs no `check_index_integrity` at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from partgraph.cli import SCHEMA_FILE, app
from partgraph.schema import load_schema
from partgraph.util.container import ContainerEngineError
from partgraph.util.health import HealthResult

RUNNER = CliRunner()


def _invoke(args: list[str]):
    """Invoke the CLI app with the given args and return the result."""
    return RUNNER.invoke(app, args)


def _fake_result(
    *,
    reachable: bool,
    schema_ok: bool | None,
    self_similarity_ok: bool | None,
    message: str,
) -> SimpleNamespace:
    """Build a duck-typed stand-in for IndexIntegrityResult.

    Exposes exactly the four fields `db check-index` is contracted to read
    (`reachable`/`schema_ok`/`self_similarity_ok`/`message`); see the module
    docstring for why this is a SimpleNamespace rather than the real frozen
    dataclass.
    """
    return SimpleNamespace(
        reachable=reachable,
        schema_ok=schema_ok,
        self_similarity_ok=self_similarity_ok,
        message=message,
    )


def _assert_clean_exit(result, expected_code: int) -> None:
    """Assert *result* exited with *expected_code* and leaked no traceback."""
    assert result.exit_code == expected_code, (
        f"`db check-index` should exit {expected_code}, got {result.exit_code}.\n"
        f"Output:\n{result.output!r}"
    )
    assert "Traceback" not in result.output
    if result.exception is not None:
        assert isinstance(result.exception, SystemExit), (
            "An unhandled exception leaked to the CLI surface instead of a "
            f"clean typer.Exit. Got: {result.exception!r}"
        )


# ---------------------------------------------------------------------------
# AC-IDX-23 — exit 0 when healthy (schema_ok True; self_similarity_ok True OR
# None — "no embedded parts yet" must still count as passing per the ratified
# exit formula, NOT a failure).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("self_similarity_ok", "case_id"),
    [
        pytest.param(True, "self_similarity_confirmed"),
        pytest.param(None, "no_embedded_parts_yet"),
    ],
)
def test_db_check_index_ac23_exits_zero_when_healthy(self_similarity_ok, case_id) -> None:
    """AC-IDX-23: Given check_index_integrity() reports reachable=True,
    schema_ok=True, and self_similarity_ok is EITHER True (confirmed) or None
    (nothing embedded yet to check).
    When we invoke `partgraph db check-index`.
    Then the command exits 0 in BOTH cases, and the exact message text
    (including a literal '[' bracket, e.g. "[hnsw]") is printed VERBATIM —
    proving the message is printed with markup=False (a literal '[...]'
    survives; Rich markup would otherwise try to interpret it as a style tag).
    """
    message = f"Index integrity OK: schema matches [hnsw exponent=6] ({case_id})."
    healthy = _fake_result(
        reachable=True,
        schema_ok=True,
        self_similarity_ok=self_similarity_ok,
        message=message,
    )
    with patch("partgraph.cli.check_index_integrity", return_value=healthy):
        result = _invoke(["db", "check-index"])

    _assert_clean_exit(result, 0)
    assert message in result.output, (
        f"Expected the exact message (with literal brackets) in output. "
        f"Got:\n{result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-IDX-24 — exit 1 on drift / unreachable / self-similarity failure.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("reachable", "schema_ok", "self_similarity_ok", "case_id"),
    [
        pytest.param(True, False, True, "schema_drift"),
        pytest.param(True, True, False, "self_similarity_failure"),
        pytest.param(False, None, None, "unreachable"),
        # (reachable=True, schema_ok=None) is not a state check_index_integrity
        # can produce (schema_ok is None only when the live schema query itself
        # failed, which always implies reachable=False) — intentionally
        # excluded rather than asserting undefined behaviour.
    ],
)
def test_db_check_index_ac24_exits_one_on_drift_unreachable_or_selfsim_failure(
    reachable, schema_ok, self_similarity_ok, case_id,
) -> None:
    """AC-IDX-24: Given check_index_integrity() reports any of: a schema
    drift (schema_ok False), a self-similarity failure (self_similarity_ok
    False), or the database being unreachable.
    When we invoke `partgraph db check-index`.
    Then the command exits 1 in every case, printing the message and no
    traceback.
    """
    unhealthy = _fake_result(
        reachable=reachable,
        schema_ok=schema_ok,
        self_similarity_ok=self_similarity_ok,
        message=f"Index integrity check failed ({case_id}).",
    )
    with patch("partgraph.cli.check_index_integrity", return_value=unhealthy):
        result = _invoke(["db", "check-index"])

    _assert_clean_exit(result, 1)
    assert f"({case_id})" in result.output, (
        f"Expected the message (containing '({case_id})') in output. "
        f"Got:\n{result.output!r}"
    )


def test_db_check_index_calls_leaf_with_schema_text_and_zero_overrides() -> None:
    """CONTRACT: Given `db check-index` must call check_index_integrity with
    EXACTLY the real on-disk schema text and NO url/timeout/http_post
    overrides (the leaf's own real-network defaults are always used).
    When we invoke `partgraph db check-index` with the leaf patched.
    Then check_index_integrity is called ONCE with schema_text equal to
    load_schema(SCHEMA_FILE) and no other keyword argument.
    """
    expected_schema_text = load_schema(SCHEMA_FILE)
    healthy = _fake_result(
        reachable=True, schema_ok=True, self_similarity_ok=True, message="ok [x]"
    )
    with patch(
        "partgraph.cli.check_index_integrity", return_value=healthy
    ) as mock_check:
        _invoke(["db", "check-index"])

    mock_check.assert_called_once_with(schema_text=expected_schema_text)


# ---------------------------------------------------------------------------
# AC-IDX-25 — engine independence: never compose_command / subprocess.run.
# ---------------------------------------------------------------------------

def test_db_check_index_ac25_never_touches_container_engine_or_subprocess() -> None:
    """AC-IDX-25 (engine independence, mirrors ADR-0018's `db status` guarantee):
    Given NO container engine is on PATH — compose_command() raises
    ContainerEngineError — but check_index_integrity() reports healthy.
    When we invoke `partgraph db check-index`.
    Then the command exits 0 AND subprocess.run is NEVER called:
    `check-index` must be engine-independent, never delegating to
    `compose`/`subprocess.run` at all.
    """
    healthy = _fake_result(
        reachable=True, schema_ok=True, self_similarity_ok=True, message="ok [x]"
    )
    with (
        patch(
            "partgraph.cli.compose_command",
            side_effect=ContainerEngineError("no engine"),
        ),
        patch("partgraph.cli.check_index_integrity", return_value=healthy),
        patch("subprocess.run") as mock_run,
    ):
        result = _invoke(["db", "check-index"])

    _assert_clean_exit(result, 0)
    assert not mock_run.called, (
        "`db check-index` called subprocess.run even though no container "
        "engine is on PATH. It must be engine-independent."
    )


# ---------------------------------------------------------------------------
# SECURITY (Finding 2) — an UNEXPECTED exception from the leaf is turned into a
# fixed, path-free error and a clean exit 1 (defense-in-depth, mirrors the
# `db status` guard from ADR-0018). The leaf deliberately lets non-requests
# exceptions propagate (test_index_health.py AC-IDX-20); the CLI must catch them
# here so no raw traceback (which could leak an internal path) reaches the user.
# ---------------------------------------------------------------------------

def test_db_check_index_unexpected_exception_exits_one_without_traceback() -> None:
    """SECURITY (Finding 2, defense-in-depth): Given check_index_integrity()
    raises an UNEXPECTED exception (a RuntimeError here — modelling a programming
    error, or a non-requests failure the leaf deliberately lets propagate rather
    than masking as "database down").
    When we invoke `partgraph db check-index`.
    Then the command exits 1, prints a fixed error that does NOT interpolate the
    raw exception text, leaks NO traceback to the CLI surface, and the rendered
    output is path-free — mirroring the `db status` defense-in-depth guard.
    """
    with patch(
        "partgraph.cli.check_index_integrity",
        side_effect=RuntimeError("boom-internal-detail-should-not-leak"),
    ):
        result = _invoke(["db", "check-index"])

    _assert_clean_exit(result, 1)
    assert "boom-internal-detail-should-not-leak" not in result.output, (
        "the raw exception text must never be interpolated into CLI output. "
        f"Got:\n{result.output!r}"
    )
    assert "/" not in result.output, (
        f"the CLI error message must be path-free. Got:\n{result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-IDX-26 — `db check-index --help` exits 0 and is English.
# ---------------------------------------------------------------------------

def test_db_check_index_ac26_help_exits_zero_and_is_english() -> None:
    """AC-IDX-26: Given `db check-index` is a registered sub-command.
    When we invoke `partgraph db check-index --help`.
    Then the exit code is 0 and the output contains "sage" (matching Typer's
    auto-generated Usage/usage banner) — `--help` short-circuits before the
    command body runs, so this test needs no leaf patch.
    """
    result = _invoke(["db", "check-index", "--help"])
    assert result.exit_code == 0, (
        f"`db check-index --help` exited {result.exit_code} (expected 0). "
        f"Output:\n{result.output!r}"
    )
    assert "sage" in result.output


# ---------------------------------------------------------------------------
# AC-IDX-27 — REGRESSION PIN: `db status` is unchanged by this feature.
# Must be GREEN right now (before Gate 4) and stay green after.
# ---------------------------------------------------------------------------

def test_db_status_ac27_regression_pin_unchanged_by_check_index_feature() -> None:
    """AC-IDX-27 (regression pin): Given `db status` remains the existing
    probe_health()-based health check (ADR-0018, PR #21) — this feature adds
    a NEW sibling command, `check-index`, and must not alter `db status` at
    all.
    When probe_health() reports healthy and we invoke `partgraph db status`.
    Then it still exits 0 and mentions "healthy" — reusing the exact pattern
    from tests/unit/test_health.py's own CLI block. This test imports ONLY
    partgraph.util.health (already merged) — never partgraph.util.index_health
    — so it stays collectible and GREEN independent of this feature's
    progress.
    """
    healthy_result = HealthResult(
        reachable=True,
        healthy=True,
        http_status=200,
        status="healthy",
        version="v25.3.4",
        message="Dgraph is healthy (v25.3.4).",
    )
    with patch("partgraph.cli.probe_health", return_value=healthy_result):
        result = _invoke(["db", "status"])

    assert result.exit_code == 0, (
        f"`db status` should exit 0 when healthy, got {result.exit_code}.\n"
        f"Output:\n{result.output!r}"
    )
    assert "healthy" in result.output
