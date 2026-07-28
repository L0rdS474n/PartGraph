"""
Tests: PR-B2 (feat/db-lazy-autostart) — Gate 3b BLOCKING 3: the scheduling
contract must not be silently broken by autostart.

Both `partgraph refresh` and `partgraph refresh-links` are in PR-B2's
autostart allowlist (see `tests/unit/test_cli_autostart.py`'s B-6 section).
`systemd/partgraph-refresh-all.service` (triggered weekly by
`partgraph-refresh-all.timer`) runs exactly those two commands, unattended,
on a schedule. Four places already promise the scheduling layer never manages
the database lifecycle:

  - `docs/decisions/ADR-0014-external-scheduling.md` D1: "the DB is never
    started here ... Periodic execution is left to your host's scheduler
    ... The scheduling layer also never manages the database lifecycle."
  - `docs/scheduling.md`'s own `[!WARNING]` block: "This scheduling layer
    only runs the refresh commands; it does not start, stop, or
    health-check the database."
  - `systemd/partgraph-refresh-all.service`'s own unit header comment: "this
    unit does NOT start or stop it."
  - `src/partgraph/cli.py`'s own docstrings for both commands (ADR-0012/0013
    D9's "is the database running?" hint on failure, not a start attempt).

With autostart default-ON and no explicit opt-out shipped, the timer would
start the container on a schedule the moment PR-B2 lands — an unattended
container left running on a host that ALSO runs an unrelated cve-graph
stack, which is exactly the resource waste ADR-0022 exists to eliminate,
reintroduced through the scheduling door instead of the login door ADR-0022
already closed.

Resolution (Option A, decided by the coordinator, not this file): the
shipped scheduling layer sets `PARTGRAPH_AUTOSTART=0` explicitly, so
ADR-0014 D1's guarantee survives for the timer while interactive use still
gets lazy start. This file pins the two invariants that resolution depends
on — it does not rewrite ADR-0022's breaking-changes section or restructure
`docs/scheduling.md`'s prose, both of which remain the implementer's job:

  1. `systemd/partgraph-refresh-all.service` sets `PARTGRAPH_AUTOSTART=0`,
     placed so it CANNOT be silently overridden by the optional,
     operator-editable `EnvironmentFile=-%h/.config/partgraph/refresh-all.env`
     (systemd applies same-key `Environment=`/`EnvironmentFile=` directives
     in file order, last one wins — the pin below is literally the LAST
     `PARTGRAPH_AUTOSTART` assignment in the unit file).
  2. `docs/scheduling.md`'s `[!WARNING]` block and the unit's own header
     comment both name `PARTGRAPH_AUTOSTART`, so a reader is told WHY the
     "never manages the database lifecycle" claim still holds once autostart
     ships, rather than the claim silently becoming stale prose the moment
     `ensure_running()` lands in `src/`.

An EnvironmentFile-only resolution (shipping a default env file that sets
`PARTGRAPH_AUTOSTART=0`, with no explicit `Environment=` line in the unit
itself) would NOT satisfy invariant 1 as written here: the ONLY
EnvironmentFile this repo ships is the OPTIONAL, leading-`-` (missing-file-
is-non-fatal) operator config at `%h/.config/partgraph/refresh-all.env`,
which most installs will never create — a guarantee that depends on a file
most operators do not have is not a guarantee at all. The explicit
`Environment=PARTGRAPH_AUTOSTART=0` line this file pins is what the unit
actually ships; see the module docstring above for why an EnvironmentFile
alone would not have been sufficient.
"""

from __future__ import annotations

import pathlib
import re

SERVICE_REL = "systemd/partgraph-refresh-all.service"
SCHEDULING_DOC_REL = "docs/scheduling.md"


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
# Invariant 1 — the unit sets PARTGRAPH_AUTOSTART=0, and last-wins.
# ---------------------------------------------------------------------------


def test_scheduling_service_sets_partgraph_autostart_zero(repo_root: pathlib.Path) -> None:
    """Gate 3b BLOCKING 3, invariant 1: Given the shipped
    `partgraph-refresh-all.service` runs `partgraph refresh`/`refresh-links`
    — both autostart-allowlisted commands.
    When the unit's `[Service]` section is scanned.
    Then it declares `Environment=PARTGRAPH_AUTOSTART=0` literally — the
    ONLY value that reliably disables autostart per the parsing table
    `tests/unit/test_cli_autostart.py` pins (`"0"` is recognised
    unconditionally; a mis-cased synonym is not worth the ambiguity risk in
    a committed, non-operator-edited file).
    """
    text = _service_text(repo_root)
    lines = _service_lines(text)
    service_idx = _find_line_index(lines, lambda ln: ln.strip() == "[Service]")
    assert service_idx is not None, f"{SERVICE_REL} has no [Service] section."

    matches = [
        i for i, line in enumerate(lines)
        if i > service_idx and line.strip() == "Environment=PARTGRAPH_AUTOSTART=0"
    ]
    assert matches, (
        f"{SERVICE_REL} does not set 'Environment=PARTGRAPH_AUTOSTART=0' in "
        "its [Service] section — a scheduled run of refresh/refresh-links "
        "(both autostart-allowlisted) would autostart the database on a "
        "timer the moment PR-B2 lands, reintroducing the exact unattended "
        "container-left-running waste ADR-0022 exists to eliminate."
    )


def test_scheduling_service_autostart_zero_is_the_last_word_after_environment_file(
    repo_root: pathlib.Path,
) -> None:
    """Gate 3b BLOCKING 3, invariant 1 (the ordering half): Given the unit
    ALSO reads an OPTIONAL, operator-editable `EnvironmentFile=` (the leading
    '-' makes a missing file non-fatal) that could, in principle, itself set
    `PARTGRAPH_AUTOSTART` to something else.
    When both directives' positions in the unit file are compared.
    Then the unit's OWN `Environment=PARTGRAPH_AUTOSTART=0` line appears
    AFTER every `EnvironmentFile=` line — systemd applies same-key
    `Environment=`/`EnvironmentFile=` directives in FILE ORDER, last one
    wins, so this ordering is what makes the guarantee un-overridable by an
    operator's own env file rather than merely present-but-defeatable.
    """
    text = _service_text(repo_root)
    lines = _service_lines(text)

    autostart_idx = _find_line_index(
        lines, lambda ln: ln.strip() == "Environment=PARTGRAPH_AUTOSTART=0"
    )
    assert autostart_idx is not None, (
        f"{SERVICE_REL} does not set 'Environment=PARTGRAPH_AUTOSTART=0' at "
        "all (see the sibling test) — cannot check its ordering."
    )

    environment_file_indices = [
        i for i, line in enumerate(lines) if line.strip().startswith("EnvironmentFile=")
    ]
    assert environment_file_indices, (
        f"{SERVICE_REL} declares no EnvironmentFile= at all — the ordering "
        "guarantee this test pins has nothing to be ordered against; if the "
        "optional operator env file was intentionally removed, this test "
        "should be revisited, not silently left passing vacuously."
    )
    assert autostart_idx > max(environment_file_indices), (
        f"'Environment=PARTGRAPH_AUTOSTART=0' (line {autostart_idx + 1}) must "
        f"appear AFTER every 'EnvironmentFile=' directive (last at line "
        f"{max(environment_file_indices) + 1}), so an operator's own env "
        "file can never silently override it (systemd: same-key "
        "Environment=/EnvironmentFile= directives apply in file order, last "
        "one wins)."
    )


# ---------------------------------------------------------------------------
# Invariant 2 — the docs and the unit header explain WHY the guarantee holds.
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
    """Gate 3b BLOCKING 3, invariant 2 (docs half): Given `docs/scheduling.md`'s
    own `[!WARNING]` block is the operator-facing promise that "This
    scheduling layer only runs the refresh commands; it does not start,
    stop, or health-check the database."
    When that block's own text is extracted and scanned.
    Then it names `PARTGRAPH_AUTOSTART` — a reader must be told WHY that
    promise still holds once `refresh`/`refresh-links` become
    autostart-capable commands (ADR-0022 Section 7), not left to find the
    claim silently contradicted by `--help` or by reading the source.
    """
    doc_text = _scheduling_doc_text(repo_root)
    block = _warning_block(doc_text)
    assert block, f"{SCHEDULING_DOC_REL} has no '[!WARNING]' blockquote at all."
    assert "PARTGRAPH_AUTOSTART" in block, (
        f"{SCHEDULING_DOC_REL}'s [!WARNING] block does not mention "
        "PARTGRAPH_AUTOSTART — it still reads as an unconditional claim "
        "that the scheduling layer is unaffected by autostart, which is "
        "only true because the shipped unit explicitly opts out; the "
        "warning must say so.\n\nBlock text:\n" + block
    )
    assert "does not start" in block.lower() or "never manages the database" in block.lower(), (
        f"{SCHEDULING_DOC_REL}'s [!WARNING] block must still make the "
        "original 'never starts the database' claim (now WITH the "
        "PARTGRAPH_AUTOSTART explanation), not merely mention the variable "
        "in isolation.\n\nBlock text:\n" + block
    )


def test_scheduling_service_header_names_partgraph_autostart(repo_root: pathlib.Path) -> None:
    """Gate 3b BLOCKING 3, invariant 2 (unit-header half): Given the unit's
    own leading comment block claims "this unit does NOT start or stop it"
    (the database).
    When the comment block BEFORE the first `[Unit]` section is scanned.
    Then it names `PARTGRAPH_AUTOSTART` — the same consistency requirement
    as the docs warning, pinned independently against the unit file itself
    so the two cannot drift apart without failing a test each.
    """
    text = _service_text(repo_root)
    lines = _service_lines(text)
    unit_idx = _find_line_index(lines, lambda ln: ln.strip() == "[Unit]")
    assert unit_idx is not None, f"{SERVICE_REL} has no [Unit] section."
    header = "\n".join(lines[:unit_idx])
    assert "PARTGRAPH_AUTOSTART" in header, (
        f"{SERVICE_REL}'s own header comment (before [Unit]) does not "
        "mention PARTGRAPH_AUTOSTART — it still reads as an unconditional "
        "'this unit does NOT start or stop it' claim, true only because the "
        "unit explicitly disables autostart further down; the header "
        "should say so.\n\nHeader text:\n" + header
    )


def test_scheduling_doc_and_unit_agree_autostart_is_explicitly_disabled(
    repo_root: pathlib.Path,
) -> None:
    """Gate 3b BLOCKING 3 (cross-file consistency): Given both
    `docs/scheduling.md` and `systemd/partgraph-refresh-all.service`
    independently claim the scheduling layer never manages the database
    lifecycle.
    When both are read together.
    Then BOTH mention `PARTGRAPH_AUTOSTART` (proven by the two sibling tests
    above) AND the unit genuinely sets it to `"0"` (proven by
    `test_scheduling_service_sets_partgraph_autostart_zero`) — this test
    exists as the single, named assertion a future reviewer can point at to
    confirm the three-way consistency (doc claim, unit header claim, unit
    ACTUAL behaviour) holds as ONE fact, not three independently-drifting
    ones.
    """
    service_text = _service_text(repo_root)
    doc_text = _scheduling_doc_text(repo_root)

    assert "Environment=PARTGRAPH_AUTOSTART=0" in service_text, (
        f"{SERVICE_REL} must actually set PARTGRAPH_AUTOSTART=0, not merely "
        "mention it in prose."
    )
    assert "PARTGRAPH_AUTOSTART" in _warning_block(doc_text), (
        f"{SCHEDULING_DOC_REL}'s warning must explain the same guarantee "
        f"{SERVICE_REL} actually implements."
    )
