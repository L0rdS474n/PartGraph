"""
Tests: PR-C (feat/db-idle-autostop) — the shipped, opt-in host-side timer
(C-11): `systemd/partgraph-db-idle-stop.{service,timer}`.

WHY THIS PAIR OF FILES EXISTS AT ALL (ADR-facing — read this before assuming
an in-process timer was overlooked). `partgraph` is a one-shot CLI: every
invocation runs its command and exits, so nothing inside the process can
ever wake up later to check idleness — there is no daemon, no thread, no
event loop that survives past the command's own return. The ONLY mechanism
that can act after `partgraph` has already exited is something OUTSIDE the
process, on the host. This unit pair is that mechanism: it periodically
invokes `partgraph db idle-stop` as its own separate, one-shot command —
exactly the pattern ADR-0014 already established for
`partgraph-refresh-all.{service,timer}` (see `docs/scheduling.md`). The
repository SHIPS the unit files; it never enables them (C-11's "opt-in"
requirement, verified below) — installation is a documented, operator-run
procedure, mirroring `docs/scheduling.md`'s own install steps for the
refresh timer.

RED-STATE CONVENTION (mirrors `tests/unit/test_db_lifecycle_docs.py`'s own,
already-established convention in this exact repo for "a file this PR's
implementer has not written yet"): while the unit files are absent,
`test_service_file_exists_and_is_nonempty` / `test_timer_file_exists_and_is_
nonempty` are HARD, non-skipped failures — every other test in this file
depends on the files existing, and uses a module-scoped fixture that SKIPS
individually, with a clear reason, while they remain absent; it goes green
the moment the files land with the right content, and FAILS (not skips) if
they land with the WRONG content.

PROPERTY OVER PROXY: the strongest check here is
`test_systemd_analyze_verify_accepts_both_units_with_empty_output` — real
`systemd-analyze verify` against the real files, exiting 0 with empty
output — mirroring EXACTLY the verification method `docs/scheduling.md`'s
own "How this was verified" section already documents having used for the
refresh units (confirmed directly, in this sandbox, against those real,
existing, already-shipped files: `systemd-analyze verify
systemd/partgraph-refresh-all.service systemd/partgraph-refresh-all.timer`
exits 0 with empty stdout/stderr). Every other check in this file is a
STATIC content assertion (a necessary complement, not a substitute — a unit
can be syntactically valid and still declare `User=root`) and is labelled
as such.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

SERVICE_REL = "systemd/partgraph-db-idle-stop.service"
TIMER_REL = "systemd/partgraph-db-idle-stop.timer"
REFRESH_SERVICE_REL = "systemd/partgraph-refresh-all.service"
REFRESH_TIMER_REL = "systemd/partgraph-refresh-all.timer"

_UNIT_NAME = "partgraph-db-idle-stop"


# ---------------------------------------------------------------------------
# R1-style existence — HARD, non-skipped.
# ---------------------------------------------------------------------------


def test_service_file_exists_and_is_nonempty(repo_root: pathlib.Path) -> None:
    """C-11: Given `systemd/partgraph-db-idle-stop.service` is PR-C's shipped,
    opt-in unit.
    When we look for it at the repo root.
    Then it exists and is non-empty. HARD failure (not a skip) — every other
    test in this file depends on it."""
    path = repo_root / SERVICE_REL
    assert path.exists(), (
        f"{SERVICE_REL} does not exist yet. PR-C (C-11) requires it: the "
        "opt-in, per-user systemd --user unit that periodically invokes "
        "`partgraph db idle-stop`."
    )
    assert path.is_file()
    assert path.stat().st_size > 0


def test_timer_file_exists_and_is_nonempty(repo_root: pathlib.Path) -> None:
    """C-11: mirrors the service check, for the timer."""
    path = repo_root / TIMER_REL
    assert path.exists(), f"{TIMER_REL} does not exist yet. PR-C (C-11) requires it."
    assert path.is_file()
    assert path.stat().st_size > 0


@pytest.fixture(scope="module")
def service_text(repo_root: pathlib.Path) -> str:
    path = repo_root / SERVICE_REL
    if not path.exists():
        pytest.skip(f"{SERVICE_REL} does not exist yet (expected pre-PR-C).")
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def timer_text(repo_root: pathlib.Path) -> str:
    path = repo_root / TIMER_REL
    if not path.exists():
        pytest.skip(f"{TIMER_REL} does not exist yet (expected pre-PR-C).")
    return path.read_text(encoding="utf-8")


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _section_body(lines: list[str], section: str) -> list[str]:
    """Return the lines belonging to [*section*] up to the next '[' header."""
    body: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"[{section}]":
            in_section = True
            continue
        if in_section and stripped.startswith("[") and stripped.endswith("]"):
            break
        if in_section:
            body.append(line)
    return body


# ---------------------------------------------------------------------------
# C-11 — per-user, never root: no User= directive at all.
# ---------------------------------------------------------------------------


def test_service_declares_no_user_directive(service_text: str) -> None:
    """C-11: Given the unit is meant to be installed with `systemctl --user`
    (mirrors `partgraph-refresh-all.service`'s own header: "installed with
    systemctl --user; runs as your own login user — never root — so no
    User= directive is set").
    When the `[Service]` section is scanned.
    Then it contains NO `User=` line at all — a `systemd --user` unit that
    also declared `User=` would be a contradiction the unit file itself
    should never contain."""
    lines = _lines(service_text)
    service_body = _section_body(lines, "Service")
    user_lines = [ln for ln in service_body if ln.strip().startswith("User=")]
    assert not user_lines, (
        f"{SERVICE_REL} must never declare User= (per-user unit, never root): {user_lines!r}"
    )


def test_service_never_mentions_root_as_a_literal_user(service_text: str) -> None:
    """Defence in depth: the string 'root' must never appear as a
    configured value anywhere in the unit (a User=root drop-in-style typo,
    or a Group=root, etc.)."""
    assert not re.search(r"=\s*root\b", service_text), (
        f"{SERVICE_REL} must never configure 'root' as a value anywhere."
    )


# ---------------------------------------------------------------------------
# C-11 — %h specifiers only, never an operator path.
#
# [Gate 3b BLOCKING fix] An earlier draft of this file kept a LOCAL copy of
# `tests/unit/test_repo_skeleton.py`'s own `_HOME_PATH_PATTERN` here, to
# assert the NEGATIVE half directly ("no real operator home path appears").
# `tests/unit/test_db_lifecycle_docs.py` already recorded, explicitly, why
# that specific duplication is actively HARMFUL rather than merely
# redundant: that regex's own source text contains the literal substring
# `/home/` inside its negative lookahead, so a second, independent copy of
# it is itself something the repo-wide scanner
# (`test_repo_skeleton.py::test_no_operator_home_paths_in_tracked_files`)
# reads as a real leaked home path THE MOMENT this file is tracked — the
# copy self-matches and breaks an unrelated, previously-green gate. Gate 3a
# independently confirmed the mechanism: a naive substring/character-class
# scan does not see the `(` immediately after `/home/` in the regex's own
# source as anything special, so it reads exactly like a real path segment.
#
# The fix mirrors `test_db_lifecycle_docs.py`'s own, already-corrected
# resolution exactly: DROP the local copy and rely on the always-on,
# repo-wide scanner for the NEGATIVE property (it covers every tracked file
# uniformly, including this one, the moment it lands) — this file keeps
# only the POSITIVE half the repo-wide scanner does not and cannot cover:
# that the `%h` placeholder is actually PRESENT, i.e. the unit could not
# simply omit any home-directory reference at all and still pass.
# ---------------------------------------------------------------------------


def test_service_references_percent_h(service_text: str) -> None:
    """C-11 [positive half only — see the note above]: Given the unit is
    meant to reference its operator's home directory only via systemd's
    `%h` specifier (mirrors `partgraph-refresh-all.service`'s own
    `ExecStart=%h/.local/bin/...`).
    When the unit's full text is scanned.
    Then the literal `%h` specifier is actually present somewhere — a unit
    that omitted any home-directory reference at all (e.g. one that never
    reads an operator env file) would still need to prove it, not merely
    be assumed to."""
    assert "%h" in service_text, f"{SERVICE_REL} never references the %h specifier."


def test_service_execstart_invokes_idle_stop(service_text: str) -> None:
    """C-11: Given the service's whole purpose is to run `db idle-stop`
    periodically.
    When the `[Service]` section is scanned.
    Then an `ExecStart=` line exists and names the `idle-stop` subcommand."""
    lines = _lines(service_text)
    service_body = _section_body(lines, "Service")
    exec_lines = [ln for ln in service_body if ln.strip().startswith("ExecStart=")]
    assert exec_lines, f"{SERVICE_REL} has no ExecStart= line in [Service]."
    assert any("idle-stop" in ln for ln in exec_lines), (
        f"{SERVICE_REL}'s ExecStart= must invoke the 'idle-stop' subcommand: {exec_lines!r}"
    )


# ---------------------------------------------------------------------------
# C-11 — hardened: NoNewPrivileges=true, PrivateTmp=true (explicitly named
# by the AC). Proxy check — the stronger property check is the
# systemd-analyze section below.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "directive", ["NoNewPrivileges=true", "PrivateTmp=true"],
)
def test_service_declares_the_named_hardening_directives(
    service_text: str, directive: str
) -> None:
    """C-11: Given the AC explicitly names `NoNewPrivileges=true` and
    `PrivateTmp=true` (mirrors `partgraph-refresh-all.service`'s own
    hardening block).
    When the `[Service]` section is scanned.
    Then the exact directive line is present."""
    lines = _lines(service_text)
    service_body = [ln.strip() for ln in _section_body(lines, "Service")]
    assert directive in service_body, (
        f"{SERVICE_REL} must declare {directive!r} in [Service] (line-exact match)."
    )


# ---------------------------------------------------------------------------
# C-11 — sane cadence (a JUDGEMENT CALL, disclosed as such — mirrors
# AUTOSTART_READY_TIMEOUT_S's own "finite alone does not catch a typo'd
# extra zero" reasoning, applied to a systemd calendar/duration spec instead
# of a Python float).
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^(\d+)\s*(s|sec|secs|second|seconds|"
    r"m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours)?$"
)
_UNIT_SECONDS = {
    None: 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}

#: A JUDGEMENT CALL, not a measured requirement (mirrors STOP_GRACE_SECONDS'
#: and AUTOSTART_READY_TIMEOUT_S's own documented precedent): sized so a
#: single-digit-minutes cadence (checking far more often than any sane idle
#: timeout would need) and a multi-day cadence (defeating the point of an
#: idle-stop check entirely) both fail this bound, while the sensible middle
#: — every few minutes to roughly an hour — passes.
_SANE_CADENCE_FLOOR_S = 60.0
_SANE_CADENCE_CEILING_S = 3600.0


def test_timer_declares_a_real_periodic_trigger(timer_text: str) -> None:
    """C-11: Given the timer must actually fire periodically (unlike the
    refresh timer's single weekly `OnCalendar=`, idle-stop's whole point is
    a REPEATING check).
    When the `[Timer]` section is scanned.
    Then it declares at least one of `OnUnitActiveSec=`, `OnBootSec=` or
    `OnCalendar=` — a genuine periodic trigger, not merely a `[Timer]`
    section with nothing in it."""
    lines = _lines(timer_text)
    timer_body = [ln.strip() for ln in _section_body(lines, "Timer")]
    trigger_lines = [
        ln for ln in timer_body
        if ln.startswith(("OnUnitActiveSec=", "OnBootSec=", "OnCalendar="))
    ]
    assert trigger_lines, f"{TIMER_REL} declares no periodic trigger in [Timer]: {timer_body!r}"


def test_timer_cadence_is_within_a_sane_bound_when_expressed_as_a_duration(
    timer_text: str,
) -> None:
    """[JUDGEMENT CALL, disclosed] Given the timer expresses its cadence as
    an `OnUnitActiveSec=`/`OnBootSec=` DURATION (not an `OnCalendar=` cron
    expression, which this test does not attempt to parse).
    When that duration is parsed.
    Then it falls within `[_SANE_CADENCE_FLOOR_S, _SANE_CADENCE_CEILING_S]`
    — catches a typo'd cadence (a stray extra/missing zero) without
    demanding an exact, unmeasured number."""
    lines = _lines(timer_text)
    timer_body = [ln.strip() for ln in _section_body(lines, "Timer")]
    duration_lines = [
        ln for ln in timer_body if ln.startswith(("OnUnitActiveSec=", "OnBootSec="))
    ]
    if not duration_lines:
        pytest.skip("timer uses OnCalendar=, not a duration — nothing to bound here.")

    for ln in duration_lines:
        _, _, value = ln.partition("=")
        match = _DURATION_RE.match(value.strip())
        assert match, f"{TIMER_REL}: unparseable duration in {ln!r}"
        amount, unit = match.groups()
        seconds = int(amount) * _UNIT_SECONDS[unit]
        assert _SANE_CADENCE_FLOOR_S <= seconds <= _SANE_CADENCE_CEILING_S, (
            f"{TIMER_REL}: cadence {ln!r} ({seconds}s) is outside the sane "
            f"bound [{_SANE_CADENCE_FLOOR_S}, {_SANE_CADENCE_CEILING_S}]s — "
            "if genuinely intended, widen the bound deliberately and "
            "document why; do not let a typo silently pass."
        )


# ---------------------------------------------------------------------------
# C-11 — opt-in: nothing in the repository enables either unit.
# ---------------------------------------------------------------------------


def _tracked_py_and_sh_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files"], cwd=repo_root, capture_output=True, text=True,
            timeout=15, check=False,
        )
        if result.returncode == 0:
            return [
                repo_root / p for p in result.stdout.splitlines()
                if (p.endswith(".py") or p.endswith(".sh"))
                and (repo_root / p).is_file()
            ]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return [
        p for p in repo_root.rglob("*")
        if p.is_file() and p.suffix in (".py", ".sh") and ".git" not in p.parts
    ]


def test_nothing_in_the_repository_enables_the_idle_stop_timer(repo_root: pathlib.Path) -> None:
    """C-11 "opt-in — nothing in the repo may enable them": Given every
    tracked `*.py`/`*.sh` file (the repo's only executable source; `*.md`
    prose documenting the OPERATOR's own install step, e.g.
    `docs/scheduling.md`'s existing `systemctl --user enable --now
    partgraph-refresh-all.timer` instructions, is explicitly OUT of scope —
    that is the documented PROCEDURE an operator runs by hand, exactly
    like the refresh timer's own install step, not something the repo
    itself executes).
    When each is scanned for `enable` alongside the new unit's own name.
    Then none combines the two — the repository itself never runs
    `systemctl --user enable` (or `--now`) naming
    `partgraph-db-idle-stop`."""
    offenders: list[str] = []
    for path in _tracked_py_and_sh_files(repo_root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _UNIT_NAME in line and "enable" in line.lower():
                offenders.append(f"{path.relative_to(repo_root)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "the repository's own executable source must never enable the "
        "idle-stop unit itself — an operator installs it by hand:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# C-11 — reuses the existing refresh units as the pattern (structural
# similarity, not a byte-for-byte diff).
# ---------------------------------------------------------------------------


def test_service_mirrors_the_refresh_services_structural_shape(
    repo_root: pathlib.Path, service_text: str
) -> None:
    """C-11 "reuse the existing refresh units as the pattern": Given
    `systemd/partgraph-refresh-all.service` already exists and is this
    repo's own established shape for a per-user, hardened, one-shot unit.
    When both files are compared structurally.
    Then the new service ALSO declares `Type=oneshot` in `[Service]` and its
    own leading comment block (before `[Unit]`) mentions BOTH "per-user"
    (or "never root") AND "%h" — the same two properties the refresh
    service's own header calls out explicitly."""
    refresh_path = repo_root / REFRESH_SERVICE_REL
    assert refresh_path.exists(), f"{REFRESH_SERVICE_REL} is expected to already exist."
    refresh_text = refresh_path.read_text(encoding="utf-8")
    refresh_body = [ln.strip() for ln in _section_body(_lines(refresh_text), "Service")]
    assert "Type=oneshot" in refresh_body, (
        "sanity check: the reference refresh service must itself declare "
        "Type=oneshot for this mirror check to mean anything."
    )

    lines = _lines(service_text)
    service_body = [ln.strip() for ln in _section_body(lines, "Service")]
    assert "Type=oneshot" in service_body, (
        f"{SERVICE_REL} should mirror the refresh unit's own Type=oneshot shape: {service_body!r}"
    )

    unit_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip() == "[Unit]"), None
    )
    assert unit_idx is not None, f"{SERVICE_REL} has no [Unit] section."
    header = "\n".join(lines[:unit_idx]).lower()
    assert "per-user" in header or "never root" in header, (
        f"{SERVICE_REL}'s header comment should state it is per-user / never "
        "root, mirroring the refresh service's own header."
    )
    assert "%h" in header, (
        f"{SERVICE_REL}'s header comment should mention the %h specifier, "
        "mirroring the refresh service's own header."
    )


# ---------------------------------------------------------------------------
# THE property check — systemd-analyze verify, exiting 0 with empty output.
# Confirmed, in THIS sandbox, to actually exercise real verification: running
# it against the two ALREADY-EXISTING refresh units
# (`systemd-analyze verify systemd/partgraph-refresh-all.service
# systemd/partgraph-refresh-all.timer`) exits 0 with empty stdout/stderr.
# ---------------------------------------------------------------------------


def test_systemd_analyze_verify_accepts_both_units_with_empty_output(
    repo_root: pathlib.Path,
) -> None:
    """C-11 [property, not proxy]: Given `systemd-analyze` is on PATH (skips
    cleanly, not a failure, if it is not — this is an environment
    capability, not a contract this repo controls) and both unit files
    exist.
    When `systemd-analyze verify <service> <timer>` is run against the REAL,
    committed files.
    Then it exits 0 with EMPTY stdout AND stderr — `systemd-analyze verify`
    prints nothing on success and something on every warning/error, so
    empty output is itself the pass signal, not merely the exit code.
    """
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is not available on PATH in this environment.")
    service_path = repo_root / SERVICE_REL
    timer_path = repo_root / TIMER_REL
    if not (service_path.exists() and timer_path.exists()):
        pytest.skip(f"{SERVICE_REL} / {TIMER_REL} do not exist yet (expected pre-PR-C).")

    result = subprocess.run(
        ["systemd-analyze", "verify", str(service_path), str(timer_path)],
        cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, (
        f"systemd-analyze verify failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert result.stdout == "" and result.stderr == "", (
        f"systemd-analyze verify printed diagnostics on an otherwise-zero "
        f"exit — treat any output as a real finding to fix, not noise:\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
