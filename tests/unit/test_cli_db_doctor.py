"""
Tests: PR-B1 (feat/db-lifecycle-doctor-and-docs) — `partgraph db doctor`
(AC-B3, AC-B3b, AC-B3c, AC-B3d).

PR-A (`fix/db-down-all-instances`, already landed on this branch) gave
`partgraph db down` the power to STOP every PartGraph lifecycle owner. PR-B1's
`db doctor` is the opposite half of that story: an operator needs a way to
SEE what PR-A's own selector policy (S1/S2/S3/UNKNOWN) sees, and whether the
quadlet unit will bring the database back at the next login, WITHOUT db
doctor ever being able to change any of it. This file is the acceptance test
for that command; `db doctor` does not exist yet, so every test below is
EXPECTED TO FAIL (RED) until it is implemented — mirrors the FIRST test file
in `tests/unit/test_cli_db_down.py`'s own history, except here the RED state
shows up as individual runtime assertion failures (typer reports "No such
command 'doctor'"), not a whole-file ModuleNotFoundError at collection: no
NEW top-level lifecycle symbol is required for THIS file to collect —
`PARTGRAPH_UNIT_NAME`/`PARTGRAPH_DATA_VOLUME`/`PARTGRAPH_CONTAINER_NAME`/
`PARTGRAPH_WATCHED_PORTS` already exist (landed by PR-A). Only the CLI
SUBCOMMAND is missing, so collection succeeds and every test fails at
run time instead.

DESIGN DECISIONS pinned by this file (none dictated verbatim by any AC;
recorded here, and repeated in the test-runner's final report, because a
future reader needs the reasoning):

1. `db doctor` resolves `engine_prefix = engine_command()` EXACTLY ONCE, at
   the top of the command, mirroring `db down`'s own cli.py pattern — but
   UNLIKE `db down`, a `ContainerEngineError` here is NOT fatal: it is caught,
   the engine-dependent sections (instances, volume) are reported as
   "could not be determined: no container engine found", and the
   engine-INDEPENDENT `unit_state()` check still runs normally. `db doctor`
   still exits 0. This is a deliberate DIVERGENCE from `db up`/`db down`
   (which genuinely need an engine to DO something, so failing loudly is
   correct there): `doctor` only ever reports, so a missing engine is itself
   a diagnostic finding, not a fatal error — and it is exactly the moment an
   operator most needs `doctor` to still say SOMETHING useful.
2. `find_partgraph_instances()` raising (`subprocess.TimeoutExpired`/`OSError`
   — deliberately NOT absorbed by the leaf itself, mirrors PR-A's own
   documented "an enumeration that never happened must never degrade to an
   empty tuple" contract) is caught by `db doctor` ITSELF (unlike `db down`,
   which turns the same exception into an exit-1 error) and reported as
   "instances could not be enumerated", still exiting 0.
3. Remediation text (the `WantedBy=` removal + `systemctl --user
   daemon-reload` instructions) is printed UNCONDITIONALLY, in every
   invocation, including the honest-empty-state one — AC-B3's own wording
   lists it as one of FOUR things `db doctor` "must report", stated
   unconditionally alongside the other three, not gated behind
   "only when autostart is currently enabled". A static runbook line is also
   simpler to reason about than an implicit "only sometimes visible" one.
4. AC-B3d's example is "a container name containing '[...]'" — but container
   NAMES are already grammar-validated by PR-A's `_accepted_identifier`
   (`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`), which rejects '[' outright, so a name
   can never actually carry a Rich-style-tag-shaped substring. This file
   instead exercises the markup=False discipline against `ActiveState`
   (raw, UNVALIDATED text straight from `systemctl show`, and one of the
   fields AC-B3 explicitly requires `db doctor` to print) as a concrete,
   RUNTIME demonstration — the correct, reachable analogue of the same
   risk. [Gate 3a SHOULD-FIX] That demonstration alone only proves ONE
   field is safe; LoadState/SubState/UnitFileState and any
   `Instance.image`/`status` text `doctor()` chooses to surface are equally
   unvalidated and would need chasing one at a time. A SECOND, STATIC test
   (`test_doctor_source_every_print_call_carries_markup_false`) instead
   parses cli.py's own source and asserts the ARCHITECTURAL rule directly:
   every `.print(...)` call inside `doctor()`'s body carries `markup=False`
   — removing the whole "did we forget one" class of bug rather than
   pinning it field by field.

HERMETICITY (mirrors tests/unit/test_cli_db_down.py exactly): every test
patches ONLY `subprocess.run`, `partgraph.cli.engine_command`,
`partgraph.cli.probe_health` (defensively — `db doctor` is not required to
call it, but patching it costs nothing and keeps every test hermetic even
if a future implementation adds a health line) and `shutil.which` — NEVER
`partgraph.util.lifecycle.*` directly. The scripted `subprocess.run` fake
below recognises ONLY four READ-ONLY call shapes (`ps --all`, `container
inspect`, `systemctl ... show`, `volume inspect`); any other argv —
including EVERY mutating verb this file's negative tests care about —
raises AssertionError immediately from inside the fixture itself. This is a
stronger, fail-FAST guarantee than a post-hoc `call_args_list` scan, and
this file does BOTH: the fixture's hard backstop, and (per AC-B3's own
literal wording, "assert over the entire call_args_list, not just the last
call") an explicit scan of `subprocess.run`'s own `call_args_list` too.
"""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from partgraph.cli import app
from partgraph.util.container import ContainerEngineError
from partgraph.util.lifecycle import (
    PARTGRAPH_CONTAINER_NAME,
    PARTGRAPH_DATA_VOLUME,
    PARTGRAPH_UNIT_NAME,
    PARTGRAPH_WATCHED_PORTS,
)

RUNNER = CliRunner()


def _invoke(args: list[str]):
    return RUNNER.invoke(app, args)


def _assert_clean(result, expected_code: int) -> None:
    """Assert *result* exited with *expected_code* and leaked no traceback."""
    assert result.exit_code == expected_code, (
        f"`db doctor` should exit {expected_code}, got {result.exit_code}.\n"
        f"Output:\n{result.output!r}"
    )
    assert "Traceback" not in result.output
    if result.exception is not None:
        assert isinstance(result.exception, SystemExit), (
            f"An unhandled exception leaked to the CLI surface instead of a clean "
            f"typer.Exit: {result.exception!r}"
        )


def _line_with(output: str, needle: str) -> str:
    """Return the first output line containing *needle* (case-sensitive), or
    fail the test with the full output for debugging.
    """
    for line in output.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no output line contains {needle!r}.\nFull output:\n{output!r}")


# ---------------------------------------------------------------------------
# Fixture builders (deliberately independent copies — per CONTRIBUTING.md's
# "Test fixtures stay local to their file" policy, copying small helpers
# across test files rather than sharing internals across
# independently-readable modules).
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


def _is_systemctl_show_call(argv: list[str]) -> bool:
    return bool(argv) and argv[0] == "systemctl" and "show" in argv


def _is_volume_inspect_call(argv: list[str]) -> bool:
    return "volume" in argv and "inspect" in argv


#: Any argv containing one of these EXACT tokens is a mutating call. Checked
#: by token EQUALITY (never substring), so e.g. "restart" never collides with
#: "start". Deliberately broader than PR-A's own `_is_engine_stop_call`/
#: `_is_systemctl_stop_call`: `db doctor` must invoke NONE of these, ever.
_FORBIDDEN_VERB_TOKENS = frozenset(
    {"stop", "rm", "down", "up", "prune", "create", "start", "enable", "disable",
     "daemon-reload", "kill", "restart", "reload"}
)


def _is_mutating_call(argv: list[str]) -> bool:
    return any(token in _FORBIDDEN_VERB_TOKENS for token in argv)


_UNIT_NOT_FOUND_LINES = [
    "LoadState=not-found", "ActiveState=inactive", "SubState=dead",
    "UnitFileState=", "WantedBy=",
]
_UNIT_ACTIVE_DEFAULT_TARGET_LINES = [
    "LoadState=loaded", "ActiveState=active", "SubState=running",
    "UnitFileState=generated", "WantedBy=default.target",
]
_UNIT_ACTIVE_OTHER_TARGET_LINES = [
    "LoadState=loaded", "ActiveState=active", "SubState=running",
    "UnitFileState=generated", "WantedBy=multi-user.target",
]
_UNIT_LOADED_EMPTY_WANTEDBY_LINES = [
    "LoadState=loaded", "ActiveState=inactive", "SubState=dead",
    "UnitFileState=generated", "WantedBy=",
]


def _make_doctor_scripted_run(  # noqa: PLR0913 — one keyword-only knob per scriptable outcome.
    *,
    ps_rows: list[dict],
    mounts_by_id: dict[str, list[dict]] | None = None,
    unit_lines: list[str] | None = None,
    volume_returncode: int = 1,
    volume_raises: Exception | None = None,
    ps_raises: Exception | None = None,
):
    """Return a STATELESS, READ-ONLY subprocess.run stand-in for `db doctor`.

    Recognises ONLY: `ps --all` (rows never change — `db doctor` performs no
    action that could alter host state, so unlike PR-A's `_make_scripted_run`
    this fixture needs no `live` mutable set at all), `container inspect`,
    `systemctl ... show`, and `volume inspect`. Any OTHER argv shape —
    including every mutating verb — raises AssertionError immediately: the
    hard backstop for AC-B3's "no mutating verb anywhere in the run".
    """
    mounts_by_id = mounts_by_id or {}
    unit_lines = unit_lines if unit_lines is not None else _UNIT_NOT_FOUND_LINES

    def _fake(argv, **kwargs):
        if _is_systemctl_show_call(argv):
            return _Proc(stdout="\n".join(unit_lines))
        if _is_volume_inspect_call(argv):
            if volume_raises is not None:
                raise volume_raises
            if volume_returncode == 0:
                return _Proc(returncode=0, stdout=f'[{{"Name": "{PARTGRAPH_DATA_VOLUME}"}}]')
            return _Proc(returncode=volume_returncode, stderr="Error: no such volume")
        if _is_inspect_call(argv):
            cid = argv[-1]
            return _Proc(stdout=json.dumps([{"Id": cid, "Mounts": mounts_by_id.get(cid, [])}]))
        if _is_ps_call(argv):
            if ps_raises is not None:
                raise ps_raises
            return _Proc(stdout=json.dumps(ps_rows))
        raise AssertionError(
            f"unscripted (and possibly MUTATING) subprocess.run call in a `db "
            f"doctor` test — doctor must be strictly read-only: {argv}"
        )

    return _fake


def _which_systemctl_present(name: str) -> str | None:
    return "/usr/bin/systemctl" if name == "systemctl" else None


def _which_nothing_present(name: str) -> str | None:
    return None


def _healthy(healthy: bool):
    return lambda: SimpleNamespace(healthy=healthy, message="probe")


# ---------------------------------------------------------------------------
# Sanity: the command exists at all
# ---------------------------------------------------------------------------


def test_doctor_command_is_registered_under_db_group() -> None:
    """Given `db doctor` is a new sub-command of the `db` group.
    When `partgraph db doctor --help` runs.
    Then it exits 0 and its own help text is shown (proving typer resolved a
    REAL `doctor` command rather than falling through to "No such command").
    """
    result = _invoke(["db", "doctor", "--help"])
    assert result.exit_code == 0, (
        f"`partgraph db doctor` is not registered as a command yet. Output:\n{result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-B3 — unit state: presence, LoadState, ActiveState, WantedBy=default.target
# ---------------------------------------------------------------------------


def test_doctor_reports_unit_present_load_state_and_active_state() -> None:
    """AC-B3: Given the quadlet unit is present, loaded and active.
    When `partgraph db doctor` runs.
    Then its output names the unit and shows the RAW LoadState ("loaded") and
    ActiveState ("active") values on the same line as the unit's name.
    """
    fake = _make_doctor_scripted_run(
        ps_rows=[], unit_lines=_UNIT_ACTIVE_DEFAULT_TARGET_LINES, volume_returncode=0,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    unit_line = _line_with(result.output, PARTGRAPH_UNIT_NAME)
    assert "loaded" in unit_line
    assert "active" in unit_line


def test_doctor_reports_unit_absent_plainly() -> None:
    """AC-B3: Given the quadlet unit does not exist on this host at all
    (LoadState=not-found).
    When `partgraph db doctor` runs.
    Then its output names the unit and states plainly that it is absent
    (one of "not present"/"not found"/"absent"), and exits 0.
    """
    fake = _make_doctor_scripted_run(ps_rows=[], unit_lines=_UNIT_NOT_FOUND_LINES, volume_returncode=1)
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    unit_line = _line_with(result.output, PARTGRAPH_UNIT_NAME)
    low = unit_line.lower()
    assert "not present" in low or "not found" in low or "absent" in low, (
        f"expected the unit line to plainly say it is absent: {unit_line!r}"
    )


@pytest.mark.parametrize(
    ("unit_lines", "case_id"),
    [
        pytest.param(_UNIT_ACTIVE_DEFAULT_TARGET_LINES, "wanted_by_default_target"),
    ],
)
def test_doctor_reports_wanted_by_default_true_without_claiming_unknown(unit_lines, case_id) -> None:
    """AC-B3: Given the unit's WantedBy includes default.target.
    When `partgraph db doctor` runs.
    Then the WantedBy-related line affirmatively signals autostart-at-login
    (one of "yes"/"enabled"/"true", as a whole word) and does NOT say
    "unknown".
    """
    fake = _make_doctor_scripted_run(ps_rows=[], unit_lines=unit_lines, volume_returncode=0)
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    wanted_by_line = _line_with(result.output, "WantedBy")
    low = wanted_by_line.lower()
    assert "unknown" not in low, f"must not hedge when confirmed True: {wanted_by_line!r}"
    import re as _re
    assert _re.search(r"\b(yes|enabled|true)\b", low), (
        f"expected an affirmative autostart signal on: {wanted_by_line!r}"
    )


def test_doctor_reports_wanted_by_default_false_without_claiming_enabled() -> None:
    """AC-B3: Given the unit IS present/active but its WantedBy names a
    DIFFERENT target (multi-user.target, never default.target) — a genuine,
    confirmed `wanted_by_default is False`.
    When `partgraph db doctor` runs.
    Then the WantedBy-related line does NOT say "unknown" and does NOT
    falsely claim the unit is enabled for autostart-at-login.
    """
    fake = _make_doctor_scripted_run(
        ps_rows=[], unit_lines=_UNIT_ACTIVE_OTHER_TARGET_LINES, volume_returncode=0,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    wanted_by_line = _line_with(result.output, "WantedBy")
    low = wanted_by_line.lower()
    assert "unknown" not in low
    import re as _re
    assert not _re.search(r"\b(yes|enabled|true)\b", low), (
        f"must not claim autostart is enabled when confirmed False: {wanted_by_line!r}"
    )


def test_doctor_reports_wanted_by_default_none_as_unknown_never_guessed() -> None:
    """[Contract: UnitState.wanted_by_default is None when undeterminable —
    NEVER guessed] Given the unit is present/loaded, but its own WantedBy=
    property line is EMPTY (no evidence either way — a real, if unusual,
    systemd answer, e.g. a static unit with no [Install] section active).
    When `partgraph db doctor` runs.
    Then the WantedBy-related line says "unknown" and asserts NEITHER an
    affirmative NOR a negative autostart claim.
    """
    fake = _make_doctor_scripted_run(
        ps_rows=[], unit_lines=_UNIT_LOADED_EMPTY_WANTEDBY_LINES, volume_returncode=1,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    wanted_by_line = _line_with(result.output, "WantedBy")
    low = wanted_by_line.lower()
    assert "unknown" in low, f"undeterminable WantedBy must render as 'unknown': {wanted_by_line!r}"
    import re as _re
    assert not _re.search(r"\b(yes|enabled|true|no|disabled|false)\b", low), (
        f"an undetermined WantedBy must never be rendered as a guessed yes/no: {wanted_by_line!r}"
    )


# ---------------------------------------------------------------------------
# AC-B3 — running instances (S1/S2) vs. report-only port holders (S3)
# ---------------------------------------------------------------------------


def test_doctor_reports_s1_and_s2_running_instances_as_partgraphs_own() -> None:
    """AC-B3: Given one S1 (exact-name) and one S2 (volume-mount) instance are
    running.
    When `partgraph db doctor` runs.
    Then BOTH names appear on a line that positively identifies them as
    PartGraph's own ("partgraph instance"), and that line does NOT ALSO carry
    the report-only wording used for S3.
    """
    row_s1 = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    row_s2 = _ps_row("cid-2", "systemd-partgraph-dgraph-duplicate", "dgraph/standalone:v25.3.4")
    fake = _make_doctor_scripted_run(
        ps_rows=[row_s1, row_s2],
        mounts_by_id={"cid-1": [], "cid-2": _mounts(PARTGRAPH_DATA_VOLUME)},
        unit_lines=_UNIT_NOT_FOUND_LINES,
        volume_returncode=0,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    for name in (PARTGRAPH_CONTAINER_NAME, "systemd-partgraph-dgraph-duplicate"):
        line = _line_with(result.output, name)
        assert "partgraph instance" in line.lower(), (
            f"expected {name!r}'s line to identify it as PartGraph's own instance: {line!r}"
        )
        assert "report-only" not in line.lower(), (
            f"an S1/S2 instance must never be printed under the S3 report-only wording: {line!r}"
        )


def test_doctor_reports_s3_report_only_port_holder_distinctly() -> None:
    """AC-B3 / [mirrors PR-A's A16 dry-run design]: Given a container holding
    one of PartGraph's watched ports but matching neither S1 nor S2.
    When `partgraph db doctor` runs.
    Then its name still appears (visibility matters), on a line carrying the
    report-only wording — distinct from the S1/S2 "partgraph instance" line.
    """
    watched_port = PARTGRAPH_WATCHED_PORTS[0]
    row_s3 = _ps_row("cid-3", "some-other-service", "nginx:1.27.3", host_ports=(watched_port,))
    fake = _make_doctor_scripted_run(
        ps_rows=[row_s3], mounts_by_id={"cid-3": []}, unit_lines=_UNIT_NOT_FOUND_LINES,
        volume_returncode=1,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    line = _line_with(result.output, "some-other-service")
    low = line.lower()
    assert "report-only" in low or "not partgraph" in low, (
        f"expected the S3 port holder to be visibly marked report-only: {line!r}"
    )
    assert "partgraph instance" not in low, (
        f"an S3 port holder must never be printed as PartGraph's own instance: {line!r}"
    )


# ---------------------------------------------------------------------------
# AC-B3 — the partgraph_dgraph_data volume
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("volume_returncode", "volume_raises", "expected_word", "forbidden_words", "case_id"),
    [
        pytest.param(0, None, "present", ("absent", "unknown"), "exists"),
        pytest.param(1, None, "absent", ("present", "unknown"), "does_not_exist"),
    ],
)
def test_doctor_reports_volume_existence_honestly(
    volume_returncode, volume_raises, expected_word, forbidden_words, case_id
) -> None:
    """AC-B3: Given the engine's `volume inspect` positively confirms the
    named data volume exists (exit 0) or positively confirms it does not
    (non-zero exit).
    When `partgraph db doctor` runs.
    Then the volume line contains the expected verdict word and NONE of the
    other two.
    """
    fake = _make_doctor_scripted_run(
        ps_rows=[], unit_lines=_UNIT_NOT_FOUND_LINES,
        volume_returncode=volume_returncode, volume_raises=volume_raises,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    volume_line = _line_with(result.output, PARTGRAPH_DATA_VOLUME)
    low = volume_line.lower()
    assert expected_word in low, f"expected {expected_word!r} on: {volume_line!r}"
    for forbidden in forbidden_words:
        assert forbidden not in low, f"unexpected {forbidden!r} on: {volume_line!r}"


def test_doctor_reports_volume_existence_unknown_on_inspect_timeout() -> None:
    """[Contract: volume_exists() tri-state, never guessed] Given the engine's
    `volume inspect` call itself times out.
    When `partgraph db doctor` runs.
    Then the volume line says "unknown" (never "present", never "absent") and
    the command still exits 0 — a timeout is not fatal to the whole
    diagnostic.
    """
    fake = _make_doctor_scripted_run(
        ps_rows=[], unit_lines=_UNIT_NOT_FOUND_LINES,
        volume_returncode=1,
        volume_raises=subprocess.TimeoutExpired(cmd=["docker", "volume", "inspect"], timeout=10),
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    volume_line = _line_with(result.output, PARTGRAPH_DATA_VOLUME)
    low = volume_line.lower()
    assert "unknown" in low
    assert "present" not in low
    assert "absent" not in low


# ---------------------------------------------------------------------------
# AC-B3 — remediation text (unconditional; see module docstring decision #3)
# ---------------------------------------------------------------------------


def test_doctor_always_prints_the_wanted_by_removal_and_daemon_reload_remediation() -> None:
    """AC-B3: Given `db doctor` must report "the exact remediation text for
    removing autostart" as one of its four unconditional bullets.
    When `partgraph db doctor` runs (any scenario — this one uses the
    honest-empty-state fixture deliberately, to prove the remediation text is
    NOT gated behind "only if autostart is currently on").
    Then the output mentions removing `WantedBy=`, running
    `systemctl --user daemon-reload`, and does so as plain print/display
    text — never as an executed subprocess call (that invariant is pinned
    separately and exhaustively in
    tests/unit/test_repo_never_executes_lifecycle_mutations.py, AC-B2).
    """
    fake = _make_doctor_scripted_run(ps_rows=[], unit_lines=_UNIT_NOT_FOUND_LINES, volume_returncode=1)
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake) as mock_run,
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    assert "WantedBy=" in result.output
    assert "daemon-reload" in result.output.lower()
    assert "systemctl --user" in result.output
    # The remediation text is DISPLAY text: it must never have been executed.
    for call in mock_run.call_args_list:
        argv = list(call.args[0])
        assert "daemon-reload" not in argv


# ---------------------------------------------------------------------------
# AC-B3 — no mutating verb anywhere in the run
# ---------------------------------------------------------------------------


def test_doctor_never_invokes_any_mutating_verb_across_the_entire_call_args_list() -> None:
    """AC-B3: Given a rich scenario — an active, autostart-enabled unit, one
    S1 running instance, one S3 report-only port holder, and an existing
    volume (maximum surface for something to go wrong).
    When `partgraph db doctor` runs.
    Then `subprocess.run`'s OWN `call_args_list` (not just its last call)
    contains no mutating verb anywhere: no stop/rm/down/up/prune/create/
    start/enable/disable/daemon-reload/kill/restart/reload token in any argv.
    """
    row_s1 = _ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")
    watched_port = PARTGRAPH_WATCHED_PORTS[1]
    row_s3 = _ps_row("cid-3", "some-other-service", "nginx:1.27.3", host_ports=(watched_port,))
    fake = _make_doctor_scripted_run(
        ps_rows=[row_s1, row_s3],
        mounts_by_id={"cid-1": [], "cid-3": []},
        unit_lines=_UNIT_ACTIVE_DEFAULT_TARGET_LINES,
        volume_returncode=0,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake) as mock_run,
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    assert mock_run.call_args_list, "expected at least one subprocess call"
    for call in mock_run.call_args_list:
        argv = list(call.args[0]) if call.args else list(call.kwargs.get("args", []))
        assert not _is_mutating_call(argv), f"db doctor invoked a mutating verb: {argv}"
        assert not (argv and argv[0] == "systemctl" and "show" not in argv), (
            f"db doctor must never call systemctl for anything but 'show': {argv}"
        )


# ---------------------------------------------------------------------------
# AC-B3b — the cve-graph fixture (reused verbatim from PR-A)
# ---------------------------------------------------------------------------


def test_doctor_never_touches_or_names_the_cve_graph_fixture_as_partgraphs() -> None:
    """AC-B3b (HIGHEST PRIORITY NEGATIVE): Given the REAL observed host state
    from PR-A's own ADR-0021 incident (names/images only) — zero PartGraph
    containers, and exactly: min-web (nginx:1.27.3), cve-ratel
    (dgraph/ratel:latest), cve-zero (dgraph/dgraph:v24.0.0), cve-alpha
    (dgraph/dgraph:v24.0.0), cve-loader (localhost/cve-loader:latest), none
    holding a PartGraph-watched port.
    When `partgraph db doctor` runs.
    Then NONE of those five names appears in ANY subprocess argv, NONE
    appears anywhere in `db doctor`'s own output at all (this fixture has no
    genuine port collision, so unlike the S3 test above there is no
    legitimate report-only reason for any of them to surface), and the
    command exits 0.
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
    fake = _make_doctor_scripted_run(
        ps_rows=rows, mounts_by_id=mounts_by_id, unit_lines=_UNIT_NOT_FOUND_LINES,
        volume_returncode=1,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake) as mock_run,
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    forbidden = {
        "min-web", "cve-ratel", "cve-zero", "cve-alpha", "cve-loader",
        "cve-graph_dgraph_zero", "cve-graph_dgraph_alpha",
    }
    for call in mock_run.call_args_list:
        argv = list(call.args[0]) if call.args else list(call.kwargs.get("args", []))
        argv_text = " ".join(argv)
        for token in forbidden:
            assert token not in argv_text, f"forbidden token {token!r} leaked into argv: {argv}"
    for token in forbidden:
        assert token not in result.output, (
            f"cve-graph identifier {token!r} must never appear in db doctor's output "
            f"(no genuine port collision in this fixture): {result.output!r}"
        )


def test_doctor_s3_report_only_section_may_legitimately_name_a_foreign_container() -> None:
    """AC-B3b [mirrors how PR-A resolved the same A16/A17 tension: a report-
    only S3 line legitimately names a foreign container that holds one of
    PartGraph's ports, while the cve-graph fixture above — which holds NO
    PartGraph port — must never be named at all]. Given a GENERIC,
    non-cve-named container that genuinely holds one of PartGraph's watched
    ports (a real collision, kept deliberately distinct from the cve-graph
    fixture so the two concerns are not conflated).
    When `partgraph db doctor` runs.
    Then its name IS visible, specifically inside the report-only section —
    this is the positive half of AC-B3b: `db doctor` is not required to
    scrub every foreign name from existence, only to never misclassify one
    as PartGraph's own.
    """
    watched_port = PARTGRAPH_WATCHED_PORTS[2]
    row = _ps_row("cid-collision", "unrelated-web-server", "nginx:1.27.3", host_ports=(watched_port,))
    fake = _make_doctor_scripted_run(
        ps_rows=[row], mounts_by_id={"cid-collision": []}, unit_lines=_UNIT_NOT_FOUND_LINES,
        volume_returncode=1,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    line = _line_with(result.output, "unrelated-web-server")
    low = line.lower()
    assert "report-only" in low or "not partgraph" in low


# ---------------------------------------------------------------------------
# AC-B3c — honest empty state
# ---------------------------------------------------------------------------


def test_doctor_honest_empty_state_says_so_plainly_and_exits_zero() -> None:
    """AC-B3c: Given NO unit, NO instances and NO volume.
    When `partgraph db doctor` runs.
    Then it says so plainly (a line containing "no partgraph instance",
    case-insensitive) rather than emitting nothing or an error-shaped
    message, and exits 0 — a diagnostic that reports nothing when it found
    nothing is the correct outcome, not an error.
    """
    fake = _make_doctor_scripted_run(ps_rows=[], unit_lines=_UNIT_NOT_FOUND_LINES, volume_returncode=1)
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    assert "no partgraph instance" in result.output.lower()
    volume_line = _line_with(result.output, PARTGRAPH_DATA_VOLUME)
    assert "absent" in volume_line.lower()
    assert "unknown" not in volume_line.lower()


# ---------------------------------------------------------------------------
# AC-B3d — output hygiene: single-line, path-free, markup=False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["empty", "running_and_autostart_on", "cve_graph_present"])
def test_doctor_output_lines_are_path_free(scenario: str) -> None:
    """AC-B3d: Given three representative `db doctor` outcomes (nothing
    found, a running autostart-enabled instance, and the real cve-graph
    fixture present alongside it).
    When `partgraph db doctor` runs.
    Then no printed line leaks an operator home path ("/home") or a raw
    leading filesystem path (mirrors tests/unit/test_cli_db_down.py's own
    `test_down_messages_are_single_line_path_free` convention exactly).
    """
    if scenario == "empty":
        rows: list[dict] = []
        mounts: dict[str, list[dict]] = {}
        unit_lines = _UNIT_NOT_FOUND_LINES
        volume_returncode = 1
    elif scenario == "running_and_autostart_on":
        rows = [_ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")]
        mounts = {"cid-1": []}
        unit_lines = _UNIT_ACTIVE_DEFAULT_TARGET_LINES
        volume_returncode = 0
    else:
        rows = [_ps_row("cid-alpha", "cve-alpha", "dgraph/dgraph:v24.0.0", host_ports=(18081,))]
        mounts = {"cid-alpha": _mounts("cve-graph_dgraph_alpha")}
        unit_lines = _UNIT_NOT_FOUND_LINES
        volume_returncode = 1

    fake = _make_doctor_scripted_run(
        ps_rows=rows, mounts_by_id=mounts, unit_lines=unit_lines, volume_returncode=volume_returncode,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    for line in result.output.splitlines():
        assert "/home" not in line, f"line leaks an operator home path: {line!r}"
        assert not line.strip().startswith("/"), f"line leaks a raw filesystem path: {line!r}"


def test_doctor_prints_engine_derived_active_state_verbatim_markup_false() -> None:
    """AC-B3d: Given `systemctl show`'s ActiveState= value contains a
    Rich-style-tag-shaped substring ("[bold red]") — raw, unvalidated
    engine-derived text (see module docstring design decision #4 for why
    ActiveState, not a container name, is the correct field to exercise
    here: names are already grammar-validated by PR-A and cannot carry '[').
    When `partgraph db doctor` runs.
    Then the literal text is printed VERBATIM in the output — proving
    markup=False; Rich markup would otherwise try to interpret it as a style
    tag and it would never appear at all.
    """
    bracketed_state = "active[bold red]injected"
    unit_lines = [
        "LoadState=loaded", f"ActiveState={bracketed_state}", "SubState=running",
        "UnitFileState=generated", "WantedBy=default.target",
    ]
    fake = _make_doctor_scripted_run(ps_rows=[], unit_lines=unit_lines, volume_returncode=1)
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    assert bracketed_state in result.output, (
        f"engine-derived ActiveState text must survive verbatim (markup=False). "
        f"Output:\n{result.output!r}"
    )


def test_doctor_source_every_print_call_carries_markup_false() -> None:
    """[Gate 3a SHOULD-FIX] Given per-field pinning (ActiveState only, the
    test above) leaves every OTHER field `doctor()` also prints —
    LoadState, SubState, UnitFileState, and any `Instance.image`/`status`
    text it chooses to surface — EQUALLY unvalidated raw text and EQUALLY
    exposed to the same Rich-markup-injection risk; chasing fields one at a
    time invites "did we forget one" the next time a field is added.
    When cli.py's own source text is parsed and the `doctor()` function's
    body is scanned for every `.print(...)` call (on ANY console object —
    `_console`/`_err_console`/otherwise).
    Then EVERY SUCH CALL carries `markup=False` as an explicit keyword
    argument — the architectural rule, not a per-field one. Skips cleanly
    (not a failure) while `doctor()` does not exist in cli.py yet.
    """
    cli_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src" / "partgraph" / "cli.py"
    )
    source = cli_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(cli_path))

    doctor_func = next(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "doctor"
        ),
        None,
    )
    if doctor_func is None:
        pytest.skip("cli.py's `doctor()` command does not exist yet (expected pre-PR-B1 implementation).")

    violations: list[str] = []
    for call in ast.walk(doctor_func):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "print"):
            continue
        has_markup_false = any(
            kw.arg == "markup" and isinstance(kw.value, ast.Constant) and kw.value.value is False
            for kw in call.keywords
        )
        if not has_markup_false:
            violations.append(
                f"cli.py:{call.lineno}: a .print(...) call inside doctor() is "
                "missing markup=False"
            )

    assert not violations, (
        "every print call inside doctor() must carry markup=False "
        "(architectural rule, AC-B3d):\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# AC-B3d — exit code is always 0 once the diagnostic ran
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit_lines", "ps_rows", "volume_returncode", "case_id"),
    [
        pytest.param(_UNIT_NOT_FOUND_LINES, [], 1, "empty"),
        pytest.param(_UNIT_ACTIVE_DEFAULT_TARGET_LINES, [], 0, "unit_active_autostart_on"),
        pytest.param(
            _UNIT_NOT_FOUND_LINES,
            [_ps_row("cid-1", PARTGRAPH_CONTAINER_NAME, "dgraph/standalone:v25.3.4")],
            1,
            "s1_running_no_unit",
        ),
    ],
)
def test_doctor_exit_code_is_always_zero_regardless_of_findings(
    unit_lines, ps_rows, volume_returncode, case_id
) -> None:
    """AC-B3d: Given several representative findings (nothing at all; an
    active, autostart-enabled unit with no running instance; a running S1
    instance with no unit installed).
    When `partgraph db doctor` runs in each case.
    Then it exits 0 in EVERY case — `db doctor` reports, it never judges.
    """
    fake = _make_doctor_scripted_run(
        ps_rows=ps_rows,
        mounts_by_id={row["Id"]: [] for row in ps_rows},
        unit_lines=unit_lines,
        volume_returncode=volume_returncode,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    assert result.exit_code == 0, (
        f"db doctor must always exit 0 once it ran, case={case_id!r}. Output:\n{result.output!r}"
    )


# ---------------------------------------------------------------------------
# Resilience: a missing engine, and an enumeration that cannot run at all
# ---------------------------------------------------------------------------


def test_doctor_no_container_engine_found_still_exits_zero_and_reports() -> None:
    """[Design decision #1] Given NEITHER docker nor podman is on PATH
    (engine_command() raises ContainerEngineError).
    When `partgraph db doctor` runs.
    Then it does NOT attempt any engine-dependent subprocess call at all
    (the scripted fixture asserts this: it recognises ONLY `systemctl ...
    show`, and raises on anything else) — the instances/volume sections are
    reported as undeterminable, the unit section still works normally
    (systemd is independent of the container engine), and the command exits
    0 with no traceback.
    """
    def _fake(argv, **kwargs):
        if _is_systemctl_show_call(argv):
            return _Proc(stdout="\n".join(_UNIT_NOT_FOUND_LINES))
        raise AssertionError(
            f"db doctor must not attempt an engine-dependent call once engine "
            f"detection has already failed: {argv}"
        )

    with (
        patch("partgraph.cli.engine_command", side_effect=ContainerEngineError("no engine found")),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=_fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    assert "engine" in result.output.lower()


def test_doctor_instance_enumeration_failure_is_absorbed_reported_and_exits_zero() -> None:
    """[Design decision #2] Given the container engine IS found, but the `ps
    --all` enumeration itself times out (a wedged engine) — a failure PR-A's
    own `find_partgraph_instances()` deliberately does NOT absorb internally
    (its docstring: "an enumeration that never happened must never be
    degraded to an empty tuple").
    When `partgraph db doctor` runs.
    Then `db doctor` ITSELF catches the exception (unlike `db down`, which
    turns the same failure into exit 1), reports that instances could not be
    enumerated, and still exits 0 with no traceback.
    """
    fake = _make_doctor_scripted_run(
        ps_rows=[], unit_lines=_UNIT_NOT_FOUND_LINES, volume_returncode=1,
        ps_raises=subprocess.TimeoutExpired(cmd=["docker", "ps", "--all"], timeout=15),
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake),
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    low = result.output.lower()
    assert "instance" in low
    assert "could not" in low or "unknown" in low


# ---------------------------------------------------------------------------
# [Gate 3a SHOULD-FIX — ROOT CAUSE IS PR-A CODE, NOT PR-B1'S REMIT]
# ---------------------------------------------------------------------------


def test_doctor_subprocess_call_count_is_bounded_over_a_large_synthetic_row_set() -> None:
    """[Gate 3a SHOULD-FIX] Given a large host (a busy CI runner, a shared
    dev box) reports thousands of containers via `ps --all`, NONE of them
    PartGraph's or even dgraph-family. PR-A's own
    `find_partgraph_instances()` (src/partgraph/util/lifecycle.py,
    `_instance_from_row`) calls `_mounts_data_volume` — one `container
    inspect` subprocess call, up to INSPECT_TIMEOUT_S (10s) each —
    UNCONDITIONALLY for every row carrying a usable name and id, BEFORE
    `_classify` ever decides whether that row is PartGraph's at all. There
    is no cap on row count. `db doctor` (AC-B3d) advertises itself as
    always safe, quick to run.
    When `partgraph db doctor` runs against 2000 synthetic, entirely
    unrelated containers.
    Then the TOTAL number of subprocess calls made stays well below one per
    row — NOT O(row count).

    HONESTLY FLAGGED, NOT WORKED AROUND: this test is expected to stay RED
    even once `doctor()` itself is fully implemented, UNLESS the underlying
    PR-A enumeration is ALSO bounded — a change to
    `src/partgraph/util/lifecycle.py`'s `find_partgraph_instances()`
    (shared by `db down` too, not `doctor()`-specific), which is outside
    this PR's `no src/` constraint and PR-A's remit, not PR-B1's. Recorded
    here as a pinned, failing acceptance test rather than silently left
    unencoded, exactly as the Gate 3a review asked: "if bounding it needs a
    change there, say so" — said here, in the one place a future
    implementer will find it attached to the behaviour it constrains.
    """
    rows = [
        _ps_row(f"cid-{i}", f"unrelated-service-{i}", "nginx:1.27.3")
        for i in range(2000)
    ]
    fake = _make_doctor_scripted_run(
        ps_rows=rows,
        mounts_by_id={row["Id"]: [] for row in rows},
        unit_lines=_UNIT_NOT_FOUND_LINES,
        volume_returncode=1,
    )
    with (
        patch("partgraph.cli.engine_command", return_value=["docker"]),
        patch("partgraph.cli.probe_health", side_effect=_healthy(False)),
        patch("subprocess.run", side_effect=fake) as mock_run,
        patch("shutil.which", side_effect=_which_systemctl_present),
    ):
        result = _invoke(["db", "doctor"])

    _assert_clean(result, 0)
    call_count = len(mock_run.call_args_list)
    assert call_count < len(rows) // 2, (
        f"db doctor issued {call_count} subprocess calls for {len(rows)} "
        "entirely unrelated containers — that scales with row count, "
        "inherited from PR-A's find_partgraph_instances() calling "
        "`container inspect` once per row before classification. A "
        "diagnostic advertised as always-safe to run must not scale this "
        "way. Bounding this is PR-A's remit "
        "(src/partgraph/util/lifecycle.py), not PR-B1's."
    )
