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
    tokens are `"0"`, `"false"`, `"no"` and `"off"` — matching any of those (in
    any case) disables autostart for that invocation. UNSET, EMPTY STRING, and
    any OTHER value (`"1"`, `"true"`, `"yes"`, `"on"`, or a genuinely
    unrecognised typo like `"banana"`) all mean autostart stays ON — the
    documented default.

    Rationale, and why `"off"` is in the disable set while a typo like
    `"banana"` is not (coordinator + security-gate ruling, decisive on the
    asymmetry): the ADR names exactly ONE escape-hatch spelling
    (`PARTGRAPH_AUTOSTART=0`); `"false"`/`"no"`/`"off"` are the numeric,
    generic-CLI and systemd-style vocabularies for the SAME concept an
    operator setting this variable is overwhelmingly likely to reach for, so
    accepting all three is a reasonable robustness concession — but the set
    stops there. Failing OPEN on `"banana"` or `"on"` changes nothing,
    because autostart is ALREADY the default; failing open on `"off"` would
    do the OPPOSITE of what the operator explicitly typed, and the guarded
    action is starting a container on a host that also runs an unrelated
    cve-graph stack — the asymmetry between "a typo does nothing" and "a
    typo starts an unwanted container" is what makes `"off"` mandatory and
    everything past these four tokens still a deliberate non-goal: widening
    further (e.g. recognising `"n"`/`"disable"`) reintroduces the OTHER
    failure this design avoids — a near-miss typo landing INSIDE the disable
    set and silently switching a default-on feature off.

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
import math
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
# [BLOCKING 1 fix] Shared helpers making the B-6 allowlist tests able to
# distinguish "ensure_running called correctly, before the work" from
# "ensure_running called uselessly, anywhere, with any arguments" — the gap
# Gate 3a flagged: a bare `mock_ensure.assert_called()` is satisfied by a call
# with dummy arguments, after the DB work already ran, or from dead code.
#
# Two mechanisms, used TOGETHER by every B-6 test below (except `search`,
# which is exempted per the coordinator's ruling: it already gets a FULL,
# non-spy, subprocess-level proof of both ordering and argv-correctness from
# the real `ensure_running()` via B-1/B-2/B-3):
#   1. ORDERING: the command's own DB-touching mock (`_build_dgraph_client`,
#      `apply_schema`, `check_index_integrity`) is wired to RAISE unless
#      `ensure_running` has already fired (recorded in a shared `order` list)
#      — a hard, fail-fast ordering requirement, not a call-count coincidence.
#   2. IDENTITY: `ensure_running`'s own call kwargs are captured, and
#      `probe_health` is asserted to be `partgraph.cli.probe_health` BY
#      IDENTITY (`is`, not equality) — proving the CLI's real module-level
#      reference was threaded through, not a dummy — while `compose_up` is
#      actually INVOKED (in isolation, with its own subprocess/compose_command
#      patches) and asserted to issue `db up`'s own argv exactly, with
#      `shell=False` and a finite, bounded `timeout=` — proving it is wired to
#      the real start path, not a no-op lambda.
# ---------------------------------------------------------------------------


def _assert_compose_up_issues_db_up_argv(compose_up) -> None:
    """Invoke *compose_up* IN ISOLATION and assert it issues db up's own argv.

    Proves the seam captured from a real `ensure_running(...)` call is wired
    to the genuine `<engine> compose -f <abs COMPOSE_FILE> up -d` start path
    — never a dummy `lambda: None` that would keep every B-6 test green while
    shipping a feature that starts nothing.
    """
    assert callable(compose_up), f"compose_up must be a callable, got: {compose_up!r}"
    calls: list[list[str]] = []
    call_kwargs: list[dict] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        call_kwargs.append(kwargs)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("subprocess.run", side_effect=_fake_run),
    ):
        compose_up()

    assert calls == [["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"]], (
        f"compose_up must issue exactly db up's own argv, never a dummy or "
        f"no-op: {calls!r}"
    )
    # [SHOULD-FIX: subprocess kwargs] the one place a container gets started
    # implicitly must not have WEAKER kwarg coverage than a read-only probe
    # like volume_exists() (ADR-0022) — shell=False and a finite, bounded
    # timeout are checked here, reused by every B-6 caller of this helper.
    kwargs = call_kwargs[0]
    assert kwargs.get("shell") is False, (
        f"the compose up subprocess call must carry shell=False: {kwargs!r}"
    )
    timeout = kwargs.get("timeout")
    assert isinstance(timeout, int | float) and math.isfinite(timeout) and timeout > 0, (
        f"the compose up subprocess call must carry a finite, bounded "
        f"timeout=, mirroring volume_exists()'s own discipline: {kwargs!r}"
    )


def _assert_ensure_running_called_with_real_seams(captured_kwargs: dict) -> None:
    """Assert *captured_kwargs* (an `ensure_running(...)` call's own kwargs)
    thread through the REAL `probe_health` (by identity) and a genuine
    `compose_up` (by actually invoking it — see
    `_assert_compose_up_issues_db_up_argv`).
    """
    assert captured_kwargs.get("probe_health") is cli_mod.probe_health, (
        "ensure_running must be called with the CLI's own module-level "
        f"probe_health reference, not a dummy: {captured_kwargs.get('probe_health')!r}"
    )
    _assert_compose_up_issues_db_up_argv(captured_kwargs.get("compose_up"))


def _make_ordered_ensure_running(order: list[str], captured_kwargs: dict) -> MagicMock:
    """Return a MagicMock ensure_running() that records an "ensure_running"
    marker in *order* and captures its own call kwargs into *captured_kwargs*
    for the identity/argv assertions above.
    """

    def _fn(*args, **kwargs):
        order.append("ensure_running")
        captured_kwargs.update(kwargs)

    return MagicMock(side_effect=_fn)


def _make_ordered_touch(
    order: list[str], name: str, *, return_value: object = None, side_effect=None
) -> MagicMock:
    """Return a MagicMock DB-touch stand-in that RAISES unless "ensure_running"
    is already present in *order* — the "downstream mock raises unless
    autostart already ran" pattern, making ordering a hard, fail-fast
    requirement rather than a call-count coincidence.
    """

    def _fn(*args, **kwargs):
        assert "ensure_running" in order, (
            f"{name} was invoked before ensure_running fired — autostart "
            "must run BEFORE the command's own DB-touching work; this mock "
            "is deliberately wired to fail fast when it does not, so a "
            "call anywhere else in the body (or after the work already "
            "ran) cannot pass this test"
        )
        order.append(name)
        if side_effect is not None:
            return side_effect(*args, **kwargs)
        return return_value

    return MagicMock(side_effect=_fn)


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
    <abs COMPOSE_FILE> up -d`, carrying `shell=False` and a finite, bounded
    `timeout=` [SHOULD-FIX: this is the one place a container gets started
    implicitly; it must not have weaker kwarg coverage than a read-only probe
    like `volume_exists()`, ADR-0022]; the search's own DB work (the mock
    client's txn) still runs afterward, proving the command's DB work begins
    only after ensure_running() reports healthy.
    """
    _autostart_on(monkeypatch)
    mock_client = _make_empty_search_client()
    calls: list[list[str]] = []
    call_kwargs: list[dict] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        call_kwargs.append(kwargs)
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
    up_indices = [i for i, argv in enumerate(calls) if "compose" in argv and "up" in argv]
    assert len(up_indices) == 1, f"expected exactly one compose up call, got: {calls!r}"
    up_argv = calls[up_indices[0]]
    up_kwargs = call_kwargs[up_indices[0]]
    assert up_argv == ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"], (
        f"the autostart argv must equal `db up`'s own argv exactly: {up_argv!r}"
    )
    assert up_kwargs.get("shell") is False, (
        f"the autostart compose up call must carry shell=False: {up_kwargs!r}"
    )
    timeout = up_kwargs.get("timeout")
    assert isinstance(timeout, int | float) and math.isfinite(timeout) and timeout > 0, (
        f"the autostart compose up call must carry a finite, bounded "
        f"timeout=, mirroring volume_exists()'s own discipline: {up_kwargs!r}"
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
    no `Traceback` reaches the user, the search's own DB work (the mock
    client's txn) NEVER runs, and [Gate 5 gap fix] NO second line naming a
    start-command failure appears — compose_up() reported success here, so
    `_autostart_database()`'s `exc.__cause__ is not None` branch must stay
    UN-taken, distinguishing this from the new
    `test_b4_start_command_fails_and_never_recovers_exits_one_with_primary_
    and_cause_lines` case below.
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
    assert "start command itself failed first" not in result.output, (
        "compose_up() reported success in this scenario — no second, "
        f"cause-naming line may appear: {result.output!r}"
    )


def test_b4_start_command_fails_and_never_recovers_exits_one_with_primary_and_cause_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-4 [Gate 5 gap fix — the new diagnostic had zero coverage]: Given the
    compose `up -d` call itself exits non-zero (`_autostart_compose_up`
    raises `AutostartComposeError`, `src/partgraph/cli.py`) AND
    `probe_health()` NEVER reports healthy afterward — a genuine,
    unrecoverable start failure, the one case where an operator most needs
    to know a fatal misconfiguration rather than a benign lost race
    (contrast `test_absorbed_start_failure_followed_by_health_recovery_
    prints_no_error`, which pins the RECOVERED path staying silent and is
    deliberately left unmodified).
    When `partgraph search MAX232` runs with autostart ON.
    Then the exit code is 1; the PRIMARY line names the readiness budget and
    suggests `partgraph db status` (identical to B-3's own line); a SECOND,
    DISTINCT line reads "The start command itself failed first
    (AutostartComposeError); see `partgraph db doctor`." — naming the
    absorbed failure's TYPE, chained via `AutostartTimeoutError.__cause__`
    in the leaf (asserted directly, by identity, in
    `tests/unit/test_lifecycle_ensure_running.py`; this file can only
    observe the RENDERED text, since `CliRunner` does not hand back the raw
    exception chain) — and both lines are individually single-line and
    path-free. The search's own DB work never runs.
    """
    _autostart_on(monkeypatch)
    mock_client = _make_empty_search_client()

    def _fake_run(argv, **kwargs):
        if "compose" in argv and "up" in argv:
            return MagicMock(returncode=125, stdout="", stderr="engine refused")
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
    assert not mock_client.txn.called, (
        "the search's own DB query must never run when the database never "
        "became healthy"
    )

    lines = result.output.splitlines()
    primary_lines = [ln for ln in lines if "partgraph db status" in ln]
    cause_lines = [ln for ln in lines if "start command itself failed first" in ln]
    assert primary_lines, (
        f"expected the primary readiness-budget line (same as B-3's): "
        f"{result.output!r}"
    )
    assert len(cause_lines) == 1, (
        f"expected EXACTLY one second line naming the absorbed start "
        f"failure's type: {result.output!r}"
    )
    cause_line = cause_lines[0]
    assert "AutostartComposeError" in cause_line, (
        f"the second line must name the cause's TYPE "
        f"(AutostartComposeError): {cause_line!r}"
    )
    assert "partgraph db doctor" in cause_line, (
        f"the second line must point at `partgraph db doctor`: {cause_line!r}"
    )
    assert cause_line != primary_lines[0], (
        "the cause line must be DISTINCT from the primary line, not merged "
        f"into one sentence: {result.output!r}"
    )
    for ln in lines:
        assert "/" not in ln, f"autostart error line leaks a path: {ln!r}"


# ---------------------------------------------------------------------------
# B-6 — the allowlist, exactly: one case per command. Uses a SPY on
# partgraph.cli.ensure_running (never exercising its real internals, which
# the B-1/B-2/B-3 section and test_lifecycle_ensure_running.py already
# cover), and mocks only what each command needs to reach its own DB-touching
# point without erroring for an UNRELATED reason.
#
# [Gate 3a BLOCKING 1 fix] Every case except `search` (exempted by the
# coordinator's own ruling: it already gets a FULL, non-spy, subprocess-level
# proof of both ordering and argv-correctness from the REAL ensure_running()
# via B-1/B-2/B-3) now asserts, via the shared helpers just above, BOTH (a)
# strict ordering — the command's own DB-touching mock is wired to RAISE if
# invoked before "ensure_running" is recorded — and (b) argument identity —
# `probe_health` is the CLI's own real reference, and `compose_up`, when
# actually invoked, issues `db up`'s argv exactly. A bare
# `mock_ensure.assert_called()` (the PRIOR, weaker shape) is satisfied by a
# call anywhere in the body, with any arguments, in any order, including
# after the DB work already ran; none of that is true of the assertions
# below.
# ---------------------------------------------------------------------------


def test_b6_stats_triggers_autostart_before_db_work_with_real_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-6 [BLOCKING 1 fix]: Given `partgraph stats`, a DB-touching command.
    When it runs with autostart ON.
    Then `ensure_running` fires strictly BEFORE `_build_dgraph_client`
    (`_build_dgraph_client` is wired to raise otherwise — a fail-fast
    ordering check, not a call-count coincidence), and it is called with the
    CLI's own REAL `probe_health` (by identity) and a `compose_up` that, when
    actually invoked, issues `db up`'s own argv exactly — proving the
    wiring cannot be satisfied by a dummy placed after the DB work.
    """
    _autostart_on(monkeypatch)
    order: list[str] = []
    captured: dict = {}

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
        patch("partgraph.cli.ensure_running", _make_ordered_ensure_running(order, captured)),
        patch.object(
            cli_mod,
            "_build_dgraph_client",
            _make_ordered_touch(
                order, "_build_dgraph_client", return_value=(mock_client, MagicMock())
            ),
        ),
    ):
        result = _invoke(["stats"])

    assert result.exit_code == 0, result.output
    assert order == ["ensure_running", "_build_dgraph_client"], (
        f"ensure_running must fire strictly before _build_dgraph_client: {order!r}"
    )
    _assert_ensure_running_called_with_real_seams(captured)


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


def test_b6_show_triggers_autostart_before_db_work_with_real_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-6 [BLOCKING 1 fix]: Given `partgraph show`, a read-only DB-touching
    command.
    When it runs with autostart ON.
    Then `ensure_running` fires strictly before `_build_dgraph_client`, with
    the CLI's real `probe_health` and a `compose_up` that genuinely issues
    `db up`'s argv (see `_assert_ensure_running_called_with_real_seams`).
    """
    _autostart_on(monkeypatch)
    order: list[str] = []
    captured: dict = {}

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
        patch("partgraph.cli.ensure_running", _make_ordered_ensure_running(order, captured)),
        patch.object(
            cli_mod,
            "_build_dgraph_client",
            _make_ordered_touch(
                order, "_build_dgraph_client", return_value=(mock_client, MagicMock())
            ),
        ),
    ):
        result = _invoke(["show", "MAX232"])

    assert result.exit_code == 0, result.output
    assert order == ["ensure_running", "_build_dgraph_client"], (
        f"ensure_running must fire strictly before _build_dgraph_client: {order!r}"
    )
    _assert_ensure_running_called_with_real_seams(captured)


def test_b6_embed_triggers_autostart_before_db_work_with_real_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-6 [BLOCKING 1 fix]: Given `partgraph embed`.
    When it runs with autostart ON.
    Then `ensure_running` fires strictly before `_build_dgraph_client`, with
    the CLI's real `probe_health` and a genuine `compose_up`.
    """
    _autostart_on(monkeypatch)
    order: list[str] = []
    captured: dict = {}

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
        patch("partgraph.cli.ensure_running", _make_ordered_ensure_running(order, captured)),
        patch.object(
            cli_mod,
            "_build_dgraph_client",
            _make_ordered_touch(
                order, "_build_dgraph_client", return_value=(mock_client, MagicMock())
            ),
        ),
        patch.object(cli_mod, "get_encoder", _fake_get_encoder, create=True),
    ):
        result = _invoke(["embed"])

    assert result.exit_code == 0, result.output
    assert order == ["ensure_running", "_build_dgraph_client"], (
        f"ensure_running must fire strictly before _build_dgraph_client: {order!r}"
    )
    _assert_ensure_running_called_with_real_seams(captured)


def test_b6_refresh_links_triggers_autostart_before_db_work_with_real_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-6 [BLOCKING 1 fix]: Given `partgraph refresh-links`.
    When it runs with autostart ON.
    Then `ensure_running` fires strictly before `_build_dgraph_client`, with
    the CLI's real `probe_health` and a genuine `compose_up`.
    """
    _autostart_on(monkeypatch)
    order: list[str] = []
    captured: dict = {}

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
        patch("partgraph.cli.ensure_running", _make_ordered_ensure_running(order, captured)),
        patch.object(
            cli_mod,
            "_build_dgraph_client",
            _make_ordered_touch(
                order, "_build_dgraph_client", return_value=(mock_client, MagicMock())
            ),
        ),
    ):
        result = _invoke(["refresh-links"])

    assert result.exit_code == 0, result.output
    assert order == ["ensure_running", "_build_dgraph_client"], (
        f"ensure_running must fire strictly before _build_dgraph_client: {order!r}"
    )
    _assert_ensure_running_called_with_real_seams(captured)


def test_b6_refresh_stock_triggers_autostart_before_db_work_with_real_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """B-6 [BLOCKING 1 fix]: Given `partgraph refresh` (the stock/price
    refresh command), with an existing dummy source file and a stubbed
    source-loading seam so it reaches its own DB-touching point.
    `_load_stock_index` is a LOCAL file read (not a DB touch), so it is
    deliberately left OUTSIDE the ordering check — only
    `_build_dgraph_client` is the ordering marker here.
    When it runs with autostart ON.
    Then `ensure_running` fires strictly before `_build_dgraph_client`, with
    the CLI's real `probe_health` and a genuine `compose_up`.
    """
    _autostart_on(monkeypatch)
    dummy = tmp_path / "dummy-jlcpcb-components.sqlite3"
    dummy.write_bytes(b"")
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", dummy)
    order: list[str] = []
    captured: dict = {}

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
        patch("partgraph.cli.ensure_running", _make_ordered_ensure_running(order, captured)),
        patch.object(
            cli_mod,
            "_build_dgraph_client",
            _make_ordered_touch(
                order, "_build_dgraph_client", return_value=(mock_client, MagicMock())
            ),
        ),
        patch.object(cli_mod, "_load_stock_index", return_value={}, create=True),
    ):
        result = _invoke(["refresh"])

    assert result.exit_code == 0, result.output
    assert order == ["ensure_running", "_build_dgraph_client"], (
        f"ensure_running must fire strictly before _build_dgraph_client: {order!r}"
    )
    _assert_ensure_running_called_with_real_seams(captured)


def test_b6_db_apply_schema_triggers_autostart_before_db_work_with_real_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-6 [BLOCKING 1 fix]: Given `partgraph db apply-schema` — this command
    does NOT go through `_build_dgraph_client()` (it uses
    `schema_module.apply_schema` over its own gRPC path), so autostart must
    be wired here EXPLICITLY, not merely inherited from a shared helper —
    exactly the divergent-wiring risk Gate 3a flagged for this command by
    name. `schema_module.load_schema` is a LOCAL file read (not a DB touch),
    so it is deliberately left OUTSIDE the ordering check; only
    `schema_module.apply_schema` (the actual gRPC call) is the marker.
    When it runs with autostart ON.
    Then `ensure_running` fires strictly before `schema_module.apply_schema`,
    with the CLI's real `probe_health` and a genuine `compose_up`.
    """
    _autostart_on(monkeypatch)
    order: list[str] = []
    captured: dict = {}

    with (
        patch("partgraph.cli.ensure_running", _make_ordered_ensure_running(order, captured)),
        patch.object(cli_mod.schema_module, "load_schema", return_value="type Part {}"),
        patch.object(
            cli_mod.schema_module,
            "apply_schema",
            _make_ordered_touch(order, "schema_module.apply_schema"),
        ),
    ):
        result = _invoke(["db", "apply-schema"])

    assert result.exit_code == 0, result.output
    assert order == ["ensure_running", "schema_module.apply_schema"], (
        f"ensure_running must fire strictly before apply_schema: {order!r}"
    )
    _assert_ensure_running_called_with_real_seams(captured)


def test_b6_db_check_index_triggers_autostart_before_db_work_with_real_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-6 [BLOCKING 1 fix]: Given `partgraph db check-index` — like
    `db apply-schema`, this does NOT go through `_build_dgraph_client()` (it
    calls `check_index_integrity()` directly), so it needs its OWN explicit
    autostart wiring too — the second of the two commands Gate 3a named as
    most likely to diverge.
    When it runs with autostart ON.
    Then `ensure_running` fires strictly before `check_index_integrity`,
    with the CLI's real `probe_health` and a genuine `compose_up`.
    """
    _autostart_on(monkeypatch)
    order: list[str] = []
    captured: dict = {}
    healthy_result = MagicMock(reachable=True, schema_ok=True, self_similarity_ok=True, message="ok")

    with (
        patch("partgraph.cli.ensure_running", _make_ordered_ensure_running(order, captured)),
        patch(
            "partgraph.cli.check_index_integrity",
            _make_ordered_touch(order, "check_index_integrity", return_value=healthy_result),
        ),
    ):
        result = _invoke(["db", "check-index"])

    assert result.exit_code == 0, result.output
    assert order == ["ensure_running", "check_index_integrity"], (
        f"ensure_running must fire strictly before check_index_integrity: {order!r}"
    )
    _assert_ensure_running_called_with_real_seams(captured)


def _existing_dummy_sqlite(tmp_path) -> os.PathLike[str]:
    import pathlib

    dummy = pathlib.Path(tmp_path) / "dummy-jlcpcb-components.sqlite3"
    dummy.write_bytes(b"")
    return dummy


def test_b6_ingest_jlcparts_load_stage_triggers_autostart_before_db_work_with_real_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """B-6 [BLOCKING 1 fix; fixture fixed per Gate re-review, defect 2]: Given
    `partgraph ingest jlcparts` reaches the LOAD stage (the source file
    "exists" as a 0-byte dummy, and fetch/normalize's OWN sub-steps are all
    stubbed to succeed). `_stage_normalize` does not merely call
    `partgraph.normalize.run.normalize` — it FIRST constructs
    `JlcpartsAdapter(open_jlcparts_db(dest))`, both imported lazily from
    `partgraph.sources.jlcparts` inside `_stage_normalize` itself, and a
    0-byte dummy file fails `open_jlcparts_db`'s own schema introspection
    (`Unrecognized jlcparts schema: the 'components' table is missing the
    required 'lcsc' column`) before `normalize()` is ever reached — so both
    are patched at their ORIGIN module here too (mirrors how
    `partgraph.normalize.run.normalize`/`partgraph.load.loader.Loader.load`
    are already patched at THEIR origin modules, since all three are lazy,
    function-local imports inside cli.py, never module-level `cli_mod`
    attributes). `open_jlcparts_db`/`JlcpartsAdapter` and
    `partgraph.normalize.run.normalize` are all LOCAL, non-DB stages and are
    deliberately left OUTSIDE the ordering check; only `_build_dgraph_client`
    (reached inside `_stage_load`) is the marker.
    When it runs with autostart ON.
    Then `ensure_running` fires strictly before `_build_dgraph_client`
    during the load stage, with the CLI's real `probe_health` and a genuine
    `compose_up`.
    """
    _autostart_on(monkeypatch)
    import pathlib

    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_sqlite(tmp_path))
    monkeypatch.setattr(cli_mod, "STAGED_PATH", pathlib.Path(tmp_path) / "staged.jsonl")
    (pathlib.Path(tmp_path) / "staged.jsonl").write_bytes(b"")
    monkeypatch.setattr(
        cli_mod, "LOAD_CHECKPOINT_PATH", pathlib.Path(tmp_path) / "state" / "load_checkpoint.json"
    )

    order: list[str] = []
    captured: dict = {}
    mock_client = MagicMock()

    with (
        patch("partgraph.cli.ensure_running", _make_ordered_ensure_running(order, captured)),
        patch.object(
            cli_mod,
            "_build_dgraph_client",
            _make_ordered_touch(
                order, "_build_dgraph_client", return_value=(mock_client, MagicMock())
            ),
        ),
        patch("partgraph.sources.jlcparts.open_jlcparts_db", return_value=MagicMock()),
        patch("partgraph.sources.jlcparts.JlcpartsAdapter"),
        patch("partgraph.normalize.run.normalize", return_value=None),
        patch("partgraph.load.loader.Loader.load", return_value=None),
    ):
        result = _invoke(["ingest", "jlcparts"])

    assert result.exit_code == 0, result.output
    assert order == ["ensure_running", "_build_dgraph_client"], (
        f"ensure_running must fire strictly before _build_dgraph_client: {order!r}"
    )
    _assert_ensure_running_called_with_real_seams(captured)


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
    """B-6 (negative half) [fixture fixed per Gate re-review, defect 2 — "the
    two negative siblings must stay meaningful"]: Given `partgraph ingest
    jlcparts` (no --fetch, source file "present" as a 0-byte dummy) where the
    NORMALIZE stage itself fails (never reaching load). `open_jlcparts_db`/
    `JlcpartsAdapter` (both lazily imported from `partgraph.sources.jlcparts`
    inside `_stage_normalize`, exactly like the load-stage positive test
    above) are patched to succeed on the dummy file, so the injected
    `_failing_normalize` RuntimeError is what genuinely aborts the run — a
    0-byte dummy's OWN, unrelated schema-validation failure inside
    `open_jlcparts_db` would otherwise abort the run FIRST, making this test
    pass for the wrong reason (never actually reaching, let alone failing
    inside, `normalize()` at all).
    When it runs with autostart ON.
    Then `ensure_running` is NEVER called, and the failure is attributably
    the injected normalize failure (its own message reaches the output),
    not an earlier, unrelated one.
    """
    _autostart_on(monkeypatch)
    monkeypatch.setattr(cli_mod, "RAW_DB_PATH", _existing_dummy_sqlite(tmp_path))

    def _failing_normalize(*args, **kwargs):
        raise RuntimeError("malformed source database")

    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch("partgraph.sources.jlcparts.open_jlcparts_db", return_value=MagicMock()),
        patch("partgraph.sources.jlcparts.JlcpartsAdapter"),
        patch("partgraph.normalize.run.normalize", side_effect=_failing_normalize),
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["ingest", "jlcparts"])

    assert result.exit_code != 0, result.output
    assert "malformed source database" in result.output, (
        f"expected the INJECTED normalize failure's own message in the "
        f"output, proving the run genuinely reached and failed inside "
        f"normalize() rather than aborting earlier for an unrelated reason: "
        f"{result.output!r}"
    )
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
        ["db", "apply-schema", "--help"],
        ["db", "check-index", "--help"],
        ["ingest", "jlcparts", "--help"],
        ["refresh", "--help"],
        ["refresh-links", "--help"],
    ],
)
def test_b7_help_never_autostarts_and_spawns_no_subprocess(args: list[str]) -> None:
    """B-7 [SHOULD-FIX: --help symmetry]: Given `--help` on the top-level app
    or on any subcommand — now covering ALL NINE allowlisted commands'
    `--help`, not just five of them. `db apply-schema --help` and
    `db check-index --help` are the two commands that do NOT go through
    `_build_dgraph_client()` (see the B-6 tests above) and therefore need
    BESPOKE autostart wiring — i.e. the two most likely for `--help` to
    diverge from the others if that bespoke wiring is placed carelessly
    ahead of Typer's own `--help` short-circuit.
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
# BEFORE autostart is reached. Reuses existing scenarios pinned by
# tests/unit/test_cli_search.py's own `_build_dgraph_client` never-called
# assertions (not modified here — this file only ADDS an `ensure_running`
# assertion on top of the same, unmodified scenarios) — specifically
# `--min-stock abc` (AC-SF-28) and `--sort bogus` (AC-SF-40), both of which
# genuinely exist there. `search --limit` is deliberately NOT one of these
# cases: unlike `embed`/`refresh`/`refresh-links`/`ingest jlcparts`,
# `search`'s own `--limit` is a bare Typer `int` with no `_validate_limit()`
# call at all (confirmed by `grep -n "_validate_limit(limit)" src/partgraph/
# cli.py`, which lists exactly four call sites and `search` is not one of
# them) — `--limit 0` is silently clamped to 1 by
# `partgraph.query.dql_builder`'s `max(1, min(int(limit), MAX_RESULT_LIMIT))`
# and the command exits 0. An earlier draft of this file asserted the
# opposite (a fabricated citation, caught in review — see this module's own
# note on the habit below); pinning that non-existent behaviour here would
# have been the third such fabrication in this body of work. See the FOLLOW-UP
# note below the last test in this section: this silent-clamp-vs-reject
# inconsistency between `search` and its siblings is real and worth fixing,
# but doing so is a user-visible contract change outside PR-B2's scope, not
# a test-authoring fix.
# ---------------------------------------------------------------------------


def test_b8_search_sort_invalid_never_autostarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-8: Given `search --sort bogus` (AC-SF-40 — an invalid --sort value,
    rejected by `_validate_sort_flag` with a fixed exit-1 message BEFORE any
    Dgraph client is built; see
    `tests/unit/test_cli_search.py::test_ac_sf_40_sort_bogus_value_exits_1_not_2_no_db_query`,
    whose own `_build_dgraph_client` never-called assertion this test reuses
    unmodified, adding only the `ensure_running` half).
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
        result = _invoke(["search", "MAX232", "--sort", "bogus"])

    assert result.exit_code == 1, result.output
    assert "--sort must be one of: relevance, stock, price." in result.output
    mock_ensure.assert_not_called()
    mock_build.assert_not_called()


# ---------------------------------------------------------------------------
# FOLLOW-UP (recorded here, deliberately NOT fixed by this file — a
# user-visible contract change with no ADR is out of scope for PR-B2):
# `search --limit 0` silently CLAMPS to 1 (`partgraph.query.dql_builder`'s
# `max(1, min(int(limit), MAX_RESULT_LIMIT))`) and exits 0, while
# `embed --limit 0` / `refresh --limit 0` / `refresh-links --limit 0` /
# `ingest jlcparts --limit 0` all REJECT it via the shared `_validate_limit()`
# helper and exit 1 with "--limit must be a positive integer." `search` is
# the one command among these five that never calls `_validate_limit()` at
# all (confirmed: `grep -n "_validate_limit(limit)" src/partgraph/cli.py`
# lists exactly four call sites, none of them `search`). This inconsistency
# predates PR-B2 and is orthogonal to autostart; it should become its own
# small fix (either give `search --limit` the same validator, or document
# the clamp deliberately) with its own test coverage, not be silently
# absorbed into this file.
# ---------------------------------------------------------------------------


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
# [SHOULD-FIX: validation-before-autostart for the SHARED validator] B-8
# above proves ordering for `search` and `ingest jlcparts --full` only —
# both go through validation paths specific to those commands.
# `_validate_limit()` is a SEPARATE, SHARED helper called at four other
# sites (`cli.py`: embed, refresh, refresh-links, plus ingest jlcparts
# itself, already covered), with no paired test proving THOSE three commands
# reject a bad --limit before autostart. Naive wiring at the top of any of
# these three function bodies would start a container before ever reporting
# the bad flag.
# ---------------------------------------------------------------------------


def test_b8_embed_limit_zero_never_autostarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-8 [SHOULD-FIX]: Given `embed --limit 0` (an invalid --limit value,
    validated by the SAME shared `_validate_limit()` helper as `search`/
    `ingest jlcparts`, but at embed's own call site).
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
        result = _invoke(["embed", "--limit", "0"])

    assert result.exit_code != 0, result.output
    assert "--limit must be a positive integer" in result.output
    mock_ensure.assert_not_called()
    mock_build.assert_not_called()


def test_b8_refresh_limit_zero_never_autostarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-8 [SHOULD-FIX]: Given `refresh --limit 0` (the stock/price refresh
    command's own use of the SAME shared `_validate_limit()` helper) —
    `_validate_limit(limit)` is the FIRST statement in `refresh()`'s body,
    before the source file / `_load_stock_index`/`_build_dgraph_client`
    path is ever touched.
    When it runs with autostart ON.
    Then `ensure_running` is never called and no subprocess is spawned.
    """
    _autostart_on(monkeypatch)
    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client") as mock_build,
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["refresh", "--limit", "0"])

    assert result.exit_code != 0, result.output
    assert "--limit must be a positive integer" in result.output
    mock_ensure.assert_not_called()
    mock_build.assert_not_called()


def test_b8_refresh_links_limit_zero_never_autostarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-8 [SHOULD-FIX]: Given `refresh-links --limit 0` (the datasheet-link
    command's own use of the SAME shared `_validate_limit()` helper).
    When it runs with autostart ON.
    Then `ensure_running` is never called and no subprocess is spawned.
    """
    _autostart_on(monkeypatch)
    with (
        patch("partgraph.cli.ensure_running") as mock_ensure,
        patch.object(cli_mod, "_build_dgraph_client") as mock_build,
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
    ):
        result = _invoke(["refresh-links", "--limit", "0"])

    assert result.exit_code != 0, result.output
    assert "--limit must be a positive integer" in result.output
    mock_ensure.assert_not_called()
    mock_build.assert_not_called()


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
        pytest.param("off", False, "lower_off"),
        pytest.param("Off", False, "mixed_case_off"),
        pytest.param("OFF", False, "upper_off"),
        pytest.param(" off ", False, "off_with_whitespace"),
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
    """[BLOCKING 2 fix] Given the PARTGRAPH_AUTOSTART parsing table pinned in
    this file's own module docstring.
    When `partgraph search MAX232` runs with PARTGRAPH_AUTOSTART set to
    *raw_value*.
    Then `ensure_running` is called iff *expect_autostart_called* — the
    ONLY recognised off-tokens are "0"/"false"/"no"/"off" (case-insensitively,
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


# ---------------------------------------------------------------------------
# [SHOULD-FIX: no stderr leak on an absorbed failure] Gate 3b found that
# `_run_compose` (cli.py) prints a red `Error:` line BEFORE raising, on a
# non-zero compose exit. `ensure_running()`'s own contract absorbs a
# `compose_up()` failure and recovers if health comes up afterward (B-5 —
# the losing side of a start-vs-start race). If `_run_compose` were reused
# VERBATIM as the `compose_up` seam, a losing racer would print a visible
# "Error: failed to start the Dgraph database..." line and then still exit 0
# — a confusing, false-alarm error message on a command that actually
# succeeded. This drives the REAL end-to-end path (like B-1/B-2/B-3, not a
# spy), so it only turns green once the implementer supplies a print-free
# `compose_up` seam — reusing `_run_compose` verbatim will NOT satisfy it.
# ---------------------------------------------------------------------------


def test_absorbed_start_failure_followed_by_health_recovery_prints_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[SHOULD-FIX] Given the compose `up -d` call itself exits non-zero
    (modelling "container name already in use" — the losing side of a
    start-vs-start race, B-5's own scenario), but `probe_health()` reports
    healthy on the very next poll (the OTHER racer's start is completing).
    When `partgraph search MAX232` runs with autostart ON.
    Then the exit code is 0, the search's own results still print normally,
    and NO `Error` text appears anywhere in the output — an absorbed,
    recovered-from start failure must never surface a scary, misleading
    error line for a command that is about to succeed.
    """
    _autostart_on(monkeypatch)
    mock_client = _make_empty_search_client()
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "compose" in argv and "up" in argv:
            return MagicMock(
                returncode=125,
                stdout="",
                stderr='Error response from daemon: container name "partgraph-dgraph" is already in use',
            )
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

    up_calls = [argv for argv in calls if "compose" in argv and "up" in argv]
    assert up_calls, (
        "the compose up call was never attempted in this scenario — the "
        "test proves nothing without it"
    )
    assert result.exit_code == 0, result.output
    assert "Error" not in result.output, (
        f"an absorbed start failure that was later recovered from must "
        f"never print a visible error line: {result.output!r}"
    )
    assert mock_client.txn.called, (
        "the search's own DB query must still run once the database is "
        "healthy, despite the losing start attempt"
    )
