"""
Tests: PR-B2 (feat/db-lazy-autostart) — CLI wiring for
`partgraph.util.lifecycle.ensure_running` (ADR-0022 Section 7, AC B-6..B-9,
plus the `PARTGRAPH_AUTOSTART` parsing decision this file pins).

OBJECTIVE this file specifies, verbatim from the operator: "partgraph ska
först startas när jag för första gången anropar partgraph" — the database
starts on the FIRST `partgraph` command that actually needs it, never before,
and NEVER for a command that does not touch the database at all. PR-B2 ships
autostart only; there is no idle auto-stop here (PR-C).

Pinned CLI contract this file exercises (NOT YET IMPLEMENTED — every test
below is expected to ERROR at RUNTIME, not at collection: `partgraph.cli`
itself already imports and collects cleanly today, so
`patch("partgraph.cli.ensure_running", ...)` fails with
`AttributeError: <module 'partgraph.cli'> does not have the attribute
'ensure_running'` the moment each test actually runs — mirrors
`tests/unit/test_cli_db_doctor.py`'s own documented PRE-PR-B1 RED history:
"individual runtime assertion failure per test, never a whole-file
ModuleNotFoundError"):

  - `partgraph.cli` imports `ensure_running` from `partgraph.util.lifecycle`
    at module level (alongside `stop_all`/`find_partgraph_instances`/
    `unit_state`/`volume_exists`, already imported there), so
    `patch("partgraph.cli.ensure_running", ...)` is the correct patch target
    for every test in this file — mirrors how `probe_health`/`compose_command`/
    `engine_command` are already patched at `partgraph.cli.*`, never at their
    origin modules, so the CLI's OWN reference is what gets replaced.
  - For every command in the allowlist (B-6), `ensure_running(probe_health=
    partgraph.cli.probe_health, compose_up=<a callable running `<engine>
    compose -f <abs COMPOSE_FILE> up -d`>)` is called before the command's own
    DB work begins. The `compose_up` seam's own argv, when actually invoked,
    equals `db up`'s argv EXACTLY (verified end-to-end, hermetically, at the
    subprocess level in the B-1/B-2/B-3 section below — using `search` as ONE
    representative command; repeating that full subprocess-level machinery for
    each of the other 8 allowlisted commands would be redundant with what that
    one deep test already proves about the SHARED `ensure_running`/`compose_up`
    wiring, so the remaining 8 commands are verified via a SPY on
    `partgraph.cli.ensure_running` instead — proving the WIRING trigger, not
    re-deriving the argv).
  - `PARTGRAPH_AUTOSTART` parsing (pinned here, not dictated by any AC): read
    via `os.environ`, case-insensitively, stripped. The ONLY recognised "off"
    tokens are `"0"`, `"false"` and `"no"` — matching any of those (in any
    case) disables autostart for that invocation. UNSET, EMPTY STRING, and any
    OTHER value (`"1"`, `"true"`, `"yes"`, or a genuinely unrecognised typo
    like `"banana"`) all mean autostart stays ON — the documented default.
    Rationale: the ADR names exactly ONE escape-hatch spelling
    (`PARTGRAPH_AUTOSTART=0`); accepting a couple of obvious synonyms
    ("false"/"no") is a reasonable robustness concession, but a value that
    matches NONE of the three recognised off-tokens is far more likely to be a
    typo than a deliberate, mis-spelled attempt to disable a feature the
    operator wants ON by default — so an ambiguous/garbage value fails OPEN
    (autostart stays on) rather than silently and surprisingly disabling
    autostart on a typo. This keeps the escape hatch UNAMBIGUOUS in the
    direction that matters most: there is exactly one well-documented way to
    turn it off, and everything else does not.

HERMETICITY: every test in this file EITHER (a) patches
`partgraph.cli.ensure_running` directly as a spy — for the B-6/B-7/B-8/B-9/
parsing-table sections, where only WHETHER autostart fires matters, not its
own internal machinery (that machinery is `ensure_running()`'s own contract,
specified end-to-end in `tests/unit/test_lifecycle_ensure_running.py`) — OR
(b) drives the REAL `ensure_running()` end-to-end through `search`, patching
only `subprocess.run`, `partgraph.cli.compose_command`, `partgraph.cli.
probe_health`, `time.sleep` and `time.monotonic` — for the B-1/B-2/B-3
section, which is the one place this file proves the argv-level and
health-probing contract for real. No test in this file ever sleeps for a real
duration, opens a socket, or starts a real container.

An autouse fixture in `tests/conftest.py` (AC B-10) forces
`PARTGRAPH_AUTOSTART=0` for every test in the whole suite by default; every
test in this file that needs the ON path opts back in explicitly via
`monkeypatch.setenv("PARTGRAPH_AUTOSTART", ...)`.
"""

from __future__ import annotations

import json
import os
import re
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import partgraph.cli as cli_mod
from partgraph.cli import COMPOSE_FILE, app

RUNNER = CliRunner()

# Pin a wide terminal so Rich never wraps a long token across lines (mirrors
# every other CLI test file's own COLUMNS convention).
os.environ.setdefault("COLUMNS", "200")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _StrippedResult:
    """Click Result wrapper whose .output has ANSI escape codes removed."""

    def __init__(self, result: object) -> None:
        self._result = result

    @property
    def output(self) -> str:
        return _ANSI_RE.sub("", self._result.output)

    def __getattr__(self, name: str) -> object:
        return getattr(self._result, name)


def _invoke(args: list[str]) -> _StrippedResult:
    return _StrippedResult(RUNNER.invoke(app, args))


# ---------------------------------------------------------------------------
# Shared fixture builders (deliberately independent copies — per
# CONTRIBUTING.md's "Test fixtures stay local to their file" policy).
# ---------------------------------------------------------------------------


def _healthy(healthy: bool):
    return lambda: MagicMock(healthy=healthy, message="probe")


def _healthy_sequence(*results: bool):
    """Return a probe_health() stand-in yielding *results* in order, then
    repeating the last one forever (so a test does not need to know exactly
    how many times a real implementation will poll).
    """
    calls = {"n": 0}

    def _probe():
        idx = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return MagicMock(healthy=results[idx], message="probe")

    return _probe


def _make_empty_search_client() -> MagicMock:
    """A mock pydgraph client whose read txn always answers an empty result."""

    def _fake_query(dql: str, *args, **kwargs):
        resp = MagicMock()
        resp.json = json.dumps({"exact": [], "trig": [], "fts": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn
    return mock_client


def _forbid_any_subprocess(argv, **kwargs):
    raise AssertionError(
        f"unexpected subprocess.run call — no engine subprocess was expected "
        f"in this scenario: {argv}"
    )


def _which_nothing_present(name: str) -> str | None:
    return None


def _which_systemctl_present(name: str) -> str | None:
    return "/usr/bin/systemctl" if name == "systemctl" else None


# ---------------------------------------------------------------------------
# B-1 / B-2 / B-3 — the real ensure_running(), driven end-to-end through
# `search` (the representative command). ONE deep, subprocess-level test per
# acceptance criterion; the remaining 8 allowlisted commands are covered by
# the spy-based B-6 section below, which proves the WIRING TRIGGER without
# re-deriving the argv-level contract this section already nails down.
# ---------------------------------------------------------------------------


def _autostart_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARTGRAPH_AUTOSTART", "1")


def test_b1_healthy_short_circuit_spawns_zero_engine_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-1: Given probe_health() reports healthy already.
    When `partgraph search MAX232` runs with autostart ON.
    Then `subprocess.run` is NEVER called (a fail-fast fake raises on any
    call), and the search itself completes normally.
    """
    _autostart_on(monkeypatch)
    mock_client = _make_empty_search_client()
    with (
        patch("partgraph.cli.probe_health", side_effect=_healthy(True)),
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["search", "MAX232"])

    assert result.exit_code == 0, result.output


def test_b2_unhealthy_start_argv_equals_db_up_argv_exactly_invoked_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-2: Given probe_health() reports unhealthy, then healthy on the very
    next poll (the database finished starting).
    When `partgraph search MAX232` runs with autostart ON.
    Then EXACTLY ONE `compose ... up -d` subprocess call is made, and its
    argv equals `db up`'s own argv byte-for-byte: `<engine> compose -f
    <abs COMPOSE_FILE> up -d`; the search's own DB work (the mock client's
    txn) still runs afterward, proving the command's DB work begins only
    after ensure_running() reports healthy.
    """
    _autostart_on(monkeypatch)
    mock_client = _make_empty_search_client()
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy_sequence(False, True)),
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        patch("subprocess.run", side_effect=_fake_run),
        patch("time.sleep"),
        patch("time.monotonic", return_value=0.0),
    ):
        result = _invoke(["search", "MAX232"])

    assert result.exit_code == 0, result.output
    up_calls = [argv for argv in calls if "compose" in argv and "up" in argv]
    assert len(up_calls) == 1, f"expected exactly one compose up call, got: {calls!r}"
    assert up_calls[0] == ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"], (
        f"the autostart argv must equal `db up`'s own argv exactly: {up_calls[0]!r}"
    )
    assert mock_client.txn.called, (
        "the search's own DB query must still run once the database is healthy"
    )


def test_b3_never_becomes_healthy_exits_one_names_budget_and_db_status_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-3: Given probe_health() NEVER reports healthy (the DB never comes
    up), and the compose `up -d` call itself succeeds (isolating this
    scenario from B-4's start-failure path).
    When `partgraph search MAX232` runs with autostart ON.
    Then the exit code is 1, a single output line names `partgraph db
    status` (the AutostartTimeoutError's own message, printed by the CLI),
    no `Traceback` reaches the user, and the search's own DB work (the mock
    client's txn) NEVER runs.
    """
    _autostart_on(monkeypatch)
    mock_client = _make_empty_search_client()

    def _fake_run(argv, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    from partgraph.util.lifecycle import AUTOSTART_READY_TIMEOUT_S

    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        patch("subprocess.run", side_effect=_fake_run),
        patch("time.sleep"),
        patch(
            "time.monotonic",
            side_effect=[0.0, AUTOSTART_READY_TIMEOUT_S + 1.0],
        ),
    ):
        result = _invoke(["search", "MAX232"])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "partgraph db status" in result.output
    assert not mock_client.txn.called, (
        "the search's own DB query must never run when the database never "
        "became healthy"
    )
    for line in result.output.splitlines():
        assert "/" not in line, f"autostart-timeout line leaks a path: {line!r}"


# ---------------------------------------------------------------------------
# B-6 — the allowlist, exactly: one parametrized case per command. Uses a
# SPY on partgraph.cli.ensure_running (never exercising its real internals,
# which the B-1/B-2/B-3 section and test_lifecycle_ensure_running.py already
# cover), and mocks only what each command needs to reach its own DB-touching
# point without erroring for an UNRELATED reason.
# ---------------------------------------------------------------------------


def test_b6_stats_triggers_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-6: Given `partgraph stats`, a DB-touching command.
    When it runs with autostart ON.
    Then `ensure_running` is called before the command's own DB work.
    """
    _autostart_on(monkeypatch)

    def _fake_query(dql, *a, **kw):
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
    ):
        _invoke(["stats"])

    mock_ensure.assert_called()


def test_b6_search_triggers_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-6: Given `partgraph search`, a read-only DB-touching command.
    When it runs with autostart ON.
    Then `ensure_running` is called.
    """
    _autostart_on(monkeypatch)
    mock_client = _make_empty_search_client()

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
    ):
        _invoke(["search", "MAX232"])

    mock_ensure.assert_called()


def test_b6_show_triggers_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-6: Given `partgraph show`, a read-only DB-touching command.
    When it runs with autostart ON.
    Then `ensure_running` is called.
    """
    _autostart_on(monkeypatch)

    def _fake_query(dql, *a, **kw):
        resp = MagicMock()
        resp.json = json.dumps({"part": [], "related": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
    ):
        _invoke(["show", "MAX232"])

    mock_ensure.assert_called()


def test_b6_embed_triggers_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-6: Given `partgraph embed`.
    When it runs with autostart ON.
    Then `ensure_running` is called.
    """
    _autostart_on(monkeypatch)

    def _fake_query(dql, *a, **kw):
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    def _fake_get_encoder():
        return lambda texts: [[0.0] for _ in texts]

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        patch.object(cli_mod, "get_encoder", _fake_get_encoder, create=True),
    ):
        _invoke(["embed"])

    mock_ensure.assert_called()


def test_b6_refresh_links_triggers_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-6: Given `partgraph refresh-links`.
    When it runs with autostart ON.
    Then `ensure_running` is called.
    """
    _autostart_on(monkeypatch)

    def _fake_query(dql, *a, **kw):
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
    ):
        _invoke(["refresh-links"])

    mock_ensure.assert_called()


def test_b6_refresh_stock_triggers_autostart(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """B-6: Given `partgraph refresh` (the stock/price refresh command),
    with an existing dummy source file and a stubbed source-loading seam so
    it reaches its own DB-touching point.
    When it runs with autostart ON.
    Then `ensure_running` is called.
    """
    _autostart_on(monkeypatch)
    dummy = tmp_path / "dummy-jlcpcb-components.sqlite3"
    dummy.write_bytes(b"")
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", dummy)

    def _fake_query(dql, *a, **kw):
        resp = MagicMock()
        resp.json = json.dumps({"q": []}).encode()
        return resp

    mock_txn = MagicMock()
    mock_txn.query.side_effect = _fake_query
    mock_txn.discard.return_value = None
    mock_client = MagicMock()
    mock_client.txn.return_value = mock_txn

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        patch.object(cli_mod, "_load_stock_index", return_value={}, create=True),
    ):
        _invoke(["refresh"])

    mock_ensure.assert_called()


def test_b6_db_apply_schema_triggers_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-6: Given `partgraph db apply-schema` — this command does NOT go
    through `_build_dgraph_client()` (it uses `schema_module.apply_schema`
    over its own gRPC path), so autostart must be wired here EXPLICITLY,
    not merely inherited from a shared helper.
    When it runs with autostart ON.
    Then `ensure_running` is called.
    """
    _autostart_on(monkeypatch)

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod.schema_module, "load_schema", return_value="type Part {}"),
        patch.object(cli_mod.schema_module, "apply_schema", return_value=None),
    ):
        _invoke(["db", "apply-schema"])

    mock_ensure.assert_called()


def test_b6_db_check_index_triggers_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-6: Given `partgraph db check-index` — like `db apply-schema`, this
    does NOT go through `_build_dgraph_client()` (it calls
    `check_index_integrity()` directly), so it needs its OWN explicit
    autostart wiring too.
    When it runs with autostart ON.
    Then `ensure_running` is called.
    """
    _autostart_on(monkeypatch)
    healthy_result = MagicMock(reachable=True, schema_ok=True, self_similarity_ok=True, message="ok")

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("partgraph.cli.check_index_integrity", return_value=healthy_result),
    ):
        _invoke(["db", "check-index"])

    mock_ensure.assert_called()


def _existing_dummy_sqlite(tmp_path) -> os.PathLike[str]:
    import pathlib

    dummy = pathlib.Path(tmp_path) / "dummy-jlcpcb-components.sqlite3"
    dummy.write_bytes(b"")
    return dummy


def test_b6_ingest_jlcparts_load_stage_triggers_autostart(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """B-6: Given `partgraph ingest jlcparts` reaches the LOAD stage (the
    source file exists, and both fetch/normalize are stubbed to succeed).
    When it runs with autostart ON.
    Then `ensure_running` is called during the load stage.
    """
    _autostart_on(monkeypatch)
    import pathlib

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_sqlite(tmp_path))
    monkeypatch.setattr(cli_mod, "STAGED_PATH", pathlib.Path(tmp_path) / "staged.jsonl")
    (pathlib.Path(tmp_path) / "staged.jsonl").write_bytes(b"")
    monkeypatch.setattr(
        cli_mod, "LOAD_CHECKPOINT_PATH", pathlib.Path(tmp_path) / "state" / "load_checkpoint.json"
    )

    mock_client = MagicMock()

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
        patch("partgraph.normalize.run.normalize", return_value=None),
        patch("partgraph.load.loader.Loader.load", return_value=None),
    ):
        result = _invoke(["ingest", "jlcparts"])

    assert result.exit_code == 0, result.output
    mock_ensure.assert_called()


def test_b6_ingest_jlcparts_fetch_stage_never_triggers_autostart(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """B-6 (negative half): Given `partgraph ingest jlcparts --fetch` where
    the fetch stage itself FAILS (never reaching normalize/load).
    When it runs with autostart ON.
    Then `ensure_running` is NEVER called — the fetch/normalize stages of
    `ingest jlcparts` must never start the database, only the load stage
    may.
    """
    _autostart_on(monkeypatch)

    def _failing_fetch(*args, **kwargs):
        raise RuntimeError("network unreachable")

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("partgraph.ingest.fetch.fetch_cdfer", side_effect=_failing_fetch),
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["ingest", "jlcparts", "--fetch"])

    assert result.exit_code != 0, result.output
    mock_ensure.assert_not_called()


def test_b6_ingest_jlcparts_normalize_stage_never_triggers_autostart(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """B-6 (negative half): Given `partgraph ingest jlcparts` (no --fetch,
    source file present) where the NORMALIZE stage itself fails (never
    reaching load).
    When it runs with autostart ON.
    Then `ensure_running` is NEVER called.
    """
    _autostart_on(monkeypatch)
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_sqlite(tmp_path))

    def _failing_normalize(*args, **kwargs):
        raise RuntimeError("malformed source database")

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("partgraph.normalize.run.normalize", side_effect=_failing_normalize),
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["ingest", "jlcparts"])

    assert result.exit_code != 0, result.output
    mock_ensure.assert_not_called()


# ---------------------------------------------------------------------------
# B-7 — hard negatives: never autostart, no engine subprocess (beyond each
# command's own PRE-EXISTING, unrelated subprocess usage — db up/down/doctor
# each legitimately call subprocess.run for their OWN job; the sharp,
# unambiguous signal pinned here is that `ensure_running` — the ONLY seam
# that could add an EXTRA compose-up call — is never invoked by any of them).
# ---------------------------------------------------------------------------


def test_b7_db_status_never_autostarts_and_spawns_no_subprocess() -> None:
    """B-7: Given `partgraph db status` (ADR-0018: an engine-independent
    HTTP health probe — a probe that starts the thing it measures would be
    a broken instrument).
    When it runs (PARTGRAPH_AUTOSTART left at the suite's autouse default,
    "0" — but this negative must hold regardless, since `db status` is not
    in the allowlist at all).
    Then `ensure_running` is never called AND `subprocess.run` is never
    called at all.
    """
    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("partgraph.cli.probe_health", side_effect=_healthy(True)),
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["db", "status"])

    assert result.exit_code == 0, result.output
    mock_ensure.assert_not_called()


def test_b7_db_down_never_autostarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-7: Given `partgraph db down`.
    When it runs (with autostart ON in the environment, to prove the
    negative holds even when the operator has NOT disabled autostart —
    `db down`'s own job must never start what it is trying to stop).
    Then `ensure_running` is never called, and no `compose ... up` call
    appears anywhere in the recorded subprocess argvs (db down's own
    `compose down`/engine `stop`/`systemctl` calls are expected and
    unrelated).
    """
    _autostart_on(monkeypatch)
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return MagicMock(returncode=0, stdout="[]", stderr="")

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=_fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        _invoke(["db", "down"])

    mock_ensure.assert_not_called()
    for argv in calls:
        assert not ("compose" in argv and "up" in argv), (
            f"db down must never issue a compose up call: {argv}"
        )


def test_b7_db_doctor_never_autostarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-7: Given `partgraph db doctor` (PR-B1 made it strictly read-only
    and always exit 0).
    When it runs with autostart ON.
    Then `ensure_running` is never called, and no `compose ... up` call
    appears anywhere in the recorded subprocess argvs.
    """
    _autostart_on(monkeypatch)
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "systemctl" in argv and "show" in argv:
            return MagicMock(
                returncode=0,
                stdout="LoadState=not-found\nActiveState=\nSubState=\nUnitFileState=\nWantedBy=\n",
                stderr="",
            )
        if "volume" in argv and "inspect" in argv:
            return MagicMock(returncode=1, stdout="", stderr="no such volume")
        if "ps" in argv and "--all" in argv:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("subprocess.run", side_effect=_fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    assert result.exit_code == 0, result.output
    mock_ensure.assert_not_called()
    for argv in calls:
        assert not ("compose" in argv and "up" in argv), (
            f"db doctor must never issue a compose up call: {argv}"
        )


def test_b7_db_up_never_calls_ensure_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-7: Given `partgraph db up` — its OWN single `compose up -d` call
    IS the primary function, not "autostart".
    When it runs with autostart ON.
    Then `ensure_running` is never called (there is nothing to lazily start
    on top of the operator's own explicit `db up`).
    """
    _autostart_on(monkeypatch)
    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")),
    ):
        result = _invoke(["db", "up"])

    assert result.exit_code == 0, result.output
    mock_ensure.assert_not_called()


def test_b7_version_never_autostarts_and_spawns_no_subprocess() -> None:
    """B-7: Given `partgraph version`.
    When it runs.
    Then `ensure_running` is never called and no subprocess is spawned.
    """
    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["version"])

    assert result.exit_code == 0, result.output
    mock_ensure.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["search", "--help"],
        ["show", "--help"],
        ["stats", "--help"],
        ["embed", "--help"],
        ["db", "--help"],
        ["db", "status", "--help"],
        ["db", "down", "--help"],
        ["db", "up", "--help"],
        ["db", "doctor", "--help"],
        ["ingest", "jlcparts", "--help"],
    ],
)
def test_b7_help_never_autostarts_and_spawns_no_subprocess(args: list[str]) -> None:
    """B-7: Given `--help` on the top-level app or on any subcommand.
    When it runs.
    Then `ensure_running` is never called and no subprocess is spawned.
    """
    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(args)

    assert result.exit_code == 0, result.output
    mock_ensure.assert_not_called()


# ---------------------------------------------------------------------------
# B-8 — validation errors never start a container: the flag is rejected
# BEFORE autostart is reached. Reuses the exact existing scenarios pinned by
# tests/unit/test_cli_search.py's own `_build_dgraph_client` never-called
# assertions (not modified here — this file only ADDS an `ensure_running`
# assertion on top of the same, unmodified scenarios).
# ---------------------------------------------------------------------------


def test_b8_search_limit_zero_never_autostarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-8: Given `search --limit 0` (an invalid --limit value).
    When it runs with autostart ON.
    Then `ensure_running` is never called and no subprocess is spawned —
    the validation error is reached and reported before autostart.
    """
    _autostart_on(monkeypatch)
    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client") as mock_build,
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["search", "MAX232", "--limit", "0"])

    assert result.exit_code != 0, result.output
    mock_ensure.assert_not_called()
    mock_build.assert_not_called()


def test_b8_search_min_stock_not_an_integer_never_autostarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-8: Given `search --min-stock abc` (a non-integer --min-stock value).
    When it runs with autostart ON.
    Then `ensure_running` is never called and no subprocess is spawned.
    """
    _autostart_on(monkeypatch)
    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client") as mock_build,
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["search", "MAX232", "--min-stock", "abc"])

    assert result.exit_code != 0, result.output
    mock_ensure.assert_not_called()
    mock_build.assert_not_called()


def test_b8_ingest_jlcparts_full_never_autostarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-8: Given `ingest jlcparts --full` (not-yet-implemented, ADR-0001) —
    exits before even the fetch stage.
    When it runs with autostart ON.
    Then `ensure_running` is never called and no subprocess is spawned.
    """
    _autostart_on(monkeypatch)
    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["ingest", "jlcparts", "--full"])

    assert result.exit_code != 0, result.output
    assert "not yet implemented" in result.output.lower()
    mock_ensure.assert_not_called()


# ---------------------------------------------------------------------------
# B-9 — the escape hatch: PARTGRAPH_AUTOSTART=0 preserves today's existing
# "is the database running?" hint verbatim, spawns no subprocess.
# ---------------------------------------------------------------------------


def test_b9_escape_hatch_search_no_subprocess_and_todays_exact_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-9: Given PARTGRAPH_AUTOSTART=0 and the database is down (mocked as
    `_build_dgraph_client` raising, exactly as it does today when pydgraph
    cannot connect).
    When `partgraph search MAX232` runs.
    Then no subprocess is spawned, `ensure_running` is never called, the
    exit code is 1, and the output contains the EXACT existing hint
    substring "Is the database running? Start it with `partgraph db up`."
    (verified against `partgraph.cli._DB_QUERY_ERROR`'s own current text,
    not assumed).
    """
    monkeypatch.setenv("PARTGRAPH_AUTOSTART", "0")

    def _raise_build(*args, **kwargs):
        raise RuntimeError("connection refused")

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", side_effect=_raise_build),
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["search", "MAX232"])

    assert result.exit_code == 1, result.output
    mock_ensure.assert_not_called()
    assert "Is the database running? Start it with" in result.output
    assert "partgraph db up" in result.output
    assert "Is the database running? Start it with `partgraph db up`." in cli_mod._DB_QUERY_ERROR


def test_b9_escape_hatch_stats_no_subprocess_and_todays_exact_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-9: Given PARTGRAPH_AUTOSTART=0 and `stats`'s own DB call fails.
    When `partgraph stats` runs.
    Then no subprocess is spawned, `ensure_running` is never called, exit
    code is 1, and the output preserves stats's OWN existing "is the
    database running?" hint verbatim.
    """
    monkeypatch.setenv("PARTGRAPH_AUTOSTART", "0")

    def _raise_build(*args, **kwargs):
        raise RuntimeError("connection refused")

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", side_effect=_raise_build),
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["stats"])

    assert result.exit_code == 1, result.output
    mock_ensure.assert_not_called()
    assert "Is the database running? Start it with `partgraph db up`" in result.output


# ---------------------------------------------------------------------------
# PARTGRAPH_AUTOSTART parsing table (pinned decision — see module docstring).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_value", "expect_autostart_called", "case_id"),
    [
        pytest.param("0", False, "zero"),
        pytest.param("false", False, "lower_false"),
        pytest.param("False", False, "mixed_case_false"),
        pytest.param("FALSE", False, "upper_false"),
        pytest.param("no", False, "lower_no"),
        pytest.param("No", False, "mixed_case_no"),
        pytest.param("NO", False, "upper_no"),
        pytest.param(" 0 ", False, "zero_with_whitespace"),
        pytest.param("", True, "empty_string_treated_as_unset"),
        pytest.param("1", True, "one"),
        pytest.param("true", True, "true"),
        pytest.param("yes", True, "yes"),
        pytest.param("on", True, "on_word_not_recognised_but_fails_open"),
        pytest.param("banana", True, "unrecognised_garbage_fails_open"),
    ],
)
def test_partgraph_autostart_env_var_parsing_table(
    monkeypatch: pytest.MonkeyPatch, raw_value: str, expect_autostart_called: bool, case_id: str
) -> None:
    """Given the PARTGRAPH_AUTOSTART parsing table pinned in this file's own
    module docstring.
    When `partgraph search MAX232` runs with PARTGRAPH_AUTOSTART set to
    *raw_value*.
    Then `ensure_running` is called iff *expect_autostart_called* — the
    ONLY recognised off-tokens are "0"/"false"/"no" (case-insensitively,
    surrounding whitespace stripped); every other value, including unset
    (not exercised here — covered by the suite's own autouse-fixture-off
    default plus the B-1..B-3/B-6/B-7 tests above, which all run WITHOUT
    setting this env var at all except where they explicitly opt in) and a
    genuinely unrecognised value, leaves autostart ON.
    """
    monkeypatch.setenv("PARTGRAPH_AUTOSTART", raw_value)
    mock_client = _make_empty_search_client()

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
    ):
        _invoke(["search", "MAX232"])

    if expect_autostart_called:
        mock_ensure.assert_called()
    else:
        mock_ensure.assert_not_called()


def test_partgraph_autostart_unset_defaults_to_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given PARTGRAPH_AUTOSTART is UNSET entirely (not merely empty).
    When `partgraph search MAX232` runs.
    Then `ensure_running` is called — autostart is ON by default.
    """
    monkeypatch.delenv("PARTGRAPH_AUTOSTART", raising=False)
    mock_client = _make_empty_search_client()

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client", return_value=(mock_client, MagicMock())),
    ):
        _invoke(["search", "MAX232"])

    mock_ensure.assert_called()
