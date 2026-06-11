"""
Tests: R7

Verifies the typer-based CLI behaviour:
- `partgraph --help` exits 0.
- `db` command group exists with sub-commands: up, down, status, apply-schema.
- Each sub-command `--help` exits 0.
- Help text is in English (no non-ASCII characters that would indicate other
  languages; at minimum the word "Usage" appears).
- db up/down/status invoke subprocess.run with a LIST argv (no shell=True)
  containing `docker compose -f <repo>/docker/docker-compose.yml`.
- db down argv must NOT include "-v".
- apply-schema targets gRPC 127.0.0.1:9081 (verified via monkeypatched client
  or the GRPC_ADDR constant in partgraph.cli).

NOTE: These tests import from partgraph.cli.  Collection will ERROR if
partgraph is not installed — that is the correct red state before implementation.
"""

from __future__ import annotations

import pathlib
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Lazy import guard: CLI tests are expected to fail at COLLECTION if
# partgraph.cli does not exist yet.  We import at module level so the
# collection error surfaces immediately rather than being hidden in a skip.
# ---------------------------------------------------------------------------

from partgraph.cli import app  # noqa: E402 — intentional module-level import

# typer.testing is only available when typer is installed.
from typer.testing import CliRunner  # noqa: E402


RUNNER = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(args: list[str]):
    """Invoke the CLI app with the given args and return the result."""
    return RUNNER.invoke(app, args)


# ---------------------------------------------------------------------------
# R7 — top-level --help
# ---------------------------------------------------------------------------

def test_help_exits_zero() -> None:
    """Given the partgraph CLI application is installed.
    When we invoke `partgraph --help`.
    Then the exit code must be 0.
    """
    result = _invoke(["--help"])
    assert result.exit_code == 0, (
        f"`partgraph --help` exited {result.exit_code}.\nOutput:\n{result.output}"
    )


def test_help_output_contains_english_usage_keyword() -> None:
    """Given the partgraph CLI is installed.
    When we invoke `partgraph --help`.
    Then the output must contain the English word 'Usage' or 'usage'.
    """
    result = _invoke(["--help"])
    assert "sage" in result.output, (
        f"Help output does not appear to be in English. Got:\n{result.output}"
    )


def test_help_output_mentions_db_group() -> None:
    """Given the CLI is installed.
    When we invoke `partgraph --help`.
    Then 'db' must appear in the output as a command group.
    """
    result = _invoke(["--help"])
    assert "db" in result.output, (
        f"'db' command group not listed in --help. Output:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# R7 — db sub-commands exist and their --help exits 0
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sub_cmd",
    ["up", "down", "status", "apply-schema"],
)
def test_db_subcommand_help_exits_zero(sub_cmd: str) -> None:
    """Given the partgraph CLI is installed.
    When we invoke `partgraph db <sub_cmd> --help`.
    Then the exit code must be 0.
    """
    result = _invoke(["db", sub_cmd, "--help"])
    assert result.exit_code == 0, (
        f"`partgraph db {sub_cmd} --help` exited {result.exit_code}.\n"
        f"Output:\n{result.output}"
    )


@pytest.mark.parametrize(
    "sub_cmd",
    ["up", "down", "status", "apply-schema"],
)
def test_db_subcommand_help_is_in_english(sub_cmd: str) -> None:
    """Given the CLI is installed.
    When we invoke `partgraph db <sub_cmd> --help`.
    Then the output must contain the English word 'Usage' or 'usage'.
    """
    result = _invoke(["db", sub_cmd, "--help"])
    assert "sage" in result.output, (
        f"`db {sub_cmd} --help` output does not appear English. Got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# R7 — subprocess argv for db commands (monkeypatched subprocess.run)
# ---------------------------------------------------------------------------

def _repo_docker_compose_path(repo_root: pathlib.Path | None = None) -> str:
    """Return the absolute path that the CLI must pass to -f."""
    # We derive this from REPO_ROOT the same way conftest.py does.
    if repo_root is None:
        # fallback: derive from this file's location
        repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    return str(repo_root / "docker" / "docker-compose.yml")


@pytest.mark.parametrize("sub_cmd", ["up", "down", "status"])
def test_db_command_calls_subprocess_run_with_list_argv_no_shell(
    sub_cmd: str,
    repo_root: pathlib.Path,
) -> None:
    """Given the db sub-command delegates to Docker Compose.
    When we invoke `partgraph db <sub_cmd>` with subprocess.run monkeypatched.
    Then subprocess.run must be called with:
      - A list as the first argument (not a string).
      - shell=False (or shell keyword absent / False).
      - The list must contain 'docker', 'compose', '-f',
        and the absolute path to docker/docker-compose.yml.
    """
    compose_path = _repo_docker_compose_path(repo_root)
    mock_completed = MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=mock_completed) as mock_run:
        _invoke(["db", sub_cmd])

    assert mock_run.called, (
        f"`db {sub_cmd}` did not call subprocess.run at all."
    )
    positional_args, keyword_args = mock_run.call_args
    argv = positional_args[0] if positional_args else keyword_args.get("args")

    assert isinstance(argv, list), (
        f"`db {sub_cmd}` called subprocess.run with a non-list argv: {argv!r}. "
        "shell=True (string command) is forbidden."
    )
    assert keyword_args.get("shell", False) is False, (
        f"`db {sub_cmd}` called subprocess.run with shell=True. Forbidden."
    )
    assert "docker" in argv, f"'docker' not in argv for `db {sub_cmd}`: {argv}"
    assert "compose" in argv, f"'compose' not in argv: {argv}"
    assert "-f" in argv, f"'-f' flag not in argv: {argv}"
    assert compose_path in argv, (
        f"docker-compose.yml path '{compose_path}' not in argv: {argv}"
    )


def test_db_down_argv_does_not_contain_v_flag(repo_root: pathlib.Path) -> None:
    """Given `db down` must preserve the named volume.
    When we invoke `partgraph db down`.
    Then the subprocess.run argv must NOT contain '-v'.

    Including '-v' would delete the named volume and violate the G3 persistence
    contract.
    """
    mock_completed = MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=mock_completed) as mock_run:
        _invoke(["db", "down"])

    assert mock_run.called, "`db down` did not call subprocess.run."
    positional_args, keyword_args = mock_run.call_args
    argv = positional_args[0] if positional_args else keyword_args.get("args")

    assert "-v" not in argv, (
        f"`db down` argv contains '-v', which would delete the volume: {argv}"
    )


# ---------------------------------------------------------------------------
# R7 — apply-schema targets gRPC 127.0.0.1:9081
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Security: -f path passed to docker compose must be absolute
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sub_cmd", ["up", "down", "status"])
def test_db_command_compose_path_is_absolute(
    sub_cmd: str,
    repo_root: pathlib.Path,
) -> None:
    """Given the db sub-command invokes docker compose.
    When we capture the subprocess.run argv for db up/down/status.
    Then the path element immediately following the '-f' flag must be an
    absolute path (starts with '/'), preventing CWD-relative file injection.
    """
    mock_completed = MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=mock_completed) as mock_run:
        _invoke(["db", sub_cmd])

    assert mock_run.called, f"`db {sub_cmd}` did not call subprocess.run."
    positional_args, keyword_args = mock_run.call_args
    argv = positional_args[0] if positional_args else keyword_args.get("args")

    assert isinstance(argv, list), (
        f"`db {sub_cmd}` argv is not a list: {argv!r}"
    )
    assert "-f" in argv, f"'-f' flag not found in argv for `db {sub_cmd}`: {argv}"

    f_index = argv.index("-f")
    assert f_index + 1 < len(argv), (
        f"'-f' is the last element in argv with no following path: {argv}"
    )
    compose_path_arg = argv[f_index + 1]
    assert compose_path_arg.startswith("/"), (
        f"`db {sub_cmd}` passes a non-absolute path after '-f': {compose_path_arg!r}. "
        "The compose file path must be absolute to prevent CWD-relative injection."
    )


def test_apply_schema_targets_localhost_9081(repo_root: pathlib.Path) -> None:
    """Given apply-schema must connect to Dgraph via gRPC.
    When we invoke `partgraph db apply-schema` with pydgraph monkeypatched.
    Then the DgraphClientStub (or equivalent) must be constructed with
    '127.0.0.1:9081' as the address.

    Verification strategy: monkeypatch pydgraph.DgraphClientStub at the
    import site used by partgraph.cli, then assert the first positional arg
    to the constructor is the expected address.
    """
    # We import the module so we know where to patch.
    import partgraph.cli as cli_module  # noqa: PLC0415

    # The CLI may reference pydgraph directly or store the address as a constant.
    # Check for a module-level constant first (most testable design).
    if hasattr(cli_module, "DGRAPH_GRPC_ADDR"):
        assert cli_module.DGRAPH_GRPC_ADDR == "127.0.0.1:9081", (
            f"DGRAPH_GRPC_ADDR must be '127.0.0.1:9081', got '{cli_module.DGRAPH_GRPC_ADDR}'"
        )
        return

    # Fallback: monkeypatch pydgraph.DgraphClientStub and capture constructor call.
    pydgraph = pytest.importorskip(
        "pydgraph",
        reason="pydgraph not installed; cannot verify apply-schema gRPC address via stub.",
    )

    mock_stub = MagicMock()
    mock_client = MagicMock()
    mock_txn = MagicMock()
    mock_client.txn.return_value.__enter__ = MagicMock(return_value=mock_txn)
    mock_client.txn.return_value.__exit__ = MagicMock(return_value=False)

    stub_calls = []

    def capturing_stub(addr, *args, **kwargs):
        stub_calls.append(addr)
        return mock_stub

    with (
        patch.object(pydgraph, "DgraphClientStub", side_effect=capturing_stub),
        patch.object(pydgraph, "DgraphClient", return_value=mock_client),
    ):
        _invoke(["db", "apply-schema"])

    assert stub_calls, (
        "`db apply-schema` did not construct a DgraphClientStub — "
        "cannot verify gRPC address."
    )
    assert stub_calls[0] == "127.0.0.1:9081", (
        f"apply-schema connected to '{stub_calls[0]}' instead of '127.0.0.1:9081'"
    )
