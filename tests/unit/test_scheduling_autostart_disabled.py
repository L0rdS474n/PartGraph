"""
Tests: PR-B2 (feat/db-lazy-autostart) — Gate 3b BLOCKING 3 / Gate 5 FAIL: the
scheduling contract must not be silently broken by autostart.

Both `partgraph refresh` and `partgraph refresh-links` are in PR-B2's
autostart allowlist (see `tests/unit/test_cli_autostart.py`'s B-6 section).
`systemd/partgraph-refresh-all.service` (triggered weekly by
`partgraph-refresh-all.timer`) runs exactly those two commands, unattended,
on a schedule, via `scripts/partgraph-refresh-all.sh`. Several places already
promise the scheduling layer never manages the database lifecycle —
`docs/decisions/ADR-0014-external-scheduling.md` D1, `docs/scheduling.md`'s
own `[!WARNING]` block, `systemd/partgraph-refresh-all.service`'s own header
comment, and `src/partgraph/cli.py`'s command docstrings. With autostart
default-ON, that promise depends on something ACTUALLY forcing
`PARTGRAPH_AUTOSTART=0` for the scheduled path.

GATE 5 CORRECTION — read this before trusting anything below about
`Environment=`/`EnvironmentFile=` ordering. The FIRST version of this file
pinned `systemd/partgraph-refresh-all.service`'s `Environment=
PARTGRAPH_AUTOSTART=0` line as authoritative BECAUSE it was placed textually
AFTER the unit's `EnvironmentFile=-%h/.config/partgraph/refresh-all.env`
directive, on the claimed premise that "systemd applies same-key directives
in file order, last one wins." A live systemd experiment (reproduced with
disposable transient units, both orderings tested) DISPROVED that premise
outright:

    EnvironmentFile= before Environment=  ->  VAR=fromEnvFile
    Environment= before EnvironmentFile=  ->  VAR=fromEnvFile

`EnvironmentFile=` wins for a shared key REGARDLESS of directive order.
Reordering the two lines in the unit does nothing. The old test asserting
"the `Environment=` line appears after every `EnvironmentFile=` line, so it
wins" was therefore asserting a TEXTUAL PROXY for the property ("this line
comes later") rather than the property itself ("this value takes effect") —
it stayed green while the guarantee it was named for was false, and has been
DELETED, not weakened. Concrete consequence this reopened: an operator who
creates the documented, intentionally-supported
`~/.config/partgraph/refresh-all.env` for ANY reason (copied from
elsewhere, set for interactive use, stale from before this feature existed)
and that file happens to contain its own `PARTGRAPH_AUTOSTART=` line would
have silently overridden the unit's `0`, on a schedule, unattended — the
exact unattended container ADR-0022 exists to eliminate.

THE ACTUAL FIX (implemented in `scripts/partgraph-refresh-all.sh`, NOT this
file — this file only pins tests): the wrapper script itself `export`s
`PARTGRAPH_AUTOSTART=0` before invoking `partgraph`, unconditionally. A
shell `export` inside the running script happens AFTER systemd (or cron, or
an interactive shell) has already assembled that process's environment, so
it OVERWRITES whatever value the parent handed it — this is plain POSIX
shell variable-assignment semantics, not a systemd feature, and is therefore
NOT subject to the `Environment=`/`EnvironmentFile=` precedence quirk at
all. This is provably the RIGHT layer for the guarantee to live in.

WHAT THIS FILE NOW PINS, and how each piece is verified:

  1. THE PROPERTY ITSELF, hermetically, at the level where it actually
     holds: `test_wrapper_exports_partgraph_autostart_zero_regardless_of_
     inherited_value` drives the REAL, committed
     `scripts/partgraph-refresh-all.sh` as a subprocess (mirrors
     `tests/unit/test_scheduling_wrapper.py`'s own stub-double technique)
     with a deliberately HOSTILE environment — `PARTGRAPH_AUTOSTART` unset,
     or pre-set to `"1"`/`"true"`/`"no"` before the wrapper even starts —
     and inspects what the STUB `partgraph` child process actually observes
     via `os.environ`. This is not a textual proxy: it spawns the real
     wrapper and reads the real child environment, so it tests the property
     itself, not a stand-in for it. It does not itself invoke systemd — but
     the mechanism it verifies (a shell `export`) is by construction
     systemd-independent, which is the whole point of moving it here, so no
     systemd invocation is needed to verify it soundly.
  2. The unit's OWN `Environment=PARTGRAPH_AUTOSTART=0` line, if it remains
     in `systemd/partgraph-refresh-all.service`, is pinned as PRESENT ONLY
     — never as authoritative, never as "wins" or "last word". Its own
     docstring says plainly why: it is defense-in-depth for the common case
     (an operator who has never touched the optional env file), not a
     guarantee, because `EnvironmentFile=` beats it unconditionally when the
     same key appears in both.
  3. The docs/unit-header text is checked for MENTIONING
     `PARTGRAPH_AUTOSTART` at all (a documentation-consistency check), never
     for describing a correct ordering mechanism — that claim has been
     removed from what this file asserts.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

SERVICE_REL = "systemd/partgraph-refresh-all.service"
SCHEDULING_DOC_REL = "docs/scheduling.md"
WRAPPER_REL = "scripts/partgraph-refresh-all.sh"
SUBPROCESS_TIMEOUT_S = 15


def _service_text(repo_root: pathlib.Path) -> str:
    path = repo_root / SERVICE_REL
    assert path.exists(), f"{SERVICE_REL} does not exist."
    return path.read_text(encoding="utf-8")


def _scheduling_doc_text(repo_root: pathlib.Path) -> str:
    path = repo_root / SCHEDULING_DOC_REL
    assert path.exists(), f"{SCHEDULING_DOC_REL} does not exist."
    return path.read_text(encoding="utf-8")


def _service_lines(text: str) -> list[str]:
    return text.splitlines()


def _find_line_index(lines: list[str], predicate) -> int | None:
    for i, line in enumerate(lines):
        if predicate(line):
            return i
    return None


# ---------------------------------------------------------------------------
# The property itself — a hermetic subprocess test of the REAL wrapper.
# ---------------------------------------------------------------------------

#: Fake `partgraph` CLI double, local to THIS file (per CONTRIBUTING.md's
#: "test fixtures stay local to their file" — not shared with, or imported
#: from, `tests/unit/test_scheduling_wrapper.py`'s own stub, even though the
#: two are similar). NOT production code: exists only inside a test's
#: `tmp_path`, never installed, never on the real PATH outside a test
#: process. Records, per invocation, the subcommand AND whatever value (or
#: absence) of `PARTGRAPH_AUTOSTART` it observes in ITS OWN environment —
#: `[[ -v PARTGRAPH_AUTOSTART ]]` distinguishes "unset" from "set to an
#: empty string" from "set to a value", so the log is unambiguous. Always
#: exits 0 (this file is not testing the phase-exit-aggregation contract —
#: that is `test_scheduling_wrapper.py`'s job — only what environment the
#: child actually sees).
_AUTOSTART_STUB_SCRIPT = r"""#!/usr/bin/env bash
set -uo pipefail
subcommand="${1:-}"
if [[ -n "${STUB_LOG:-}" ]]; then
    if [[ -v PARTGRAPH_AUTOSTART ]]; then
        printf '%s PARTGRAPH_AUTOSTART=%s\n' "$subcommand" "$PARTGRAPH_AUTOSTART" >> "$STUB_LOG"
    else
        printf '%s PARTGRAPH_AUTOSTART=<unset>\n' "$subcommand" >> "$STUB_LOG"
    fi
fi
exit 0
"""


@pytest.fixture
def bash_path() -> str:
    """Resolve `bash` on PATH, or skip cleanly (never hard-fail) if absent."""
    resolved = shutil.which("bash")
    if resolved is None:
        pytest.skip("bash is not available on PATH; cannot exercise the wrapper script.")
    return resolved


def _write_autostart_stub(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "partgraph"
    path.write_text(_AUTOSTART_STUB_SCRIPT, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_wrapper_and_capture_child_autostart_env(
    repo_root: pathlib.Path,
    bash_path: str,
    tmp_path: pathlib.Path,
    *,
    inherited_autostart: str | None,
) -> list[str]:
    """Run the REAL, committed wrapper script with *inherited_autostart*
    already present (or deliberately absent) in the PARENT environment
    handed to it, and return each stub invocation's own logged
    `PARTGRAPH_AUTOSTART` observation, in call order.
    """
    stub = _write_autostart_stub(tmp_path)
    log_path = tmp_path / "autostart_env.log"
    wrapper_path = repo_root / WRAPPER_REL
    assert wrapper_path.is_file(), f"{WRAPPER_REL} does not exist at {wrapper_path}"

    env = dict(os.environ)
    env["PARTGRAPH_BIN"] = str(stub)
    env["STUB_LOG"] = str(log_path)
    env.pop("PARTGRAPH_REFRESH_FETCH", None)
    if inherited_autostart is None:
        env.pop("PARTGRAPH_AUTOSTART", None)
    else:
        env["PARTGRAPH_AUTOSTART"] = inherited_autostart

    result = subprocess.run(
        [bash_path, str(wrapper_path)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=env,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, (
        f"the wrapper itself failed unexpectedly (the stub always exits 0, "
        f"so a non-zero wrapper exit here means the wrapper's OWN logic "
        f"broke, not a phase failure):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    if not log_path.exists():
        return []
    return log_path.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("inherited_autostart", "case_id"),
    [
        pytest.param(None, "unset_in_parent_environment"),
        pytest.param("1", "hostile_inherited_one_would_enable_autostart_if_not_overridden"),
        pytest.param("true", "hostile_inherited_true_synonym"),
        pytest.param(
            "no",
            "inherited_a_recognised_off_synonym_still_forced_to_the_canonical_0",
        ),
    ],
)
def test_wrapper_exports_partgraph_autostart_zero_regardless_of_inherited_value(
    repo_root: pathlib.Path,
    bash_path: str,
    tmp_path: pathlib.Path,
    inherited_autostart: str | None,
    case_id: str,
) -> None:
    """[Gate 5 fix — the actual property, not a proxy] Given
    `PARTGRAPH_AUTOSTART` is EITHER absent from the wrapper's own parent
    environment, or already present there with a value that would (if left
    alone) leave autostart ON per the parsing table
    `tests/unit/test_cli_autostart.py` pins — modelling exactly the
    real-world hazard: an operator's `~/.config/partgraph/refresh-all.env`,
    a stale exported value in whatever shell profile spawns the wrapper, or
    simply the variable never having been set at all.
    When the REAL, committed `scripts/partgraph-refresh-all.sh` runs as a
    subprocess with that environment, invoking a stub `partgraph` that
    records the `PARTGRAPH_AUTOSTART` value it ACTUALLY observes in its own
    environment.
    Then BOTH invocations (`refresh`, then `refresh-links`) observe the
    EXACT literal `PARTGRAPH_AUTOSTART=0` — proving the wrapper's own
    `export` unconditionally overwrites whatever it inherited, for every
    `partgraph` call it makes, not merely the first one, and not merely
    when the variable started out unset.
    """
    lines = _run_wrapper_and_capture_child_autostart_env(
        repo_root, bash_path, tmp_path, inherited_autostart=inherited_autostart
    )
    assert len(lines) == 2, (
        f"expected exactly 2 stub invocations (refresh, refresh-links), "
        f"got: {lines!r}"
    )
    subcommands = [line.split(" ", 1)[0] for line in lines]
    assert subcommands == ["refresh", "refresh-links"], (
        f"expected refresh then refresh-links, got: {subcommands!r}"
    )
    for line in lines:
        assert line.endswith("PARTGRAPH_AUTOSTART=0"), (
            f"the wrapper must export PARTGRAPH_AUTOSTART=0 before EVERY "
            f"partgraph invocation, overriding whatever the parent process "
            f"(systemd, cron, an interactive shell) had already set — got: "
            f"{line!r} (parent environment had "
            f"PARTGRAPH_AUTOSTART={inherited_autostart!r})"
        )


# ---------------------------------------------------------------------------
# The unit-file line — PRESENT ONLY, never claimed authoritative.
# ---------------------------------------------------------------------------


def test_scheduling_service_declares_partgraph_autostart_zero_as_defense_in_depth(
    repo_root: pathlib.Path,
) -> None:
    """[Gate 5 fix — honesty about what this line actually does] Given
    `systemd/partgraph-refresh-all.service` still declares
    `Environment=PARTGRAPH_AUTOSTART=0` in its `[Service]` section.
    When the unit file is scanned.
    Then that line is PRESENT — this test asserts EXISTENCE ONLY. It does
    NOT assert, and must never again assert, that this line is
    authoritative, "wins", or is "the last word": a live systemd experiment
    (see this file's own module docstring) proved `EnvironmentFile=` beats a
    same-key `Environment=` unconditionally, REGARDLESS of which one is
    written later in the unit file. This line is defense-in-depth for the
    common case only — an operator who has never added a conflicting
    `PARTGRAPH_AUTOSTART=` line to their own, optional
    `~/.config/partgraph/refresh-all.env`. The ACTUAL guarantee is proven by
    `test_wrapper_exports_partgraph_autostart_zero_regardless_of_inherited_
    value` above, against the wrapper script, not against this unit file.
    """
    text = _service_text(repo_root)
    lines = _service_lines(text)
    service_idx = _find_line_index(lines, lambda ln: ln.strip() == "[Service]")
    assert service_idx is not None, f"{SERVICE_REL} has no [Service] section."

    matches = [
        i
        for i, line in enumerate(lines)
        if i > service_idx and line.strip() == "Environment=PARTGRAPH_AUTOSTART=0"
    ]
    assert matches, (
        f"{SERVICE_REL} no longer declares 'Environment=PARTGRAPH_AUTOSTART=0' "
        "in its [Service] section. If it was deliberately removed because the "
        "wrapper-level export now makes it redundant, this test should be "
        "deleted rather than left failing — but confirm the wrapper-level "
        "property test above is still green first."
    )


# ---------------------------------------------------------------------------
# Documentation-consistency checks — presence of the term only, never a
# claim about the correctness of a mechanism described in prose.
# ---------------------------------------------------------------------------


def _warning_block(doc_text: str) -> str:
    """Return the first `[!WARNING]` GitHub-alert blockquote's own text
    (every `>`-prefixed line starting at `[!WARNING]`, up to the first
    non-`>` line), or an empty string if none is found.
    """
    lines = doc_text.splitlines()
    start = _find_line_index(lines, lambda ln: "[!WARNING]" in ln)
    if start is None:
        return ""
    block: list[str] = []
    for line in lines[start:]:
        if not line.strip().startswith(">"):
            break
        block.append(line)
    return "\n".join(block)


def test_scheduling_doc_warning_names_partgraph_autostart(repo_root: pathlib.Path) -> None:
    """Given `docs/scheduling.md`'s own `[!WARNING]` block is the
    operator-facing promise that "This scheduling layer only runs the
    refresh commands; it does not start, stop, or health-check the
    database."
    When that block's own text is extracted and scanned.
    Then it names `PARTGRAPH_AUTOSTART` — a reader must be told WHY that
    promise still holds once `refresh`/`refresh-links` become
    autostart-capable commands (ADR-0022 Section 7). This is a
    documentation-CONSISTENCY check only (the term is mentioned somewhere) —
    it makes no claim about which mechanism (unit-level vs wrapper-level)
    the prose attributes the guarantee to, or whether that attribution is
    itself accurate; docs/ prose is out of scope for this file to fix.
    """
    doc_text = _scheduling_doc_text(repo_root)
    block = _warning_block(doc_text)
    assert block, f"{SCHEDULING_DOC_REL} has no '[!WARNING]' blockquote at all."
    assert "PARTGRAPH_AUTOSTART" in block, (
        f"{SCHEDULING_DOC_REL}'s [!WARNING] block does not mention "
        "PARTGRAPH_AUTOSTART — it still reads as an unconditional claim "
        "that the scheduling layer is unaffected by autostart, which is "
        "only true because SOMETHING explicitly opts out; the warning must "
        "say so.\n\nBlock text:\n" + block
    )
    assert "does not start" in block.lower() or "never manages the database" in block.lower(), (
        f"{SCHEDULING_DOC_REL}'s [!WARNING] block must still make the "
        "original 'never starts the database' claim (now WITH the "
        "PARTGRAPH_AUTOSTART explanation), not merely mention the variable "
        "in isolation.\n\nBlock text:\n" + block
    )


def test_scheduling_service_header_names_partgraph_autostart(repo_root: pathlib.Path) -> None:
    """Given the unit's own leading comment block claims "this unit does NOT
    start or stop it" (the database).
    When the comment block BEFORE the first `[Unit]` section is scanned.
    Then it names `PARTGRAPH_AUTOSTART` — a documentation-consistency check
    only, mirroring the docs-warning check above; makes no claim about
    mechanism correctness.
    """
    text = _service_text(repo_root)
    lines = _service_lines(text)
    unit_idx = _find_line_index(lines, lambda ln: ln.strip() == "[Unit]")
    assert unit_idx is not None, f"{SERVICE_REL} has no [Unit] section."
    header = "\n".join(lines[:unit_idx])
    assert "PARTGRAPH_AUTOSTART" in header, (
        f"{SERVICE_REL}'s own header comment (before [Unit]) does not "
        "mention PARTGRAPH_AUTOSTART — it still reads as an unconditional "
        "'this unit does NOT start or stop it' claim, true only because "
        "SOMETHING explicitly disables autostart; the header should say so."
        "\n\nHeader text:\n" + header
    )
