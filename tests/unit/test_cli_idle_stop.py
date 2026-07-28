"""
Tests: PR-C (feat/db-idle-autostop) — `partgraph db idle-stop` end-to-end,
and `PARTGRAPH_IDLE_TIMEOUT_MINUTES` parsing (ADR-0022, "idle auto-stop").

NOT YET IMPLEMENTED. `partgraph.util.activity` does not exist yet — this
whole file is expected to ERROR at COLLECTION with `ModuleNotFoundError`
(its own top-level `from partgraph.util.activity import (...)`), mirroring
`tests/unit/test_cli_db_down.py`'s own documented pre-PR-A history for
`partgraph.util.lifecycle`. This is the correct test-first RED state.

Pinned CLI contract this file exercises:

  - A new `partgraph db idle-stop` command, no flags.
  - A new pure function `partgraph.cli._idle_timeout_minutes() -> float`,
    mirroring `_autostart_enabled()`'s own precedent EXACTLY ("Parsing lives
    HERE and not in the leaf on purpose... this is the CLI's policy, so the
    CLI owns it"). Read from `os.environ.get("PARTGRAPH_IDLE_TIMEOUT_MINUTES")`
    at CALL time, never captured at import time. THE PINNED RULING (my own,
    reported for pushback — the ACs ask for it to be decided, not dictate
    it):

      | input (after `.strip()`)                | result                    |
      |-------------------------------------------|---------------------------|
      | unset                                      | `DEFAULT_IDLE_TIMEOUT_MINUTES` (30.0) |
      | `""` (empty)                                | `DEFAULT_IDLE_TIMEOUT_MINUTES` |
      | fails `float(...)` (`"banana"`, `"5m"`)     | `DEFAULT_IDLE_TIMEOUT_MINUTES` |
      | parses but is NOT finite (`"inf"`, `"nan"`, `"-inf"`) | `DEFAULT_IDLE_TIMEOUT_MINUTES` |
      | parses to a finite value `<= 0` (`"0"`, `"-5"`) | `0.0` (the canonical DISABLED sentinel — C-9) |
      | parses to a finite value `> 0`              | that value, VERBATIM, no ceiling |

    Rationale for each row, since "decide and pin the rule" was explicit:
    unset/empty/unparseable/non-finite all fail toward the DOCUMENTED
    DEFAULT (30 minutes) rather than toward EITHER extreme — never silently
    toward "always stop immediately" (which a naive `float(garbage) or 0`
    idiom, or an un-guarded `math.isfinite` omission letting `"nan"` flow
    through un-caught into a `>=` comparison that is always False, would
    each risk in a DIFFERENT wrong direction) NOR toward "silently
    disabled" (which would surprise an operator who was plainly trying to
    CONFIGURE the timeout, not turn the feature off). Only the exact,
    documented escape-hatch family — a value that parses AND is `<= 0` —
    disables the feature; this collapses the AC's literal `"0"` example
    into the same, simpler "non-positive" rule as any negative number,
    since a negative timeout has no sane positive interpretation either and
    must not be handled by leaving it unguarded (an unguarded negative
    inside a bare `age_minutes >= timeout` comparison is ALWAYS true --
    the single most dangerous possible parsing bug for a control that
    exists to stop a database). No SANITY CEILING is imposed on an
    absurdly large value (unlike `AUTOSTART_READY_TIMEOUT_S`'s own
    documented ceiling): the two constants' risk directions are NOT
    symmetric — an oversized `AUTOSTART_READY_TIMEOUT_S` costs a HUMAN a
    real foreground wait, while an oversized idle timeout only ever means
    idle-stop practically never fires, which is the SAFE direction already
    (nothing destructive happens either way).

    [Gate 3b SHOULD-FIX, recorded rather than fixed] NO FLOOR is imposed on
    an absurdly SMALL positive value either (e.g. `"0.001"`, a likely typo
    for `"10"`), unlike the systemd cadence bound
    (`tests/unit/test_systemd_idle_stop_units.py`'s own
    `_SANE_CADENCE_FLOOR_S`), which DOES get a floor. The asymmetry is
    deliberate, not an oversight: the systemd cadence directly controls how
    OFTEN a `partgraph db idle-stop` PROCESS itself gets spawned — too
    frequent genuinely wastes host resources (a real, unbounded-in-practice
    cost). The idle-TIMEOUT value only controls the THRESHOLD a check
    applies once it does run; an absurdly small one merely makes the
    database look "idle" sooner, and C-4's live-lease check unconditionally
    blocks the stop regardless of how small the timeout is — so the
    downside of a typo here is "stops a little too eagerly, pays one extra
    autostart round-trip next use," not lost or corrupted work. A
    recommendation for a future PR, not a blocker for this one.

  - `partgraph.cli.ACTIVITY_STATE_DIR: Path` — a new, patchable module
    constant (mirrors `NORMALIZE_CHECKPOINT_PATH`/`LOAD_CHECKPOINT_PATH`'s
    own already-established `monkeypatch.setattr(cli_mod, "X", tmp_path /
    ...)` pattern — see `test_cli_autostart.py`'s own load-stage test),
    defaulting to `partgraph.util.activity.default_state_dir()`.

  - `idle_stop()`'s own body, in order:
      1. Resolve `timeout = _idle_timeout_minutes()`. If `timeout <= 0`
         (C-9): print a message, exit 0, WITHOUT calling `probe_health`,
         `evaluate_idle`, `engine_command`, or `subprocess.run` AT ALL — a
         genuine, zero-I/O no-op at the CLI layer, not merely "the leaf
         decided not to stop".
      2. Otherwise, probe `db_reachable = bool(getattr(probe_health(),
         "healthy", False))` (the CLI's own `probe_health` reference, same
         seam `db status`/`db down`/autostart already share) and call
         `evaluate_idle(state_dir=ACTIVITY_STATE_DIR,
         idle_timeout_minutes=timeout, db_reachable=db_reachable,
         probe_health=...)`. NEVER `ensure_running`/`_autostart_database` —
         `db idle-stop` is the OPPOSITE of a DB-touching command (C-12);
         this is asserted by patching `partgraph.cli.ensure_running` to
         raise if it is ever called, with `PARTGRAPH_AUTOSTART` explicitly
         left ON, so the guarantee is proven regardless of that unrelated
         switch's own setting.
      3. If `decision.should_stop` is False: print a message naming
         `decision.reason`, exit 0, and NEVER call `stop_all`.
      4. If True: delegate to `partgraph.util.lifecycle.stop_all`, called
         EXACTLY the way `down()` already calls it — `engine_prefix=
         engine_command()`, `compose_down=lambda: _run_compose(["down"],
         action="stop", timeout=COMPOSE_DOWN_TIMEOUT_S)`, `probe_health=
         probe_health`, `dry_run=False` — so the S1/S2/S3 selector policy
         and the cve-graph guarantee are INHERITED from the already-tested
         leaf (C-7), never re-derived here. Always exits 0 REGARDLESS of
         `stop_all`'s own outcome (my own ruling: `idle-stop` is an
         unattended, best-effort background action — a survivor there only
         costs continued idle memory, never correctness, so it must not
         make a systemd timer's own run accounting look like a failure the
         way a HUMAN-typed `db down` correctly does). [Gate 3a BLOCKING
         fix] "Always exit 0" MUST NOT collapse into "always silent": if
         `result.survivors` or `result.undetermined` is non-empty, a
         path-free stdout line naming the fact (e.g. containing "survivor"
         or "could not verify") is STILL printed before exiting 0 — a
         genuinely-failed stop must remain OBSERVABLE (e.g. via
         `journalctl --user -u partgraph-db-idle-stop.service`) even though
         the unit itself is never reported as failed. Proven below by
         `test_idle_stop_exits_zero_but_prints_a_path_free_diagnostic_
         when_a_survivor_remains` — every OTHER test reaching `stop_all` in
         this file supplies a clean `DownResult` and therefore never
         exercises this branch on its own.

HERMETICITY: every test patches only `subprocess.run`,
`partgraph.cli.compose_command`, `partgraph.cli.engine_command`,
`partgraph.cli.probe_health`, `partgraph.cli.ensure_running` and
`shutil.which` — the REAL `stop_all`/`find_partgraph_instances`/`unit_state`
run underneath, driven by the SAME kind of scripted, stateful
`subprocess.run` fake `test_cli_db_down.py` uses (copied here, not shared,
per CONTRIBUTING.md's "test fixtures stay local to their file"). No test
opens a real socket, starts a real container, sleeps, or reads the real wall
clock; `ACTIVITY_STATE_DIR` is always redirected to `tmp_path`.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import partgraph.cli as cli_mod
from partgraph.cli import COMPOSE_FILE, app

# This import is expected to raise ModuleNotFoundError until
# src/partgraph/util/activity.py exists — the correct test-first red state.
from partgraph.util.activity import (  # noqa: E402
    DEFAULT_IDLE_TIMEOUT_MINUTES,
    acquire_lease,
    touch_activity,
)
from partgraph.util.lifecycle import (  # noqa: E402
    PARTGRAPH_CONTAINER_NAME,
    PARTGRAPH_DATA_VOLUME,
)

RUNNER = CliRunner()


def _invoke(args: list[str]):
    return RUNNER.invoke(app, args)


# ---------------------------------------------------------------------------
# Fixture builders — local copies, mirroring test_cli_db_down.py's own.
# ---------------------------------------------------------------------------


def _ps_row(container_id: str, name: str, image: str, *, state: str = "running",
            host_ports: tuple[int, ...] = ()) -> dict:
    return {
        "Id": container_id,
        "Names": [name],
        "Image": image,
        "State": state,
        "Ports": [
            {"host_ip": "127.0.0.1", "host_port": p, "container_port": 8080, "protocol": "tcp"}
            for p in host_ports
        ],
    }


def _mounts(*volume_names: str, destination: str = "/dgraph") -> list[dict]:
    return [{"Type": "volume", "Name": v, "Destination": destination} for v in volume_names]


class _Proc:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _is_ps_call(argv: list[str]) -> bool:
    return "ps" in argv and "--all" in argv


def _is_inspect_call(argv: list[str]) -> bool:
    return "container" in argv and "inspect" in argv


def _is_engine_stop_call(argv: list[str]) -> bool:
    return bool(argv) and argv[0] != "systemctl" and "stop" in argv and "compose" not in argv


def _is_compose_down_call(argv: list[str]) -> bool:
    return "compose" in argv and "down" in argv


def _is_systemctl_show_call(argv: list[str]) -> bool:
    return bool(argv) and argv[0] == "systemctl" and "show" in argv


_UNIT_NOT_FOUND_LINES = [
    "LoadState=not-found", "ActiveState=inactive", "SubState=dead",
    "UnitFileState=", "WantedBy=",
]


def _make_scripted_run(  # noqa: PLR0913 — one keyword-only knob per scriptable outcome.
    *,
    initial_rows: list[dict],
    mounts_by_id: dict[str, list[dict]] | None = None,
    unit_lines: list[str] | None = None,
    compose_removes_ids: frozenset[str] = frozenset(),
    stop_returncode: int = 0,
    stop_fails_ids: frozenset[str] = frozenset(),
):
    """A trimmed, local copy of test_cli_db_down.py's own stateful
    subprocess.run fake — only the knobs THIS file's scenarios need.
    `stop_fails_ids` (Gate 3a BLOCKING fix) makes ONE specific container's
    engine `stop` call fail while others succeed, so a genuine SURVIVOR can
    be scripted without failing every stop in the scenario."""
    mounts_by_id = mounts_by_id or {}
    unit_lines = unit_lines if unit_lines is not None else _UNIT_NOT_FOUND_LINES
    live: dict[str, dict] = {row["Id"]: row for row in initial_rows}
    calls: list[tuple[list[str], dict]] = []

    def _fake(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        if _is_systemctl_show_call(argv):
            return _Proc(stdout="\n".join(unit_lines))
        if _is_compose_down_call(argv):
            for cid in compose_removes_ids:
                live.pop(cid, None)
            return _Proc(returncode=0)
        if _is_inspect_call(argv):
            cid = argv[-1]
            return _Proc(stdout=json.dumps([{"Id": cid, "Mounts": mounts_by_id.get(cid, [])}]))
        if _is_ps_call(argv):
            return _Proc(stdout=json.dumps(list(live.values())))
        if _is_engine_stop_call(argv):
            target_id = argv[-1]
            if target_id in stop_fails_ids:
                return _Proc(returncode=1, stderr="stop failed")
            if target_id in live:
                live[target_id] = {**live[target_id], "State": "exited"}
            return _Proc(returncode=stop_returncode)
        raise AssertionError(f"unscripted subprocess.run call in test fixture: {argv}")

    return _fake, calls


def _which_systemctl_present(name: str) -> str | None:
    return "/usr/bin/systemctl" if name == "systemctl" else None


def _healthy(healthy: bool):
    return lambda: SimpleNamespace(healthy=healthy, message="probe")


def _forbid_any_subprocess(argv, **kwargs):
    raise AssertionError(f"unexpected subprocess.run call: {argv}")


def _forbid_ensure_running(*args, **kwargs):
    raise AssertionError(
        "ensure_running must never be called by `db idle-stop` — it is the "
        "opposite of a DB-touching command (C-12)"
    )


def _forbid_stop_all(*args, **kwargs):
    raise AssertionError(
        "stop_all must never be called when the idle decision says NOT to stop"
    )


def _seed_state_dir_stale_no_lease(state_dir: pathlib.Path) -> None:
    """Seed ACTIVITY_STATE_DIR with an ancient stamp and no lease — the
    ordinary "should stop" scenario."""
    touch_activity(state_dir=state_dir, now=lambda: _OLD)


import datetime as _dt_mod  # noqa: E402

_OLD = _dt_mod.datetime(2000, 1, 1, tzinfo=_dt_mod.UTC)


# ---------------------------------------------------------------------------
# _idle_timeout_minutes() — pure parsing table (C-9 rigor).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, DEFAULT_IDLE_TIMEOUT_MINUTES, id="unset"),
        pytest.param("", DEFAULT_IDLE_TIMEOUT_MINUTES, id="empty_string"),
        pytest.param("   ", DEFAULT_IDLE_TIMEOUT_MINUTES, id="whitespace_only"),
        pytest.param("banana", DEFAULT_IDLE_TIMEOUT_MINUTES, id="non_numeric_typo"),
        pytest.param("5m", DEFAULT_IDLE_TIMEOUT_MINUTES, id="non_numeric_unit_suffix"),
        pytest.param("30.5.5", DEFAULT_IDLE_TIMEOUT_MINUTES, id="malformed_float"),
        pytest.param("inf", DEFAULT_IDLE_TIMEOUT_MINUTES, id="infinity_falls_back_to_default"),
        pytest.param("-inf", DEFAULT_IDLE_TIMEOUT_MINUTES, id="neg_infinity_falls_back"),
        pytest.param("nan", DEFAULT_IDLE_TIMEOUT_MINUTES, id="nan_falls_back_to_default"),
        pytest.param("0", 0.0, id="zero_is_the_canonical_disabled_sentinel"),
        pytest.param("0.0", 0.0, id="zero_point_zero_is_also_disabled"),
        pytest.param("-1", 0.0, id="negative_is_treated_same_as_disabled"),
        pytest.param("-1000000", 0.0, id="large_negative_is_disabled_not_always_stale"),
        pytest.param("30", 30.0, id="the_documented_default_spelled_explicitly"),
        pytest.param("45.5", 45.5, id="ordinary_fractional_value"),
        pytest.param(" 45 ", 45.0, id="whitespace_padded_value_is_stripped"),
        pytest.param("999999999999", 999999999999.0, id="absurdly_large_accepted_verbatim_no_ceiling"),
    ],
)
def test_idle_timeout_minutes_parsing_table(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
) -> None:
    """Pinned parsing table (see module docstring for the full rationale)."""
    if raw is None:
        monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    else:
        monkeypatch.setenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raw)
    result = cli_mod._idle_timeout_minutes()
    assert isinstance(result, float)
    if math.isnan(expected):  # pragma: no cover — no NaN expected value in the table above
        assert math.isnan(result)
    else:
        assert result == pytest.approx(expected)


def test_idle_timeout_minutes_is_read_at_call_time_not_import_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given `partgraph.cli` is already imported (module import happened long
    before this test body runs).
    When `PARTGRAPH_IDLE_TIMEOUT_MINUTES` is set AFTER that import, then
    `_idle_timeout_minutes()` is called.
    Then it reflects the value set just now — proving `os.environ` is read
    live, never captured at import time."""
    monkeypatch.setenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", "17")
    assert cli_mod._idle_timeout_minutes() == pytest.approx(17.0)
    monkeypatch.setenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", "23")
    assert cli_mod._idle_timeout_minutes() == pytest.approx(23.0)


# ---------------------------------------------------------------------------
# C-9 — the escape hatch, at the CLI layer: a genuine zero-I/O no-op.
# ---------------------------------------------------------------------------


def test_idle_stop_disabled_is_a_genuine_zero_io_noop_exit_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """C-9 [property, not proxy]: Given `PARTGRAPH_IDLE_TIMEOUT_MINUTES=0`
    AND a state dir seeded with a stale stamp AND `PARTGRAPH_AUTOSTART` left
    ON (so the guarantee is proven against the harder case).
    When `partgraph db idle-stop` runs.
    Then it exits 0, `subprocess.run` is NEVER called (a fail-fast fake
    raises on any call — no `ps`, no `systemctl`, no engine detection at
    all), `ensure_running` is never called, and `stop_all` is never called.
    """
    monkeypatch.setenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", "0")
    monkeypatch.setenv("PARTGRAPH_AUTOSTART", "1")
    state_dir = tmp_path / "state"
    _seed_state_dir_stale_no_lease(state_dir)

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("subprocess.run", side_effect=_forbid_any_subprocess),
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
        patch("partgraph.cli.stop_all", side_effect=_forbid_stop_all),
        patch("partgraph.cli.probe_health", side_effect=AssertionError(
            "probe_health must never be called while idle-stop is disabled"
        )),
    ):
        result = _invoke(["db", "idle-stop"])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# C-4 (CLI level) — a live lease blocks the stop, unconditionally.
# ---------------------------------------------------------------------------


def test_idle_stop_live_lease_blocks_even_with_stale_stamp_never_calls_stop_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """C-4: Given a lease naming the REAL, currently-running pytest process
    (genuinely alive) and a very stale activity stamp.
    When `partgraph db idle-stop` runs.
    Then it exits 0 and `stop_all` is NEVER called (a fail-fast fake would
    raise) — the live lease blocks the stop before `stop_all` is ever
    reached, exactly as it does at the leaf level.
    """
    monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    state_dir = tmp_path / "state"
    acquire_lease(state_dir=state_dir, pid=os.getpid())
    _seed_state_dir_stale_no_lease(state_dir)

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("partgraph.cli.stop_all", side_effect=_forbid_stop_all),
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
    ):
        result = _invoke(["db", "idle-stop"])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# C-6 (CLI level) — a fresh stamp is a no-op, stop_all never called.
# ---------------------------------------------------------------------------


def test_idle_stop_fresh_stamp_is_a_noop_never_calls_stop_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    state_dir = tmp_path / "state"
    touch_activity(state_dir=state_dir)  # just now — fresh by construction

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("partgraph.cli.stop_all", side_effect=_forbid_stop_all),
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
    ):
        result = _invoke(["db", "idle-stop"])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# C-7 — must REUSE stop_all, inherited from PR-A, never re-derived.
# ---------------------------------------------------------------------------


def _assert_compose_down_issues_db_down_argv(compose_down) -> None:
    """Invoke *compose_down* IN ISOLATION and assert it issues `db down`'s
    own argv — proves genuine reuse of the SAME mechanism `down()` itself
    uses, never a parallel, look-alike implementation (mirrors
    `test_cli_autostart.py`'s own `_assert_compose_up_issues_db_up_argv`).
    """
    assert callable(compose_down)
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("subprocess.run", side_effect=_fake_run),
    ):
        compose_down()

    assert calls == [["docker", "compose", "-f", str(COMPOSE_FILE), "down"]], (
        f"idle-stop's compose_down must equal db down's own argv exactly, "
        f"never `-v`: {calls!r}"
    )


def test_idle_stop_stale_no_lease_delegates_to_the_real_stop_all_with_db_downs_own_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """C-7: Given a stale stamp and no lease (the "should stop" case).
    When `partgraph db idle-stop` runs.
    Then `partgraph.util.lifecycle.stop_all` (spied, not re-implemented) is
    called EXACTLY ONCE with `dry_run=False`, and its `compose_down` kwarg,
    when actually invoked, issues `db down`'s own argv byte-for-byte —
    proving inheritance of the S1/S2/S3 policy's ONE entry point, not a
    second, parallel "which containers are ours" implementation.
    """
    monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    state_dir = tmp_path / "state"
    _seed_state_dir_stale_no_lease(state_dir)

    from partgraph.util.lifecycle import DownResult

    captured: dict = {}

    def _spy_stop_all(**kwargs):
        captured.update(kwargs)
        return DownResult(
            stopped=(), skipped_foreign_port_holders=(), unit_stopped=False,
            survivors=(), still_serving_health=False, undetermined=(),
        )

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.stop_all", side_effect=_spy_stop_all) as mock_stop_all,
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
    ):
        result = _invoke(["db", "idle-stop"])

        assert result.exit_code == 0, result.output
        mock_stop_all.assert_called_once()
        assert captured.get("dry_run") is False
        assert captured.get("engine_prefix") == ["docker"]
        # [Gate 3b defect 2 fix] Must be asserted INSIDE this `with` block:
        # `partgraph.cli.probe_health` is patched here, so `cli_mod.probe_health`
        # currently IS the same MagicMock `idle_stop()` was called with — proving
        # the CLI threads through its OWN, live module attribute (never an
        # import-time-bound copy). Asserted after this block closes, the patch
        # would already be undone and `cli_mod.probe_health` would be the
        # restored REAL function — which the captured MagicMock could never be
        # identical to, making the assertion permanently, silently unwinnable
        # regardless of what src/ does.
        assert captured.get("probe_health") is cli_mod.probe_health

    _assert_compose_down_issues_db_down_argv(captured.get("compose_down"))


def test_idle_stop_end_to_end_real_stop_all_stops_the_partgraph_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """C-7 [end-to-end, the REAL stop_all, not a spy]: Given one genuine
    `partgraph-dgraph` instance running, a stale stamp, and no lease.
    When `partgraph db idle-stop` runs.
    Then the underlying engine `stop` call actually happens (targeting the
    container ID), and idle-stop exits 0.
    """
    monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    state_dir = tmp_path / "state"
    _seed_state_dir_stale_no_lease(state_dir)

    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4",
                   host_ports=(8081, 9081, 8001))
    fake_run, calls = _make_scripted_run(
        initial_rows=[row], mounts_by_id={"cid-1": _mounts(PARTGRAPH_DATA_VOLUME)},
    )

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("partgraph.cli.probe_health", side_effect=_healthy(True)),
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
    ):
        result = _invoke(["db", "idle-stop"])

    assert result.exit_code == 0, result.output
    stop_calls = [argv for argv, _k in calls if _is_engine_stop_call(argv)]
    assert len(stop_calls) == 1, f"expected exactly one engine stop call: {calls!r}"
    assert stop_calls[0][-1] == "cid-1", "the stop target must be the container id"


def test_idle_stop_exits_zero_but_prints_a_path_free_diagnostic_when_a_survivor_remains(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """[Gate 3a BLOCKING fix] Given the REAL `stop_all` sweep leaves a
    genuine SURVIVOR — the underlying engine `stop` call for the sole
    PartGraph instance FAILS (scripted via `stop_fails_ids`), so
    `stop_all()`'s own verification re-enumeration finds it still running.
    When `partgraph db idle-stop` runs.
    Then it STILL exits 0 (idle-stop's own "always exit 0, unattended"
    ruling) BUT its output is NOT empty and NOT silent: a path-free line
    naming the survivor state is printed (containing "survivor", matching
    `db down`'s own established vocabulary for the identical DownResult
    field, so an operator grepping `journalctl` for either command finds
    the same word) — "always exit 0" must never collapse into "always
    silent"; a genuinely failed stop must remain observable even though the
    unit itself is never reported as failed.
    """
    monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    state_dir = tmp_path / "state"
    _seed_state_dir_stale_no_lease(state_dir)

    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4",
                   host_ports=(8081, 9081, 8001))
    fake_run, _calls = _make_scripted_run(
        initial_rows=[row],
        mounts_by_id={"cid-1": _mounts(PARTGRAPH_DATA_VOLUME)},
        stop_fails_ids=frozenset({"cid-1"}),
    )

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("partgraph.cli.probe_health", side_effect=_healthy(True)),
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
    ):
        result = _invoke(["db", "idle-stop"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() != "", (
        "a genuinely-failed stop must never be reported silently, even "
        "though idle-stop still exits 0"
    )
    assert "survivor" in result.output.lower(), (
        f"expected the output to name the survivor state (matching db "
        f"down's own vocabulary): {result.output!r}"
    )
    assert PARTGRAPH_CONTAINER_NAME in result.output or "cid-1" not in result.output, (
        "the diagnostic must never leak the opaque container ID as if it "
        "were a path — display names only, exactly like db down"
    )
    assert "/" not in result.output, (
        f"the diagnostic line(s) must stay path-free: {result.output!r}"
    )


def test_idle_stop_exits_zero_but_prints_a_path_free_diagnostic_when_undetermined_remains(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """[Gate 3a BLOCKING fix] Mirrors the survivor test above for the
    DISTINCT `result.undetermined` case (an inspect failure during
    verification, never conflated with a confirmed survivor — see
    `test_cli_db_down.py`'s own Gate 5 finding A). Given the verification
    pass cannot confirm a row's ownership (a scripted `container inspect`
    failure) on a row that also holds a watched PartGraph port.
    When `partgraph db idle-stop` runs.
    Then it exits 0 but prints a NON-EMPTY, path-free diagnostic containing
    "could not verify" (never "survivor" — the two must stay textually
    distinct, mirroring `db down`'s own established distinction).
    """
    monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    state_dir = tmp_path / "state"
    _seed_state_dir_stale_no_lease(state_dir)

    row = _ps_row("cid-unknown", "systemd-partgraph-dgraph-duplicate",
                   "dgraph/standalone:v25.3.4", host_ports=(8081,))
    fake_run, _calls = _make_scripted_run(initial_rows=[row], mounts_by_id={})

    real_run = fake_run

    def _inspect_fails_on_verification(argv, **kwargs):
        if _is_inspect_call(argv):
            return _Proc(returncode=1, stderr="inspect failed")
        return real_run(argv, **kwargs)

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("partgraph.cli.probe_health", side_effect=_healthy(True)),
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("subprocess.run", side_effect=_inspect_fails_on_verification),
        patch("shutil.which", side_effect=_which_systemctl_present),
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
    ):
        result = _invoke(["db", "idle-stop"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() != ""
    assert "could not verify" in result.output.lower(), (
        f"expected an undetermined-state diagnostic: {result.output!r}"
    )
    assert "/" not in result.output, (
        f"the diagnostic line(s) must stay path-free: {result.output!r}"
    )


def test_idle_stop_exits_zero_but_prints_a_path_free_diagnostic_when_stop_all_itself_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """[Coordinator follow-up, same always-exit-0-must-not-mean-always-silent
    property as the two tests above, for a THIRD, distinct path]: Given
    `stop_all` itself raises an UNEXPECTED exception — not one of its
    ordinary `DownResult` outcomes (survivors/undetermined), but a genuine
    failure a wedged engine or an OS-level error could produce, mirroring
    `down()`'s own defence-in-depth catch around the identical call. The
    exception's own message deliberately CONTAINS a path-shaped string, so
    this test also proves the printed diagnostic is a FIXED, clean line —
    never the raw exception message forwarded verbatim (mirrors `down()`'s
    own "Turn it into a fixed, path-free message... so no raw traceback can
    leak an internal path").
    When `partgraph db idle-stop` runs.
    Then it still exits 0 (extending idle-stop's own always-exit-0 ruling to
    this failure mode too — an unattended background command must not look
    like a failed systemd unit merely because one sweep hit an unexpected
    error) AND prints a NON-EMPTY, path-free diagnostic. This is the
    property `except Exception: ...` around `stop_all` could otherwise
    absorb silently: every OTHER test in this file that reaches `stop_all`
    supplies either a clean `DownResult` or one of the two SURVIVOR/
    UNDETERMINED outcomes above, so none of them alone proves this
    third path — a `_forbid_stop_all`-shaped fake is only ever reachable on
    a branch that never calls `stop_all` at all, and would not catch a
    regression here either.
    """
    monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    state_dir = tmp_path / "state"
    _seed_state_dir_stale_no_lease(state_dir)

    def _raise(*args, **kwargs):
        raise RuntimeError(
            "simulated wedged-engine failure, see /some/fake/compose/path/docker-compose.yml"
        )

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("partgraph.cli.probe_health", side_effect=_healthy(True)),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.stop_all", side_effect=_raise),
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
    ):
        result = _invoke(["db", "idle-stop"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() != "", (
        "a genuine exception raised by stop_all must never be silently absorbed"
    )
    assert "Traceback" not in result.output, (
        f"no raw traceback may reach the operator: {result.output!r}"
    )
    assert "docker-compose.yml" not in result.output and "/" not in result.output, (
        f"the raw exception message (which carried a path) must never be "
        f"forwarded verbatim: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# C-12 — negatives: reuses PR-A's real cve-graph fixture, never rm/prune,
# never autostarts.
# ---------------------------------------------------------------------------


def test_idle_stop_cve_graph_fixture_never_touched_and_never_named_in_any_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """C-12 [PR-A's real, observed-host cve-graph fixture, reused verbatim —
    see `test_cli_db_down.py`'s own A6]: Given the REAL observed host state —
    zero PartGraph containers, and exactly: min-web (nginx:1.27.3), cve-ratel
    (dgraph/ratel:latest), cve-zero (dgraph/dgraph:v24.0.0), cve-alpha
    (dgraph/dgraph:v24.0.0), cve-loader (localhost/cve-loader:latest), and a
    stale stamp with no lease (the "should stop" case, which is the ONE
    where a bug could actually touch something).
    When `partgraph db idle-stop` runs.
    Then no argv anywhere in the entire call list contains any cve-graph
    name, any `cve-graph_*` volume, or `systemd-cve-graph`; `stop`/`rm`/
    `kill`/`prune`/`-v`/`--volumes` never appear; exit code is 0.
    """
    monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    state_dir = tmp_path / "state"
    _seed_state_dir_stale_no_lease(state_dir)

    rows = [
        _ps_row("cid-minweb", "min-web", "nginx:1.27.3", host_ports=(18080,)),
        _ps_row("cid-ratel", "cve-ratel", "dgraph/ratel:latest", host_ports=(18000,)),
        _ps_row("cid-zero", "cve-zero", "dgraph/dgraph:v24.0.0", host_ports=(15080,)),
        _ps_row("cid-alpha", "cve-alpha", "dgraph/dgraph:v24.0.0", host_ports=(18081, 19081)),
        _ps_row("cid-loader", "cve-loader", "localhost/cve-loader:latest"),
    ]
    mounts_by_id = {
        "cid-minweb": [], "cid-ratel": [],
        "cid-zero": _mounts("cve-graph_dgraph_zero"),
        "cid-alpha": _mounts("cve-graph_dgraph_alpha"),
        "cid-loader": [],
    }
    fake_run, calls = _make_scripted_run(initial_rows=rows, mounts_by_id=mounts_by_id)

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
    ):
        result = _invoke(["db", "idle-stop"])

    assert result.exit_code == 0, result.output
    forbidden = {
        "min-web", "cve-ratel", "cve-zero", "cve-alpha", "cve-loader",
        "cve-graph_dgraph_zero", "cve-graph_dgraph_alpha", "systemd-cve-graph",
    }
    for argv, _kwargs in calls:
        argv_text = " ".join(argv)
        for token in forbidden:
            assert token not in argv_text, f"forbidden token {token!r} leaked into argv: {argv}"
        assert not _is_engine_stop_call(argv)
        assert "rm" not in argv
        assert "kill" not in argv
        assert "prune" not in argv
        assert "-v" not in argv
        assert "--volumes" not in argv


def test_idle_stop_never_autostarts_regardless_of_partgraph_autostart_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """C-12: Given `PARTGRAPH_AUTOSTART=1` (autostart explicitly ON — the
    HARDER case for this guarantee) and the "should stop" scenario (stale
    stamp, no lease, nothing currently running).
    When `partgraph db idle-stop` runs.
    Then `ensure_running` is never called — proven with a fail-fast fake, not
    merely by absence of evidence."""
    monkeypatch.setenv("PARTGRAPH_AUTOSTART", "1")
    monkeypatch.delenv("PARTGRAPH_IDLE_TIMEOUT_MINUTES", raising=False)
    state_dir = tmp_path / "state"
    _seed_state_dir_stale_no_lease(state_dir)
    fake_run, _calls = _make_scripted_run(initial_rows=[])

    with (
        patch.object(cli_mod, "ACTIVITY_STATE_DIR", state_dir),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
        patch("partgraph.cli.ensure_running", side_effect=_forbid_ensure_running),
    ):
        result = _invoke(["db", "idle-stop"])

    assert result.exit_code == 0, result.output
