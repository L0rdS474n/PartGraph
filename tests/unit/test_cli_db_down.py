"""
Tests: PR-A (fix/db-down-all-instances) — `partgraph db down` end-to-end.

A single `partgraph db down` must leave ZERO PartGraph instances running — for
0, 1 or N instances, regardless of which lifecycle owner started them (Compose
vs. the quadlet/systemd unit `partgraph-dgraph.service`) — while PROVABLY
never touching an unrelated cve-graph stack.

Pinned CLI contract this file exercises (NOT YET IMPLEMENTED — collection of
THIS file is expected to ERROR with ModuleNotFoundError until
partgraph.util.lifecycle exists, mirroring tests/unit/test_health.py's
original design before partgraph.util.health existed):

  - `db down` gains a new `--dry-run` flag (default False).
  - `down()` resolves the engine prefix via `partgraph.cli.engine_command()`
    (a NEW import into cli.py, alongside the existing `compose_command`);
    a `ContainerEngineError` here is caught exactly like `db up`'s existing
    handling (one clean stderr "Error" message, exit 1, no traceback; A13
    additionally covers the pre-existing `compose_command()` failure path).
  - The REST of the work is delegated to ONE call:
    `partgraph.util.lifecycle.stop_all(engine_prefix=..., compose_down=lambda:
    _run_compose(["down"], action="stop"), probe_health=probe_health,
    dry_run=dry_run) -> DownResult`. Passing cli.py's OWN module-level
    `probe_health` reference (already imported for `db status`, ADR-0018) as
    the injected seam is what lets `patch("partgraph.cli.probe_health", ...)`
    reach the leaf's health check without the leaf importing anything from
    partgraph.cli (mirrors the `get_encoder` threading discipline documented
    at the top of cli.py).
  - `stop_all()`'s internal order is EXACTLY: (1) if the systemd unit is
    present+active, `systemctl --user stop <PARTGRAPH_UNIT_NAME>`; (2) the
    injected `compose_down` callback (`<engine> compose -f <abs> down`,
    never `-v`); (3) `<engine> stop -t <n> <name>` for every SURVIVING S1/S2
    instance (S3 port holders are NEVER stopped); (4) a final verification
    re-enumeration. Every one of these is skipped under `dry_run=True` except
    the READ-ONLY unit_state()/find_partgraph_instances() calls.
  - Exit-code formula: `result.survivors` non-empty -> exit 1 (A8); else if
    `result.still_serving_health` -> exit 0 + one advisory stderr line (A9);
    else exit 0. `--dry-run` always exits 0.

HERMETICITY (HARD CONSTRAINT): every test in this file patches ONLY
`subprocess.run`, `partgraph.cli.compose_command`, `partgraph.cli.
engine_command` and (where relevant) `partgraph.cli.probe_health` /
`shutil.which` — never `partgraph.util.lifecycle.*` directly. This means the
REAL `find_partgraph_instances()`/`unit_state()`/`stop_all()` implementations
run in every test, driven end-to-end by a single scripted, STATEFUL
`subprocess.run` fake (`_make_scripted_run` below) that mutates its own
in-memory container set as `stop`/`compose down`/`systemctl stop` calls
"succeed" — exactly mirroring how a real engine would make a container
disappear from the NEXT `ps` call. No test opens a real socket, starts a real
container, sleeps, or reads the real wall clock.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from partgraph.cli import app
from partgraph.util.container import ContainerEngineError

# This import is expected to raise ModuleNotFoundError until
# src/partgraph/util/lifecycle.py exists — the correct test-first red state.
from partgraph.util.lifecycle import (  # noqa: E402
    PARTGRAPH_CONTAINER_NAME,
    PARTGRAPH_DATA_VOLUME,
    PARTGRAPH_UNIT_NAME,
)

RUNNER = CliRunner()


def _invoke(args: list[str]):
    return RUNNER.invoke(app, args)


# ---------------------------------------------------------------------------
# Fixture builders (deliberately independent copies of test_lifecycle.py's —
# each test file stays self-contained; mirrors cli.py's own documented
# convention of copying small UID/cursor helpers across sections rather than
# sharing internals across independently-readable modules).
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


def _is_systemctl_stop_call(argv: list[str]) -> bool:
    return bool(argv) and argv[0] == "systemctl" and "stop" in argv and "show" not in argv


_UNIT_NOT_FOUND_LINES = [
    "LoadState=not-found", "ActiveState=inactive", "SubState=dead",
    "UnitFileState=", "WantedBy=",
]
_UNIT_ACTIVE_LINES = [
    "LoadState=loaded", "ActiveState=active", "SubState=running",
    "UnitFileState=generated", "WantedBy=default.target",
]


def _make_scripted_run(  # noqa: PLR0913 — one keyword-only knob per scriptable outcome.
    *,
    initial_rows: list[dict],
    mounts_by_id: dict[str, list[dict]] | None = None,
    unit_lines: list[str] | None = None,
    compose_returncode: int = 0,
    compose_removes_ids: frozenset[str] = frozenset(),
    systemctl_stop_returncode: int = 0,
    systemctl_stop_removes_ids: frozenset[str] = frozenset(),
    stop_returncode: int = 0,
    stop_fails_names: frozenset[str] = frozenset(),
    systemctl_stop_raises: Exception | None = None,
    ps_raises_on_call_index: int | None = None,
):
    """Return (fake_run, calls) — a STATEFUL subprocess.run replacement.

    ``live`` is the in-memory {container_id: row} set; a successful
    stop/compose-down/systemctl-stop mutates it exactly as a real engine
    would, so a SECOND `ps` call (the verification re-enumeration) genuinely
    reflects what survived. ``calls`` records every (argv, kwargs) pair in
    order for ordering/timeout/argv assertions.
    """
    mounts_by_id = mounts_by_id or {}
    unit_lines = unit_lines if unit_lines is not None else _UNIT_NOT_FOUND_LINES
    live: dict[str, dict] = {row["Id"]: row for row in initial_rows}
    calls: list[tuple[list[str], dict]] = []
    ps_call_count = 0

    def _fake(argv, **kwargs):  # noqa: PLR0911, PLR0912 — one branch per scriptable verb.
        nonlocal ps_call_count
        calls.append((list(argv), dict(kwargs)))
        if _is_systemctl_show_call(argv):
            return _Proc(stdout="\n".join(unit_lines))
        if _is_systemctl_stop_call(argv):
            if systemctl_stop_raises is not None:
                raise systemctl_stop_raises
            if systemctl_stop_returncode == 0:
                for cid in systemctl_stop_removes_ids:
                    live.pop(cid, None)
            return _Proc(returncode=systemctl_stop_returncode)
        if _is_compose_down_call(argv):
            if compose_returncode == 0:
                for cid in compose_removes_ids:
                    live.pop(cid, None)
            return _Proc(returncode=compose_returncode)
        if _is_inspect_call(argv):
            cid = argv[-1]
            return _Proc(stdout=json.dumps([{"Id": cid, "Mounts": mounts_by_id.get(cid, [])}]))
        if _is_ps_call(argv):
            ps_call_count += 1
            if ps_raises_on_call_index == ps_call_count:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=10)
            return _Proc(stdout=json.dumps(list(live.values())))
        if _is_engine_stop_call(argv):
            name = argv[-1]
            if name in stop_fails_names:
                return _Proc(returncode=1, stderr="stop failed")
            for cid, row in list(live.items()):
                if row["Names"][0] == name:
                    live.pop(cid, None)
            return _Proc(returncode=stop_returncode)
        raise AssertionError(f"unscripted subprocess.run call in test fixture: {argv}")

    return _fake, calls


def _which_systemctl_present(name: str) -> str | None:
    return "/usr/bin/systemctl" if name == "systemctl" else None


def _which_systemctl_absent(name: str) -> str | None:
    return None


def _healthy(healthy: bool):
    return lambda: SimpleNamespace(healthy=healthy, message="probe")


# ---------------------------------------------------------------------------
# A1 — zero instances, unit inactive/not-found
# ---------------------------------------------------------------------------


def test_a1_zero_instances_and_no_unit_no_stop_call_exit_zero() -> None:
    """A1: Given zero PartGraph instances exist and the systemd unit is
    not-found.
    When `partgraph db down` runs.
    Then NO `stop` subprocess call happens at all (neither engine `stop` nor
    `systemctl ... stop`), stdout says nothing was running and the volume is
    preserved, and the exit code is 0.
    """
    fake_run, calls = _make_scripted_run(initial_rows=[])
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output
    assert "-v" not in " ".join(" ".join(a) for a, _k in calls).split()
    for argv, _kwargs in calls:
        assert not _is_engine_stop_call(argv), f"unexpected engine stop call: {argv}"
        assert not _is_systemctl_stop_call(argv), f"unexpected systemctl stop call: {argv}"
    assert "volume" in result.output.lower() or "preserved" in result.output.lower()


# ---------------------------------------------------------------------------
# A2 — one running partgraph-dgraph, no active unit
# ---------------------------------------------------------------------------


def test_a2_single_compose_owned_instance_compose_down_once_no_v_exit_zero() -> None:
    """A2: Given one running `partgraph-dgraph` container that Compose itself
    created (so `compose down` alone fully removes it), and the systemd unit
    is not active.
    When `partgraph db down` runs.
    Then `<engine> compose -f <abs> down` is invoked EXACTLY once, WITHOUT
    `-v`, verification re-enumeration follows and finds it gone, and the exit
    code is 0.
    """
    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4",
                   host_ports=(8081, 9081, 8001))
    fake_run, calls = _make_scripted_run(
        initial_rows=[row],
        mounts_by_id={"cid-1": _mounts(PARTGRAPH_DATA_VOLUME)},
        compose_removes_ids=frozenset({"cid-1"}),
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output
    compose_calls = [argv for argv, _k in calls if _is_compose_down_call(argv)]
    assert len(compose_calls) == 1, f"expected exactly one compose down call, got {compose_calls}"
    for argv in compose_calls:
        assert "-v" not in argv
    stop_calls = [argv for argv, _k in calls if _is_engine_stop_call(argv)]
    assert stop_calls == [], (
        f"compose already removed the sole instance; no engine stop should fire: {stop_calls}"
    )
    ps_calls = [argv for argv, _k in calls if _is_ps_call(argv)]
    assert len(ps_calls) >= 1, "expected at least one verification re-enumeration ps call"


# ---------------------------------------------------------------------------
# A3 — active unit -> systemctl --user stop, frozen unit name, list argv
# ---------------------------------------------------------------------------


def test_a3_active_systemd_unit_stopped_via_frozen_unit_name() -> None:
    """A3: Given `systemctl --user show` reports the unit LoadState=loaded,
    ActiveState=active.
    When `partgraph db down` runs.
    Then `systemctl --user stop partgraph-dgraph.service` is invoked as a
    LIST argv with `shell=False`, containing neither 'sudo' nor '--system',
    and the unit name is the frozen module constant — never derived from any
    ps/engine output.
    """
    fake_run, calls = _make_scripted_run(
        initial_rows=[], unit_lines=_UNIT_ACTIVE_LINES,
        systemctl_stop_removes_ids=frozenset(),
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        _invoke(["db", "down"])

    stop_calls = [(argv, kwargs) for argv, kwargs in calls if _is_systemctl_stop_call(argv)]
    assert len(stop_calls) == 1, f"expected exactly one systemctl stop call, got {stop_calls}"
    argv, kwargs = stop_calls[0]
    assert isinstance(argv, list)
    assert kwargs.get("shell", False) is False
    assert "sudo" not in argv
    assert argv[0] != "sudo"
    assert "--system" not in argv
    assert "--user" in argv
    assert PARTGRAPH_UNIT_NAME in argv
    assert PARTGRAPH_UNIT_NAME == "partgraph-dgraph.service"


# ---------------------------------------------------------------------------
# A4 — both owners present: EXACT call order
# ---------------------------------------------------------------------------


def test_a4_call_order_is_systemd_then_compose_then_engine_stop_then_verify() -> None:
    """A4: Given the systemd unit is active AND its associated
    `partgraph-dgraph` container survives BOTH the systemd stop (unit-level
    success, container lingers) and `compose down` (compose never tracked a
    container it did not create — the real ADR incident's root cause: the
    compose project name on this host is literally 'docker', so Compose does
    not recognise a quadlet-created container as its own).
    When `partgraph db down` runs.
    Then the recorded classified call order is EXACTLY: (1) systemd unit
    stop, (2) compose down, (3) engine `stop` of the surviving instance, (4)
    the final verification re-enumeration — asserted directly against
    `mock_run.call_args_list` (a reordering of ANY of these four phases must
    fail this test).
    """
    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4",
                   host_ports=(8081, 9081, 8001))
    fake_run, _calls = _make_scripted_run(
        initial_rows=[row],
        mounts_by_id={"cid-1": []},
        unit_lines=_UNIT_ACTIVE_LINES,
        systemctl_stop_removes_ids=frozenset(),   # unit-level success, container lingers
        compose_removes_ids=frozenset(),          # compose never created/tracks it
    )
    with (
        patch("subprocess.run", side_effect=fake_run) as mock_run,
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output
    call_list = mock_run.call_args_list
    assert call_list, "subprocess.run was never called"

    kinds: list[str] = []
    for c in call_list:
        argv = list(c.args[0]) if c.args else list(c.kwargs.get("args", []))
        if _is_systemctl_stop_call(argv):
            kinds.append("systemctl_stop")
        elif _is_compose_down_call(argv):
            kinds.append("compose_down")
        elif _is_engine_stop_call(argv):
            kinds.append("engine_stop")
        elif _is_ps_call(argv):
            kinds.append("ps")
        elif _is_inspect_call(argv) or _is_systemctl_show_call(argv):
            kinds.append("read")
        else:
            kinds.append("other")

    assert "systemctl_stop" in kinds
    assert "compose_down" in kinds
    assert "engine_stop" in kinds
    assert "ps" in kinds

    systemctl_stop_idx = kinds.index("systemctl_stop")
    compose_down_idx = kinds.index("compose_down")
    engine_stop_idx = kinds.index("engine_stop")
    last_ps_idx = max(i for i, k in enumerate(kinds) if k == "ps")

    assert systemctl_stop_idx < compose_down_idx, (
        f"systemd unit stop must precede compose down: {kinds}"
    )
    assert compose_down_idx < engine_stop_idx, (
        f"compose down must precede the engine stop sweep: {kinds}"
    )
    assert engine_stop_idx < last_ps_idx, (
        f"the FINAL verification re-enumeration must be strictly after every "
        f"engine stop call: {kinds}"
    )


# ---------------------------------------------------------------------------
# A5 — two matching containers (S1 name + S2 volume)
# ---------------------------------------------------------------------------


def test_a5_two_matching_containers_each_stopped_once_exit_zero() -> None:
    """A5: Given two containers survive compose down: one matching by exact
    name (S1) and one matching by mounted data volume (S2, a different
    name).
    When `partgraph db down` runs.
    Then both names appear in exactly one `stop` argv each, and the exit
    code is 0 only because verification finds neither surviving.
    """
    row_s1 = _ps_row("cid-s1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    row_s2 = _ps_row("cid-s2", "systemd-partgraph-dgraph-duplicate", "dgraph/standalone:v25.3.4")
    fake_run, calls = _make_scripted_run(
        initial_rows=[row_s1, row_s2],
        mounts_by_id={"cid-s1": [], "cid-s2": _mounts(PARTGRAPH_DATA_VOLUME)},
        compose_removes_ids=frozenset(),
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output
    stop_calls = [argv for argv, _k in calls if _is_engine_stop_call(argv)]
    stopped_targets = [argv[-1] for argv in stop_calls]
    assert stopped_targets.count(PARTGRAPH_CONTAINER_NAME) == 1
    assert stopped_targets.count("systemd-partgraph-dgraph-duplicate") == 1
    assert len(stop_calls) == 2


# ---------------------------------------------------------------------------
# A6 (HIGHEST PRIORITY NEGATIVE) — cve-graph fixture, real observed names
# ---------------------------------------------------------------------------


def test_a6_cve_graph_fixture_never_touched_and_never_named_in_any_argv() -> None:
    """A6: Given the REAL observed host state (names/images only) — zero
    PartGraph containers, and exactly: min-web (nginx:1.27.3), cve-ratel
    (dgraph/ratel:latest), cve-zero (dgraph/dgraph:v24.0.0), cve-alpha
    (dgraph/dgraph:v24.0.0), cve-loader (localhost/cve-loader:latest), on
    host ports distinct from PARTGRAPH_WATCHED_PORTS.
    When `partgraph db down` runs.
    Then NO argv anywhere in the ENTIRE call_args_list contains any of those
    names, any `cve-graph_*` volume, or `systemd-cve-graph`; no
    `stop`/`rm`/`kill`/`prune` is invoked; the exit code is 0.
    """
    rows = [
        _ps_row("cid-minweb", "min-web", "nginx:1.27.3", host_ports=(18080,)),
        _ps_row("cid-ratel", "cve-ratel", "dgraph/ratel:latest", host_ports=(18000,)),
        _ps_row("cid-zero", "cve-zero", "dgraph/dgraph:v24.0.0", host_ports=(15080,)),
        _ps_row("cid-alpha", "cve-alpha", "dgraph/dgraph:v24.0.0", host_ports=(18081, 19081)),
        _ps_row("cid-loader", "cve-loader", "localhost/cve-loader:latest"),
    ]
    mounts_by_id = {
        "cid-minweb": [],
        "cid-ratel": [],
        "cid-zero": _mounts("cve-graph_dgraph_zero"),
        "cid-alpha": _mounts("cve-graph_dgraph_alpha"),
        "cid-loader": [],
    }
    fake_run, calls = _make_scripted_run(initial_rows=rows, mounts_by_id=mounts_by_id)
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

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


# ---------------------------------------------------------------------------
# A7 — parametrized: no destructive volume/verb flags in ANY scenario
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rows,mounts,unit_lines,compose_removes,systemctl_removes",
    [
        pytest.param([], {}, _UNIT_NOT_FOUND_LINES, frozenset(), frozenset(), id="a1-zero"),
        pytest.param(
            [_ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")],
            {"cid-1": []}, _UNIT_NOT_FOUND_LINES, frozenset({"cid-1"}), frozenset(),
            id="a2-single-compose-owned",
        ),
        pytest.param(
            [_ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4"),
             _ps_row("cid-2", "systemd-partgraph-dgraph-duplicate", "dgraph/standalone:v25.3.4")],
            {"cid-1": [], "cid-2": _mounts(PARTGRAPH_DATA_VOLUME)},
            _UNIT_NOT_FOUND_LINES, frozenset(), frozenset(),
            id="a5-two-matches",
        ),
    ],
)
def test_a7_no_volume_destroying_flags_across_ac_scenarios(
    rows, mounts, unit_lines, compose_removes, systemctl_removes,
) -> None:
    """A7: Given each of the A1/A2/A5 scenarios above (parametrized).
    When `partgraph db down` runs.
    Then NO argv anywhere contains `-v`, `--volumes`, `volume rm`,
    `system prune`, or the verb `rm`.
    """
    fake_run, calls = _make_scripted_run(
        initial_rows=rows, mounts_by_id=mounts, unit_lines=unit_lines,
        compose_removes_ids=compose_removes, systemctl_stop_removes_ids=systemctl_removes,
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        _invoke(["db", "down"])

    for argv, _kwargs in calls:
        assert "-v" not in argv
        assert "--volumes" not in argv
        assert "rm" not in argv
        argv_text = " ".join(argv)
        assert "volume rm" not in argv_text
        assert "system prune" not in argv_text


# ---------------------------------------------------------------------------
# A8 — a surviving instance after verification -> exit 1, path-free stderr
# ---------------------------------------------------------------------------


def test_a8_surviving_instance_after_verification_exits_one_and_names_it() -> None:
    """A8: Given the sole S1 instance's `stop` call itself fails (the engine
    refuses/times out at the process level), so it is STILL present at
    verification.
    When `partgraph db down` runs.
    Then the exit code is 1 and stderr names the survivor on a single,
    path-free line.
    """
    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    fake_run, _calls = _make_scripted_run(
        initial_rows=[row], mounts_by_id={"cid-1": []},
        compose_removes_ids=frozenset(), stop_fails_names=frozenset({PARTGRAPH_CONTAINER_NAME}),
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 1
    assert PARTGRAPH_CONTAINER_NAME in result.output
    error_lines = [ln for ln in result.output.splitlines() if PARTGRAPH_CONTAINER_NAME in ln]
    assert error_lines, "survivor name not found in any output line"
    for ln in error_lines:
        assert "/" not in ln, f"survivor line leaks a path: {ln!r}"


def test_a8_empty_verification_exits_zero() -> None:
    """A8: Given the sole S1 instance is successfully stopped (verification
    is empty).
    When `partgraph db down` runs.
    Then the exit code is 0.
    """
    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    fake_run, _calls = _make_scripted_run(
        initial_rows=[row], mounts_by_id={"cid-1": []}, compose_removes_ids=frozenset(),
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# A9 — verification empty but the health port still answers 200
# ---------------------------------------------------------------------------


def test_a9_verification_empty_but_health_still_200_exit_zero_with_advisory() -> None:
    """A9: Given verification finds NOTHING surviving (zero instances the
    whole run) but Dgraph's health endpoint still answers 200 — served by
    something PartGraph does not own/control via containers.
    When `partgraph db down` runs.
    Then the exit code is 0, plus EXACTLY one advisory stderr line stating
    the port is served by something PartGraph does not own.
    """
    fake_run, _calls = _make_scripted_run(initial_rows=[])
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(True)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output
    advisory_lines = [
        ln for ln in result.output.splitlines()
        if "does not own" in ln.lower() or "not own" in ln.lower()
    ]
    assert len(advisory_lines) == 1, (
        f"expected exactly one advisory line about an unowned port responder, "
        f"got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# A10 — no systemctl on PATH
# ---------------------------------------------------------------------------


def test_a10_systemctl_absent_skips_systemd_step_compose_and_sweep_still_run() -> None:
    """A10: Given `shutil.which("systemctl")` returns None (no systemd on
    this host at all).
    When `partgraph db down` runs (with one S1 survivor still needing an
    engine-level stop).
    Then the systemd step is silently skipped (NO `systemctl` call of any
    kind — not even `show`), compose down and the engine-stop sweep still
    run, no exception is raised, and the exit code follows A8.
    """
    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    fake_run, calls = _make_scripted_run(
        initial_rows=[row], mounts_by_id={"cid-1": []}, compose_removes_ids=frozenset(),
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_absent),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    systemctl_calls = [argv for argv, _k in calls if argv and argv[0] == "systemctl"]
    assert systemctl_calls == [], f"systemctl must never be invoked: {systemctl_calls}"
    assert any(_is_compose_down_call(argv) for argv, _k in calls)
    assert any(_is_engine_stop_call(argv) for argv, _k in calls)


# ---------------------------------------------------------------------------
# A11 — unit not-found
# ---------------------------------------------------------------------------


def test_a11_unit_not_found_no_systemctl_stop_run_proceeds_cleanly() -> None:
    """A11: Given `systemctl --user show` reports LoadState=not-found.
    When `partgraph db down` runs.
    Then `systemctl ... stop` is NEVER invoked (though `show` legitimately
    was, to discover not-found in the first place), and the run proceeds
    cleanly to exit 0.
    """
    fake_run, calls = _make_scripted_run(initial_rows=[], unit_lines=_UNIT_NOT_FOUND_LINES)
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output
    assert not any(_is_systemctl_stop_call(argv) for argv, _k in calls)
    assert any(_is_systemctl_show_call(argv) for argv, _k in calls)


# ---------------------------------------------------------------------------
# A12 — systemctl --user stop fails or times out
# ---------------------------------------------------------------------------


def test_a12_systemctl_stop_nonzero_exit_compose_and_sweep_still_execute() -> None:
    """A12: Given `systemctl --user stop` exits non-zero.
    When `partgraph db down` runs (with a surviving S1 instance that DOES get
    stopped by the engine-level sweep).
    Then the failure is reported path-free, compose down and the sweep still
    execute, and the exit code follows A8 (here: exit 0, since the survivor
    is stopped by the sweep despite the systemd failure).
    """
    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    fake_run, calls = _make_scripted_run(
        initial_rows=[row], mounts_by_id={"cid-1": []},
        unit_lines=_UNIT_ACTIVE_LINES, systemctl_stop_returncode=1,
        compose_removes_ids=frozenset(),
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert any(_is_compose_down_call(argv) for argv, _k in calls)
    assert any(_is_engine_stop_call(argv) for argv, _k in calls)


def test_a12_systemctl_stop_times_out_compose_and_sweep_still_execute() -> None:
    """A12: Given `systemctl --user stop` raises subprocess.TimeoutExpired.
    When `partgraph db down` runs.
    Then no traceback reaches the user, compose down and the sweep still
    execute, and the exit code follows A8.
    """
    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    fake_run, calls = _make_scripted_run(
        initial_rows=[row], mounts_by_id={"cid-1": []},
        unit_lines=_UNIT_ACTIVE_LINES,
        systemctl_stop_raises=subprocess.TimeoutExpired(cmd=["systemctl"], timeout=10),
        compose_removes_ids=frozenset(),
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert any(_is_compose_down_call(argv) for argv, _k in calls)
    assert any(_is_engine_stop_call(argv) for argv, _k in calls)


# ---------------------------------------------------------------------------
# A13 — compose_command() raises ContainerEngineError (preserve existing)
# ---------------------------------------------------------------------------


def test_a13_compose_command_raises_container_engine_error_exit_one_no_traceback() -> None:
    """A13: Given engine_command() succeeds (a usable engine IS on PATH) but
    compose_command() raises ContainerEngineError (mirrors the EXISTING
    `db up` behaviour pinned by test_cli.py's
    test_db_up_exits_cleanly_when_no_engine_available).
    When `partgraph db down` runs.
    Then exactly one clean stderr "Error" message is printed, the exit code
    is 1, and no traceback ever reaches the user's terminal.
    """
    fake_run, _calls = _make_scripted_run(initial_rows=[])
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch(
            "partgraph.cli.compose_command",
            side_effect=ContainerEngineError("no engine"),
        ),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == 1
    assert "Error" in result.output
    assert "Traceback" not in result.output
    if result.exception is not None:
        assert not isinstance(result.exception, ContainerEngineError)


# ---------------------------------------------------------------------------
# A14 — hostile enumerated name never reaches the engine
# ---------------------------------------------------------------------------


def test_a14_hostile_container_name_rejected_never_reaches_engine_or_systemctl() -> None:
    """A14: Given `ps` returns a container whose name starts with '-' AND
    mounts the PartGraph data volume (it would otherwise be a legitimate S2
    match).
    When `partgraph db down` runs.
    Then the hostile string is rejected — it never appears in ANY subprocess
    argv anywhere (engine OR systemctl), and no engine-derived string is ever
    used to build the systemctl argument (that argument is always the frozen
    PARTGRAPH_UNIT_NAME constant).
    """
    hostile = "-rf;rm-rf-slash"
    row = _ps_row("cid-hostile", hostile, "dgraph/standalone:v25.3.4")
    fake_run, calls = _make_scripted_run(
        initial_rows=[row], mounts_by_id={"cid-hostile": _mounts(PARTGRAPH_DATA_VOLUME)},
        unit_lines=_UNIT_ACTIVE_LINES,
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert "Traceback" not in result.output
    for argv, _kwargs in calls:
        for token in argv:
            assert hostile not in token, f"hostile name leaked into argv: {argv}"
        if argv and argv[0] == "systemctl" and "stop" in argv:
            assert PARTGRAPH_UNIT_NAME in argv
            assert hostile not in argv


# ---------------------------------------------------------------------------
# A15 — bounded timeouts everywhere; a timeout never hangs the CLI
# ---------------------------------------------------------------------------


def test_a15_every_call_carries_a_finite_bounded_timeout() -> None:
    """A15: Given a full clean run (one S1 survivor, active unit).
    When `partgraph db down` runs.
    Then EVERY subprocess.run call — systemctl, compose, ps, inspect, stop —
    carries a finite, positive `timeout=` kwarg.
    """
    row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    fake_run, calls = _make_scripted_run(
        initial_rows=[row], mounts_by_id={"cid-1": []},
        unit_lines=_UNIT_ACTIVE_LINES, systemctl_stop_removes_ids=frozenset(),
        compose_removes_ids=frozenset(),
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        _invoke(["db", "down"])

    assert calls
    for argv, kwargs in calls:
        timeout = kwargs.get("timeout")
        assert timeout is not None, f"call missing timeout=: {argv}"
        assert isinstance(timeout, int | float)
        assert timeout > 0


def test_a15_a_hanging_ps_call_times_out_cleanly_never_hangs() -> None:
    """A15: Given the FIRST `ps` enumeration call raises
    subprocess.TimeoutExpired (an engine wedged/hung).
    When `partgraph db down` runs.
    Then the command exits non-zero cleanly — never hangs, never raises an
    unhandled exception out of the CliRunner.
    """
    fake_run, _calls = _make_scripted_run(initial_rows=[], ps_raises_on_call_index=1)
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# A16 — --dry-run
# ---------------------------------------------------------------------------


def test_a16_dry_run_enumerates_only_no_mutating_call_exit_zero() -> None:
    """A16: Given one S1 survivor, one S3 port-holder, and an active systemd
    unit.
    When `partgraph db down --dry-run` runs.
    Then ONLY enumeration/inspection calls happen (ps/inspect/systemctl
    show); NO `stop` (engine or systemctl), NO `compose ... down` is ever
    invoked; stdout prints BOTH the would-stop set and the S3 report-only
    set; exit code is 0.
    """
    row_s1 = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    row_s3 = _ps_row("cid-3", "some-other-service", "nginx:1.27.3", host_ports=(8081,))
    fake_run, calls = _make_scripted_run(
        initial_rows=[row_s1, row_s3],
        mounts_by_id={"cid-1": [], "cid-3": []},
        unit_lines=_UNIT_ACTIVE_LINES,
    )
    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down", "--dry-run"])

    assert result.exit_code == 0, result.output
    for argv, _kwargs in calls:
        assert not _is_engine_stop_call(argv), f"dry-run must never stop: {argv}"
        assert not _is_systemctl_stop_call(argv), f"dry-run must never systemctl stop: {argv}"
        assert not _is_compose_down_call(argv), f"dry-run must never compose down: {argv}"
    assert PARTGRAPH_CONTAINER_NAME in result.output
    assert "some-other-service" in result.output


# ---------------------------------------------------------------------------
# Messages: single-line, path-free (across representative scenarios)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    ["clean_success", "survivor_error", "health_advisory"],
)
def test_down_messages_are_single_line_path_free(scenario: str) -> None:
    """Given the three representative `db down` outcomes (clean success, a
    surviving instance, and the health-still-served advisory).
    When `partgraph db down` runs.
    Then none of the command's own printed lines are multi-line or leak a
    filesystem path (no '/home', no leading '/').
    """
    if scenario == "clean_success":
        fake_run, _calls = _make_scripted_run(initial_rows=[])
        probe = _healthy(False)
        expect_exit = 0
    elif scenario == "survivor_error":
        row = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
        fake_run, _calls = _make_scripted_run(
            initial_rows=[row], mounts_by_id={"cid-1": []},
            compose_removes_ids=frozenset(),
            stop_fails_names=frozenset({PARTGRAPH_CONTAINER_NAME}),
        )
        probe = _healthy(False)
        expect_exit = 1
    else:
        fake_run, _calls = _make_scripted_run(initial_rows=[])
        probe = _healthy(True)
        expect_exit = 0

    with (
        patch("partgraph.cli.compose_command", return_value=["docker", "compose"]),
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=probe),
        patch("subprocess.run", side_effect=fake_run),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "down"])

    assert result.exit_code == expect_exit, result.output
    for line in result.output.splitlines():
        assert "/home" not in line, f"line leaks an operator home path: {line!r}"
        assert not line.strip().startswith("/"), f"line leaks a raw filesystem path: {line!r}"
