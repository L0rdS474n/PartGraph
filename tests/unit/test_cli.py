"""
Tests: R7

Verifies the typer-based CLI behaviour:
- `partgraph --help` exits 0.
- `db` command group exists with sub-commands: up, down, status, apply-schema.
- Each sub-command `--help` exits 0.
- Help text is in English (no non-ASCII characters that would indicate other
  languages; at minimum the word "Usage" appears).
- db up/down/status invoke subprocess.run with a LIST argv (no shell=True)
  whose prefix is the detected container engine + `compose` (from
  partgraph.util.compose_command, so docker OR podman works), followed by
  `-f <repo>/docker/docker-compose.yml`.
- db down argv must NOT include "-v".
- apply-schema targets gRPC 127.0.0.1:9081 (verified via monkeypatched client
  or the GRPC_ADDR constant in partgraph.cli).

NOTE: These tests import from partgraph.cli.  Collection will ERROR if
partgraph is not installed — that is the correct red state before implementation.

PR-A (fix/db-down-all-instances) UPDATE: `db down` no longer issues exactly
ONE subprocess.run call. It now sweeps every PartGraph lifecycle owner
(Compose AND the quadlet/systemd unit `partgraph-dgraph.service`), so it
issues a systemd unit-state query, the Compose `down` call, a container
enumeration, and a verification re-enumeration — several subprocess.run
calls in one run (see tests/unit/test_lifecycle.py and
tests/unit/test_cli_db_down.py for the full PR-A contract). The two shared
argv-shape tests below (parametrized over ["up", "down"]) therefore:
  - select the COMPOSE call OUT OF `mock_run.call_args_list` (never assume
    it is the LAST call — for `down` the last call is now the verification
    re-enumeration, not compose);
  - additionally assert list-argv + shell=False for EVERY recorded call,
    not just the compose one.
`db down` also now resolves a SECOND, bare engine prefix via the NEW
`partgraph.cli.engine_command` import (used for the enumeration/stop sweep,
independently of `compose_command`'s compose-plugin prefix), and calls
`partgraph.cli.probe_health` (the SAME module-level reference `db status`
already uses) for its A9 advisory check — both are stubbed below
(`stub_engine_command` / `stub_probe_health`) so these argv-shape tests stay
hermetic (no real PATH lookup, no real HTTP request).
"""

from __future__ import annotations

import pathlib
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Lazy import guard: CLI tests are expected to fail at COLLECTION if
# partgraph.cli does not exist yet.  We import at module level so the
# collection error surfaces immediately rather than being hidden in a skip.
# ---------------------------------------------------------------------------

from partgraph.cli import app  # noqa: E402 — intentional module-level import
from partgraph.util.container import ContainerEngineError  # noqa: E402

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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_compose_command() -> list[str]:
    """Stub partgraph.cli.compose_command to return a fixed deterministic argv prefix.

    Given: partgraph.cli imports compose_command from partgraph.util.container.
    When: any test requests this fixture.
    Then: compose_command() returns ["docker", "compose"] so argv-level assertions
          are engine-agnostic and hermetic on CI runners without a container engine
          installed.

    Yields the stubbed prefix list so tests can assert on it directly, and raises
    AttributeError at fixture-setup time if partgraph.cli has not yet imported
    compose_command — the correct test-first red state.
    """
    with patch("partgraph.cli.compose_command", return_value=["docker", "compose"]):
        yield ["docker", "compose"]


@pytest.fixture
def stub_engine_command() -> list[str]:
    """Stub partgraph.cli.engine_command to return a fixed deterministic argv prefix.

    Given: PR-A's `db down` resolves a SECOND, bare engine prefix (for its
    container enumeration / stop sweep) independently of `compose_command`'s
    compose-plugin prefix.
    When: any test requests this fixture.
    Then: engine_command() returns ["docker"] so no test depends on a real
    container engine being on PATH. Harmless (unused) for `db up`, which
    never calls engine_command.
    """
    with patch("partgraph.cli.engine_command", return_value=["docker"]):
        yield ["docker"]


@pytest.fixture
def stub_probe_health():
    """Stub partgraph.cli.probe_health so `db down` never opens a real socket.

    Given: PR-A's `db down` calls probe_health() for its A9 advisory check
    (the same module-level reference `db status` already uses, ADR-0018).
    When: any test requests this fixture.
    Then: probe_health() returns a fixed, unhealthy HealthResult-shaped stub
    — no real HTTP request to 127.0.0.1:8081/health is ever attempted.
    Harmless (unused) for `db up`.
    """
    fake_result = SimpleNamespace(healthy=False, message="stub: not probed")
    with patch("partgraph.cli.probe_health", return_value=fake_result) as mock_probe:
        yield mock_probe


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


# `status` no longer delegates to Compose — it is engine-independent via an
# HTTP health probe (see partgraph.util.health.probe_health / ADR-0018).
@pytest.mark.parametrize("sub_cmd", ["up", "down"])
def test_db_command_calls_subprocess_run_with_list_argv_no_shell(
    sub_cmd: str,
    repo_root: pathlib.Path,
    stub_compose_command: list[str],
    stub_engine_command: list[str],
    stub_probe_health,
) -> None:
    """Given the db sub-command delegates to Docker Compose.
    When we invoke `partgraph db <sub_cmd>` with subprocess.run monkeypatched
    and compose_command()/engine_command()/probe_health() stubbed.
    Then EVERY recorded subprocess.run call must use:
      - A list as the first argument (not a string).
      - shell=False (or shell keyword absent / False).
    And the ONE call whose argv contains "compose" (PR-A: `db down` now
    issues several subprocess.run calls — a systemd unit-state query, the
    compose call, a container enumeration, and a verification
    re-enumeration — so the compose call is no longer necessarily the LAST
    one) must have:
      - Its first two elements equal to compose_command()'s return value,
        i.e. the stub_compose_command fixture value (an engine-agnostic prefix).
      - '-f' and the absolute path to docker/docker-compose.yml in its argv.

    The stub_compose_command/stub_engine_command fixtures ensure this test is
    hermetic: it passes on a CI runner that has no container engine
    installed, because neither compose_command() nor engine_command() is
    ever called for real.
    """
    compose_path = _repo_docker_compose_path(repo_root)
    mock_completed = MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=mock_completed) as mock_run:
        _invoke(["db", sub_cmd])

    assert mock_run.called, (
        f"`db {sub_cmd}` did not call subprocess.run at all."
    )

    compose_calls: list[list[str]] = []
    for recorded_call in mock_run.call_args_list:
        positional_args, keyword_args = recorded_call
        argv = positional_args[0] if positional_args else keyword_args.get("args")
        assert isinstance(argv, list), (
            f"`db {sub_cmd}` called subprocess.run with a non-list argv: {argv!r}. "
            "shell=True (string command) is forbidden."
        )
        assert keyword_args.get("shell", False) is False, (
            f"`db {sub_cmd}` called subprocess.run with shell=True. Forbidden."
        )
        if "compose" in argv:
            compose_calls.append(argv)

    assert compose_calls, (
        f"`db {sub_cmd}` never invoked the compose command at all. "
        f"All calls: {mock_run.call_args_list!r}"
    )
    argv = compose_calls[0]
    assert argv[:2] == stub_compose_command, (
        f"`db {sub_cmd}` argv prefix must equal compose_command()'s return value "
        f"{stub_compose_command!r}, got: {argv[:2]!r}. "
        "The CLI must build argv from compose_command(), not a hard-coded engine name."
    )
    assert "-f" in argv, f"'-f' flag not in argv: {argv}"
    assert compose_path in argv, (
        f"docker-compose.yml path '{compose_path}' not in argv: {argv}"
    )


def test_db_down_argv_does_not_contain_v_flag(
    repo_root: pathlib.Path,
    stub_compose_command: list[str],
    stub_engine_command: list[str],
    stub_probe_health,
) -> None:
    """Given `db down` must preserve the named volume.
    When we invoke `partgraph db down` with compose_command()/
    engine_command()/probe_health() stubbed.
    Then NO subprocess.run call anywhere in the run (PR-A: `db down` now
    issues several calls, not just one) may contain '-v'.

    Including '-v' would delete the named volume and violate the G3 persistence
    contract.  The stub fixtures make this hermetic on CI.
    """
    mock_completed = MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=mock_completed) as mock_run:
        _invoke(["db", "down"])

    assert mock_run.called, "`db down` did not call subprocess.run."
    for recorded_call in mock_run.call_args_list:
        positional_args, keyword_args = recorded_call
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

# `status` no longer delegates to Compose — it is engine-independent via an
# HTTP health probe (see partgraph.util.health.probe_health / ADR-0018).
@pytest.mark.parametrize("sub_cmd", ["up", "down"])
def test_db_command_compose_path_is_absolute(
    sub_cmd: str,
    repo_root: pathlib.Path,
    stub_compose_command: list[str],
    stub_engine_command: list[str],
    stub_probe_health,
) -> None:
    """Given the db sub-command invokes docker compose with compose_command() stubbed.
    When we capture the subprocess.run argv for db up/down (status no longer
    delegates to Compose — ADR-0018).
    Then the path element immediately following the '-f' flag, in the ONE
    call whose argv contains "compose" (PR-A: `db down` now issues several
    subprocess.run calls, so the compose call is no longer necessarily the
    LAST one — selected out of call_args_list instead), must be an absolute
    path (starts with '/'), preventing CWD-relative file injection.

    The stub_compose_command/stub_engine_command fixtures make this hermetic
    on engine-less CI.
    """
    mock_completed = MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", return_value=mock_completed) as mock_run:
        _invoke(["db", sub_cmd])

    assert mock_run.called, f"`db {sub_cmd}` did not call subprocess.run."

    compose_calls: list[list[str]] = []
    for recorded_call in mock_run.call_args_list:
        positional_args, keyword_args = recorded_call
        argv = positional_args[0] if positional_args else keyword_args.get("args")
        assert isinstance(argv, list), (
            f"`db {sub_cmd}` argv is not a list: {argv!r}"
        )
        if "compose" in argv:
            compose_calls.append(argv)

    assert compose_calls, f"`db {sub_cmd}` never invoked the compose command at all."
    argv = compose_calls[0]
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


# ---------------------------------------------------------------------------
# AC-5 — db up exits cleanly when no container engine is available
# ---------------------------------------------------------------------------

def test_db_up_exits_cleanly_when_no_engine_available() -> None:
    """Given partgraph.cli.compose_command raises ContainerEngineError (no engine on PATH).
    When we invoke `partgraph db up`.
    Then:
    - exit_code is 1 (non-zero, standard CLI error code).
    - output contains the word "Error" — a human-readable message, not a raw
      Python traceback (which would expose internal module paths).
    - result.exception is NOT a ContainerEngineError: the CLI must catch the
      engine-detection failure and convert it to a typer.Exit(code=1) so no
      unhandled exception propagates to the user's terminal.

    This test is deliberately engine-agnostic: it patches compose_command() at the
    partgraph.cli namespace level and therefore requires that cli.py imports
    compose_command — the correct test-first red state before implementation.
    """
    with patch(
        "partgraph.cli.compose_command",
        side_effect=ContainerEngineError("no engine"),
    ):
        result = _invoke(["db", "up"])

    assert result.exit_code == 1, (
        f"`db up` with no engine available should exit 1, got {result.exit_code}.\n"
        f"Output:\n{result.output!r}"
    )
    assert "Error" in result.output, (
        "`db up` with no engine should print a human-readable 'Error' message "
        "(not a raw traceback). "
        f"Got output:\n{result.output!r}"
    )
    # ContainerEngineError must NOT leak as an unhandled exception.
    # When properly handled: result.exception is None or SystemExit (from typer.Exit).
    # When leaked (bug): result.exception is a ContainerEngineError instance.
    if result.exception is not None:
        assert not isinstance(result.exception, ContainerEngineError), (
            "ContainerEngineError leaked as an unhandled exception. "
            "It must be caught in _run_compose (or db up) and re-raised as "
            "typer.Exit(code=1) so no traceback is printed to the user."
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
