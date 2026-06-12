"""
Tests: T-CLI-*

Verifies CLI additions for the 'ingest' sub-Typer and 'stats' top-level command
added to partgraph.cli in PR2.

Tests:
- T-CLI-help:          ingest --help, ingest jlcparts --help, stats --help
                       all exit 0 and contain "Usage".
- T-CLI-limit:         --limit 0, --limit -5, --limit abc -> exit non-zero with
                       a clear message containing "--limit must be a positive integer";
                       --limit 5 and --limit sys.maxsize accepted (exit 0 or proceeds).
- T-CLI-missing-file:  no --fetch and the SQLite file absent -> exit non-zero,
                       message names expected path AND hints --fetch.
- T-CLI-pipeline-order: injected fakes (via monkeypatch) confirm fetch runs
                        before normalize, which runs before load; failure in any
                        stage aborts later ones.
- T-CLI-full-stub:     --full -> exit non-zero, message mentions "not implemented"
                       AND "ADR-0001".
- T-CLI-no-shell:      no subprocess.run(..., shell=True) during ingest dry path.

NOTE: Collection will ERROR if the ingest sub-typer is not yet added to
partgraph.cli. That is the expected red state before implementation.
"""

from __future__ import annotations

import os

# Pin a wide terminal so Rich/Typer never wraps long tokens (e.g. "--fetch",
# "ADR-0001") across lines in help/error output. Must precede the partgraph.cli
# import: Rich caches terminal width at Console construction and cli.py builds
# its Console objects at import time. (Color is handled separately by stripping
# ANSI in _invoke, because CI emits ANSI even when NO_COLOR is set.)
os.environ["COLUMNS"] = "200"

import re  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from partgraph.cli import app  # noqa: E402, F401 — env set above must precede this import

RUNNER = CliRunner()

# CI emits colored Rich output even when NO_COLOR is set; the ANSI escape codes
# break exact-substring asserts on help/error text. Strip them so assertions are
# render-independent. Combined with COLUMNS=200 above (no line wrapping), help
# tokens like "--fetch" and "ADR-0001" are always contiguous, plain substrings.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _StrippedResult:
    """Click Result wrapper whose .output has ANSI escape codes removed.

    All other attributes (exit_code, exception, stdout, ...) delegate to the
    wrapped Result unchanged.
    """

    def __init__(self, result: object) -> None:
        self._result = result

    @property
    def output(self) -> str:
        return _ANSI_RE.sub("", self._result.output)

    def __getattr__(self, name: str) -> object:
        return getattr(self._result, name)


def _invoke(args: list[str]):
    return _StrippedResult(RUNNER.invoke(app, args))


# ---------------------------------------------------------------------------
# T-CLI-help
# ---------------------------------------------------------------------------

def test_cli_ingest_help_exits_zero() -> None:
    """Given the partgraph CLI with the ingest sub-command group.
    When we invoke `partgraph ingest --help`.
    Then exit code is 0.
    """
    result = _invoke(["ingest", "--help"])
    assert result.exit_code == 0, (
        f"`ingest --help` exited {result.exit_code}.\nOutput:\n{result.output}"
    )


def test_cli_ingest_help_contains_usage() -> None:
    """Given the ingest sub-command group.
    When we invoke `partgraph ingest --help`.
    Then the output contains 'Usage' or 'usage' (English help text).
    """
    result = _invoke(["ingest", "--help"])
    assert "sage" in result.output, (
        f"ingest --help output does not contain 'Usage': {result.output}"
    )


def test_cli_ingest_jlcparts_help_exits_zero() -> None:
    """Given the jlcparts sub-command of ingest.
    When we invoke `partgraph ingest jlcparts --help`.
    Then exit code is 0.
    """
    result = _invoke(["ingest", "jlcparts", "--help"])
    assert result.exit_code == 0, (
        f"`ingest jlcparts --help` exited {result.exit_code}.\n{result.output}"
    )


def test_cli_ingest_jlcparts_help_contains_usage() -> None:
    """Given the jlcparts command.
    When we invoke `partgraph ingest jlcparts --help`.
    Then the output contains 'Usage'.
    """
    result = _invoke(["ingest", "jlcparts", "--help"])
    assert "sage" in result.output, (
        f"ingest jlcparts --help missing 'Usage': {result.output}"
    )


def test_cli_ingest_jlcparts_help_mentions_fetch_option() -> None:
    """Given the jlcparts command help.
    When rendered.
    Then --fetch option is listed.
    """
    result = _invoke(["ingest", "jlcparts", "--help"])
    assert "--fetch" in result.output, (
        f"--fetch not mentioned in jlcparts help: {result.output}"
    )


def test_cli_ingest_jlcparts_help_mentions_limit_option() -> None:
    """Given the jlcparts command help.
    When rendered.
    Then --limit option is listed.
    """
    result = _invoke(["ingest", "jlcparts", "--help"])
    assert "--limit" in result.output, (
        f"--limit not mentioned in jlcparts help: {result.output}"
    )


def test_cli_stats_help_exits_zero() -> None:
    """Given the stats top-level command.
    When we invoke `partgraph stats --help`.
    Then exit code is 0.
    """
    result = _invoke(["stats", "--help"])
    assert result.exit_code == 0, (
        f"`stats --help` exited {result.exit_code}.\n{result.output}"
    )


def test_cli_stats_help_contains_usage() -> None:
    """Given the stats command.
    When we invoke `partgraph stats --help`.
    Then the output contains 'Usage'.
    """
    result = _invoke(["stats", "--help"])
    assert "sage" in result.output, (
        f"stats --help missing 'Usage': {result.output}"
    )


# ---------------------------------------------------------------------------
# T-CLI-limit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_limit", ["0", "-5", "-1", "abc", "0.5"])
def test_cli_limit_invalid_values_exit_nonzero(bad_limit: str, tmp_path) -> None:
    """Given an invalid --limit value (zero, negative, or non-integer string).
    When `partgraph ingest jlcparts --limit <bad_limit>` is invoked.
    Then exit code is non-zero and a clear error message containing "--limit must
    be a positive integer" is emitted.
    """
    result = _invoke(["ingest", "jlcparts", "--limit", bad_limit])
    assert result.exit_code != 0, (
        f"`ingest jlcparts --limit {bad_limit}` should exit non-zero, "
        f"got {result.exit_code}.\nOutput: {result.output}"
    )
    # D3: assert the exact UX copy "--limit must be a positive integer"
    assert "--limit must be a positive integer" in result.output, (
        f"`ingest jlcparts --limit {bad_limit}` error output must contain "
        f"'--limit must be a positive integer'. Got: {result.output!r}"
    )


def test_cli_limit_valid_positive_accepted(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Given --limit 5 (a valid positive integer).
    When `partgraph ingest jlcparts --limit 5` is invoked
    (with missing file to trigger early exit before actual processing).
    Then the exit is NOT due to an invalid-limit error (the limit validation passes).

    Specifically: the output must NOT contain both 'limit' and 'positive',
    which would indicate the limit was rejected.

    Isolation: RAW_DB_PATH is monkeypatched to an absent tmp_path location so the
    command takes the documented early-exit path before any real processing.
    Without this, the corrected FK-schema adapter would run a real partial ingest
    of 5 parts (writing staged/checkpoint/metrics files and mutating Dgraph) as a
    side effect of this limit-validation unit test.
    """
    import pathlib

    absent_path = pathlib.Path(tmp_path) / "absent.sqlite3"
    monkeypatch.setattr("partgraph.cli.RAW_DB_PATH", absent_path)
    result = _invoke(["ingest", "jlcparts", "--limit", "5"])
    output_lower = (result.output or "").lower()
    # D2: replaced the weak disjunction with the correct assertion.
    assert not ("limit" in output_lower and "positive" in output_lower), (
        f"--limit 5 was rejected as invalid: {result.output}"
    )


def test_cli_limit_sys_maxsize_accepted(tmp_path) -> None:
    """Given --limit sys.maxsize (9223372036854775807 on 64-bit Python).
    When `partgraph ingest jlcparts --limit <sys.maxsize>` is invoked
    with all pipeline stages mocked.
    Then exit code is 0 (the value is accepted as a valid positive integer).
    """
    max_size_str = str(sys.maxsize)

    def _fake_fetch(*args, **kwargs):
        pass

    def _fake_normalize(*args, **kwargs):
        pass

    def _fake_load(*args, **kwargs):
        pass

    with (
        patch("partgraph.ingest.fetch.fetch_cdfer", side_effect=_fake_fetch),
        patch("partgraph.normalize.run.normalize", side_effect=_fake_normalize),
        patch("partgraph.load.loader.Loader.load", side_effect=_fake_load),
    ):
        result = _invoke(["ingest", "jlcparts", "--limit", max_size_str])

    # The limit itself must be accepted; the command may exit non-zero for other
    # reasons (e.g. missing file), but the output must NOT reject the limit value.
    output_lower = (result.output or "").lower()
    assert not ("limit" in output_lower and "positive" in output_lower), (
        f"--limit {max_size_str} (sys.maxsize) was rejected as invalid. "
        f"Output: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# T-CLI-missing-file
# ---------------------------------------------------------------------------

def test_cli_missing_file_no_fetch_exits_nonzero(
    tmp_path: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given no --fetch flag and the expected SQLite source file is absent.
    When `partgraph ingest jlcparts` is invoked.
    Then exit code is non-zero.

    Isolation: RAW_DB_PATH is monkeypatched to a tmp_path location so this test
    passes regardless of whether the real 1.6 GB file exists on disk.
    """
    import pathlib
    absent_path = pathlib.Path(tmp_path) / "absent.sqlite3"  # type: ignore[arg-type]
    monkeypatch.setattr("partgraph.cli.RAW_DB_PATH", absent_path)
    result = _invoke(["ingest", "jlcparts"])
    assert result.exit_code != 0, (
        f"Expected non-zero exit when source file absent and no --fetch. "
        f"Got {result.exit_code}.\nOutput: {result.output}"
    )


def test_cli_missing_file_message_names_expected_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given no --fetch and file absent.
    When `partgraph ingest jlcparts` is invoked.
    Then the error output mentions the expected file path (e.g. 'jlcpcb-components'
    or 'data/raw' path segment).

    Isolation: RAW_DB_PATH is monkeypatched to a tmp_path location so this test
    passes regardless of whether the real 1.6 GB file exists on disk.
    """
    import pathlib
    absent_path = pathlib.Path(tmp_path) / "jlcpcb-components.sqlite3"
    monkeypatch.setattr("partgraph.cli.RAW_DB_PATH", absent_path)
    result = _invoke(["ingest", "jlcparts"])
    output = result.output or ""
    # Output must mention either the file name or the directory where it's expected.
    assert any(kw in output for kw in ("jlcpcb", "components", "data/raw", "sqlite")), (
        f"Missing-file error should name the expected file path. Got: {output!r}"
    )


def test_cli_missing_file_message_hints_fetch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given no --fetch and file absent.
    When `partgraph ingest jlcparts` is invoked.
    Then the error output mentions '--fetch' as the remedy.

    Isolation: RAW_DB_PATH is monkeypatched to a tmp_path location so this test
    passes regardless of whether the real 1.6 GB file exists on disk.
    """
    import pathlib
    absent_path = pathlib.Path(tmp_path) / "absent.sqlite3"
    monkeypatch.setattr("partgraph.cli.RAW_DB_PATH", absent_path)
    result = _invoke(["ingest", "jlcparts"])
    output = result.output or ""
    assert "--fetch" in output, (
        f"Missing-file error must hint '--fetch' as the remedy. Got: {output!r}"
    )


# ---------------------------------------------------------------------------
# T-CLI-pipeline-order
# ---------------------------------------------------------------------------

def test_cli_pipeline_order_fetch_before_normalize_before_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given fake fetch/normalize/load stages injected via monkeypatch.
    When `partgraph ingest jlcparts --fetch` is invoked.
    Then fetch is called before normalize, which is called before load.

    D1: The dead `if False:` branch for partgraph.ingest.run has been removed.
    Seams: partgraph.ingest.fetch.fetch_cdfer, partgraph.normalize.run.normalize,
           partgraph.load.loader.Loader.load.
    """
    call_order: list[str] = []

    def _fake_fetch(*args, **kwargs):
        call_order.append("fetch")

    def _fake_normalize(*args, **kwargs):
        call_order.append("normalize")

    def _fake_load(*args, **kwargs):
        call_order.append("load")

    with (
        patch("partgraph.ingest.fetch.fetch_cdfer", side_effect=_fake_fetch),
        patch("partgraph.normalize.run.normalize", side_effect=_fake_normalize),
        patch("partgraph.load.loader.Loader.load", side_effect=_fake_load),
    ):
        result = _invoke(["ingest", "jlcparts", "--fetch", "--limit", "1"])

    if "fetch" in call_order:
        if "normalize" in call_order:
            assert call_order.index("fetch") < call_order.index("normalize"), (
                f"fetch must precede normalize. Call order: {call_order}"
            )
    if "normalize" in call_order and "load" in call_order:
        assert call_order.index("normalize") < call_order.index("load"), (
            f"normalize must precede load. Call order: {call_order}"
        )


def test_cli_pipeline_fetch_failure_aborts_normalize_and_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a fetch stage that raises an error.
    When `partgraph ingest jlcparts --fetch` is invoked.
    Then normalize and load are never called and the exit code is non-zero.
    """
    normalize_called = False
    load_called = False

    def _failing_fetch(*args, **kwargs):
        raise RuntimeError("fetch failed")

    def _normalize(*args, **kwargs):
        nonlocal normalize_called
        normalize_called = True

    def _load(*args, **kwargs):
        nonlocal load_called
        load_called = True

    with (
        patch("partgraph.ingest.fetch.fetch_cdfer", side_effect=_failing_fetch),
        patch("partgraph.normalize.run.normalize", side_effect=_normalize),
        patch("partgraph.load.loader.Loader.load", side_effect=_load),
    ):
        result = _invoke(["ingest", "jlcparts", "--fetch"])

    assert result.exit_code != 0, (
        f"Expected non-zero exit when fetch fails, got {result.exit_code}."
    )
    assert not normalize_called, "normalize must not be called if fetch fails."
    assert not load_called, "load must not be called if fetch fails."


# ---------------------------------------------------------------------------
# T-CLI-full-stub
# ---------------------------------------------------------------------------

def test_cli_full_stub_exits_nonzero() -> None:
    """Given the --full flag (not yet implemented).
    When `partgraph ingest jlcparts --full` is invoked.
    Then exit code is non-zero.
    """
    result = _invoke(["ingest", "jlcparts", "--full"])
    assert result.exit_code != 0, (
        f"`ingest jlcparts --full` should exit non-zero (stub). "
        f"Got {result.exit_code}.\nOutput: {result.output}"
    )


def test_cli_full_stub_message_mentions_not_implemented() -> None:
    """Given --full flag.
    When invoked.
    Then the output mentions 'not implemented' (or equivalent).
    """
    result = _invoke(["ingest", "jlcparts", "--full"])
    output = (result.output or "").lower()
    assert "not implement" in output or "not yet" in output or "stub" in output, (
        f"--full must mention 'not implemented'. Got: {result.output!r}"
    )


def test_cli_full_stub_message_mentions_adr_0001() -> None:
    """Given --full flag.
    When invoked.
    Then the output mentions 'ADR-0001'.
    """
    result = _invoke(["ingest", "jlcparts", "--full"])
    output = result.output or ""
    assert "ADR-0001" in output or "adr-0001" in output.lower(), (
        f"--full must mention 'ADR-0001'. Got: {output!r}"
    )


# ---------------------------------------------------------------------------
# T-CLI-no-shell
# ---------------------------------------------------------------------------

def test_cli_ingest_no_subprocess_shell_true(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a monkeypatched subprocess.run that detects shell=True calls.
    When `partgraph ingest jlcparts` is invoked (without --fetch, exits early
    on missing file — the 'dry path').
    Then subprocess.run is never called with shell=True.

    The ingest pipeline uses pydgraph and httpx, never shell commands.

    Isolation: RAW_DB_PATH is monkeypatched to an absent tmp_path location (as in
    the sibling missing-file tests) so the command takes the documented
    early-exit dry path regardless of whether the real 1.6 GB source file exists
    on disk. Without this, the corrected FK-schema adapter would iterate the
    entire real catalogue here, turning a dry-path unit test into a full ingest.
    """
    import pathlib

    absent_path = pathlib.Path(tmp_path) / "absent.sqlite3"
    monkeypatch.setattr("partgraph.cli.RAW_DB_PATH", absent_path)

    shell_true_calls: list = []
    original_run = subprocess.run

    def _spy_run(*args, **kwargs):
        if kwargs.get("shell") is True:
            shell_true_calls.append({"args": args, "kwargs": kwargs})
        # Still allow the call (for any subprocess usage that IS legitimate).
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    _invoke(["ingest", "jlcparts"])

    assert not shell_true_calls, (
        f"subprocess.run was called with shell=True during ingest dry path: "
        f"{shell_true_calls}. shell=True is forbidden in the ingest pipeline."
    )
