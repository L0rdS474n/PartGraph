"""
Tests: PR-B1 (feat/db-lifecycle-doctor-and-docs) — AC-B1, AC-B1b.

``docs/db-lifecycle.md`` is the OPERATOR-run remediation this repo hands out
instead of ever touching ``~/.config/containers/systemd/`` itself (AC-B2, the
"documents and detects, never executes" hard rule). This file does NOT
create that document — creating it is the implementer's job (explicitly out
of scope for this test-first pass) — it pins the properties that document
must hold, hermetically, once it exists.

Every test here is genuinely FALSIFIABLE against a wrong-but-plausible draft:
a doc that merely SAYS "edit the unit file" without the ``WantedBy=`` key,
one that hard-codes this machine's own home directory instead of a templated
``$HOME``/``%h``, one that recommends the ``StopTimeout=`` drop-in WITHOUT
the honest caveat that it does not, by itself, restore signal delivery, or
one that tells the operator to edit files in
``~/.config/containers/systemd/`` without ever naming which file is
PartGraph's or warning about its five neighbours — each of those would fail
a specific test below, not just "the file doesn't exist yet".

RED-STATE CONVENTION (this file was written test-first, before the doc
existed): while ``docs/db-lifecycle.md`` was absent,
``test_doc_file_exists_and_is_nonempty`` below was a HARD, non-skipped
failure (the base fact every other test depends on), and every OTHER test
used a module-scoped fixture that SKIPPED individually, with a clear reason
— mirrors ``tests/unit/test_lifecycle_architecture.py``'s own established
convention in this exact repo for "a file this PR's implementer has not
written yet": skip cleanly, go green the moment the file lands with the
right content, and FAIL (not skip) if it lands with the WRONG content.
``docs/db-lifecycle.md`` now exists and every test below is green — the
fixture and its skip branch are kept exactly as they were, unchanged,
because the mechanism itself (not merely its RED-phase behaviour) is still
load-bearing: it is what makes this file's content assertions run at all,
and what would make them skip cleanly again rather than crash outright if
the file were ever deleted.

AC-PRIV OVERLAP (disclosed, not duplicated — and DELIBERATELY not
re-implemented locally, see the note below): ``docs/db-lifecycle.md``, now
that it exists, is scanned like every other tracked file by
``tests/unit/test_repo_skeleton.py::test_no_operator_home_paths_in_tracked_files``
for a real, concrete, non-placeholder ``/home/`` path — that NEGATIVE check
is intentionally NOT duplicated here (an earlier draft of this file DID keep
a local copy of that scanner's own regex for self-contained readability,
mirroring CONTRIBUTING.md's "Test fixtures stay local to their file" policy
— but a regex whose OWN negative-lookahead source text contains a literal
``/home/(?!...)`` substring is, ironically, itself something the SIMPLE
substring-based scanner reads as a real home path; the copy self-matched and
broke `test_repo_skeleton.py`, exactly the kind of independent-duplication
cost CONTRIBUTING.md's own new paragraph asks contributors to weigh. Here the
weighing comes out the other way: the check is fully, uniformly covered by
the always-on repo-wide scanner the moment this file's own text exists, so a
second, narrower, hazard-prone copy buys nothing). This file adds only the
POSITIVE half the repo-wide scanner does not and cannot cover: that a
``$HOME``/``%h`` placeholder is actually PRESENT, i.e. the doc could not
simply omit any home-directory reference at all and still pass.
"""

from __future__ import annotations

import pathlib

import pytest

DOC_REL_PATH = "docs/db-lifecycle.md"


def test_doc_file_exists_and_is_nonempty(repo_root: pathlib.Path) -> None:
    """AC-B1: Given docs/db-lifecycle.md is PR-B1's operator-run remediation.
    When we look for it at the repo root.
    Then it exists and is non-empty. This is a HARD failure (not a skip) —
    every other test in this file depends on this file existing.
    """
    doc_path = repo_root / DOC_REL_PATH
    assert doc_path.exists(), (
        f"{DOC_REL_PATH} does not exist yet. PR-B1 requires it: the documented, "
        "operator-run procedure for stopping the quadlet unit's autostart-at-login."
    )
    assert doc_path.is_file(), f"{DOC_REL_PATH} exists but is not a regular file."
    assert doc_path.stat().st_size > 0, f"{DOC_REL_PATH} exists but is empty."


@pytest.fixture(scope="module")
def doc_text(repo_root: pathlib.Path) -> str:
    """Return docs/db-lifecycle.md's full text, or skip cleanly if absent.

    Skipping (not failing) here is deliberate: the base "must exist" fact is
    already pinned, hard, by test_doc_file_exists_and_is_nonempty above; a
    test that CANNOT even read the file has no content to assert properties
    about, and a chain of misleading content-assertion failures on an empty
    string would obscure the one real problem ("the file is missing") behind
    noise.
    """
    doc_path = repo_root / DOC_REL_PATH
    if not doc_path.exists():
        pytest.skip(f"{DOC_REL_PATH} does not exist yet (expected pre-PR-B1).")
    return doc_path.read_text(encoding="utf-8")


def _window_after(text: str, anchor: str, size: int = 1200) -> str:
    """Return up to *size* characters of *text* starting at the first
    occurrence of *anchor*. Fails the calling test (via a plain AssertionError,
    not a silent empty string) if *anchor* is absent at all.
    """
    idx = text.find(anchor)
    assert idx != -1, f"expected to find {anchor!r} in docs/db-lifecycle.md, but it is absent."
    return text[idx : idx + size]


# ---------------------------------------------------------------------------
# AC-B1 — no operator home path; $HOME/%h placeholders only
# ---------------------------------------------------------------------------


def test_doc_uses_a_home_placeholder_not_a_concrete_path(doc_text: str) -> None:
    """AC-B1: Given the doc must reference the operator's systemd user-unit
    directory (``~/.config/containers/systemd/``) to be useful at all.
    When we scan its text.
    Then it uses the templated ``$HOME`` or the systemd specifier ``%h`` —
    never a bare, un-templated reference implying a fixed path — proving the
    doc was written to be copy-pastable by ANY operator, not this machine's
    own operator.
    """
    assert "$HOME" in doc_text or "%h" in doc_text, (
        "docs/db-lifecycle.md must reference the operator's home directory via "
        "the '$HOME' shell variable or the systemd '%h' specifier — never a "
        "hard-coded path."
    )


# NOTE: no local _HOME_PATH_PATTERN copy here — see the module docstring's
# "AC-PRIV OVERLAP" note for why keeping one turned out to be actively
# harmful (a literal, self-matching duplicate of test_repo_skeleton.py's own
# regex) rather than merely redundant. The negative check (no real home path)
# is covered exclusively by the always-on repo-wide scanner in
# tests/unit/test_repo_skeleton.py once this file's text exists.


# ---------------------------------------------------------------------------
# AC-B1 — the documented procedure: WantedBy= removal AND daemon-reload
# ---------------------------------------------------------------------------


def test_doc_documents_removing_the_wanted_by_key(doc_text: str) -> None:
    """AC-B1: Given the ONLY documented way to remove a quadlet unit's
    autostart is editing 'WantedBy=' in the .container file or a drop-in
    (podman-systemd.unit(5) — quadlet units cannot be `systemctl --user
    enable`/`disable`d).
    When we scan the doc's text.
    Then it names the literal systemd key 'WantedBy=' AND uses removal
    language ('remove'/'delete'/'drop') in its vicinity — not merely
    mentioning the word "autostart" in the abstract.
    """
    assert "WantedBy=" in doc_text, (
        "docs/db-lifecycle.md must name the literal systemd key 'WantedBy=' — "
        "the only documented way to remove quadlet autostart."
    )
    window = _window_after(doc_text, "WantedBy=")
    low = window.lower()
    assert any(word in low for word in ("remove", "delete", "drop")), (
        f"expected removal language near the first 'WantedBy=' mention: {window!r}"
    )


def test_doc_documents_systemctl_user_daemon_reload(doc_text: str) -> None:
    """AC-B1: Given editing a quadlet's .container file requires
    'systemctl --user daemon-reload' before systemd notices the change.
    When we scan the doc's text.
    Then the exact phrase 'systemctl --user daemon-reload' appears.
    """
    assert "systemctl --user daemon-reload" in doc_text, (
        "docs/db-lifecycle.md must document the exact command "
        "'systemctl --user daemon-reload' — editing WantedBy= alone is not "
        "enough; systemd must be told to reload unit files."
    )


def test_doc_requires_wanted_by_removal_via_a_drop_in_not_a_direct_edit(doc_text: str) -> None:
    """[Gate 3a BLOCKING 2, "Recommended" half] Given AC-B1b already requires
    the StopTimeout= budget to be documented as a drop-in rather than a
    direct edit of the generated .container file — and the SAME reasoning
    applies to WantedBy=: hand-editing a quadlet-generated file directly is
    strictly more dangerous than a drop-in override in the shared
    ~/.config/containers/systemd/ directory that also holds five OTHER
    units, because a direct edit invites opening (and mis-editing) the wrong
    file entirely, where a drop-in's own filename names only the unit it
    overrides.
    When we scan the window after the doc's first 'WantedBy=' mention.
    Then it documents the removal via a drop-in (a '.d/' override directory,
    or 'override.conf', or the word 'drop-in' itself) — exactly the same
    three acceptable phrasings already required for StopTimeout=.
    """
    window = _window_after(doc_text, "WantedBy=", size=1500).lower()
    assert "drop-in" in window or ".d/" in window or "override.conf" in window, (
        f"expected the WantedBy= removal to be documented via a DROP-IN, "
        f"exactly like StopTimeout= already must be: {window!r}"
    )


# ---------------------------------------------------------------------------
# [Gate 3a BLOCKING 2] The shared directory holds SIX unit files; five are
# NOT PartGraph's. A doc that never distinguishes them, followed correctly,
# lets an operator delete a stranger's container by mistake — exactly the
# outcome this whole PR exists to prevent, achieved through documentation
# instead of code.
# ---------------------------------------------------------------------------

#: The five real, observed foreign units sharing
#: ~/.config/containers/systemd/ with partgraph-dgraph's own unit (ADR-0021).
_FOREIGN_UNIT_NAMES = ("cve-alpha", "cve-loader", "cve-ratel", "cve-zero", "min-web")


def test_doc_names_the_specific_unit_file_near_the_wanted_by_instructions(doc_text: str) -> None:
    """[Gate 3a BLOCKING 2] Given the doc's WantedBy= removal instructions
    are the exact place an operator could edit the WRONG file.
    When we scan the window after the doc's first 'WantedBy=' mention.
    Then it names the SPECIFIC unit file — 'partgraph-dgraph.container' or
    'partgraph-dgraph.service' — not just "the unit file" in the abstract.
    """
    window = _window_after(doc_text, "WantedBy=", size=1500)
    assert "partgraph-dgraph.container" in window or "partgraph-dgraph.service" in window, (
        "docs/db-lifecycle.md must name the SPECIFIC unit file "
        "('partgraph-dgraph.container' or 'partgraph-dgraph.service') within "
        f"the same window as its WantedBy= removal instructions: {window!r}"
    )


def test_doc_warns_against_touching_the_other_units_in_the_shared_directory(doc_text: str) -> None:
    """[Gate 3a BLOCKING 2] Given ~/.config/containers/systemd/ holds SIX
    unit files on the host this ADR describes — partgraph-dgraph's own, and
    five belonging to an entirely unrelated cve-graph/min-web stack this
    repo has promised never to touch (ADR-0021) — and a doc that never once
    distinguishes them would pass every other test in this file while an
    operator following it correctly could delete a stranger's container.
    When we scan the window after the doc's first 'WantedBy=' mention.
    Then EITHER all five real foreign unit names are present (an explicit,
    host-specific warning) OR an unambiguous generic caution is present:
    the word "only" together with "partgraph-dgraph" and a negation word
    ("other"/"not"/"never") — the generic form is PREFERRED (the doc ships
    to any operator, not just this host's), but either satisfies the
    underlying safety requirement.
    """
    window = _window_after(doc_text, "WantedBy=", size=2500)
    low = window.lower()
    names_all_foreign_units = all(name in low for name in _FOREIGN_UNIT_NAMES)
    generic_caution = (
        "only" in low
        and "partgraph-dgraph" in low
        and any(word in low for word in ("other", "not ", "never", "no other"))
    )
    assert names_all_foreign_units or generic_caution, (
        "docs/db-lifecycle.md must warn the operator against touching any "
        "OTHER file in ~/.config/containers/systemd/ — either by naming all "
        f"five real foreign units ({', '.join(_FOREIGN_UNIT_NAMES)}), or "
        "(preferred) with an explicit, generic 'edit only "
        f"partgraph-dgraph...' caution. Window scanned:\n{window!r}"
    )


# ---------------------------------------------------------------------------
# AC-B1b — the StopTimeout= drop-in, and the honest caveat that it does NOT
# restore signal delivery by itself.
# ---------------------------------------------------------------------------


def test_doc_documents_a_host_side_stop_timeout_drop_in(doc_text: str) -> None:
    """AC-B1b: Given PR-A's own STOP_GRACE_SECONDS docstring explicitly
    scopes its 60s budget to paths PartGraph owns directly (its own engine
    `stop` sweep, and Compose) and states the quadlet path needs "host-side
    unit changes (PR-B1)".
    When we scan the doc's text.
    Then it documents the systemd key 'StopTimeout=' AND names it as a
    drop-in (an override file under a unit '.d/' directory, or an
    'override.conf', or the word 'drop-in' itself) — not a change to
    docker/docker-compose.yml, which this repo does not control the
    quadlet's shutdown timeout through at all.
    """
    assert "StopTimeout=" in doc_text, (
        "docs/db-lifecycle.md must document the systemd 'StopTimeout=' key as "
        "the host-side lever for the quadlet unit's own shutdown budget."
    )
    window = _window_after(doc_text, "StopTimeout=").lower()
    assert (
        "drop-in" in window
        or ".d/" in window
        or "override.conf" in window
    ), (
        f"expected 'StopTimeout=' to be documented as a DROP-IN (not a direct "
        f"edit of a generated .container file, and not a docker-compose.yml "
        f"change): {window!r}"
    )


def test_doc_honestly_states_a_drop_in_alone_does_not_restore_signal_delivery(
    doc_text: str,
) -> None:
    """AC-B1b [the honesty requirement, not optional]: Given raising
    StopTimeout= alone does NOT give the quadlet-started container a real
    init process (that container still gets bash as PID 1, running `dgraph
    alpha` in the foreground — the exact structural cause PR-A's own
    STOP_GRACE_SECONDS docstring diagnosed and fixed ONLY for the Compose
    path via `init: true`). A StopTimeout= drop-in on its own only lengthens
    the wait before the engine gives up and sends SIGKILL; it does not make
    SIGTERM reach `dgraph alpha`.
    When we scan the text following the doc's first 'StopTimeout=' mention.
    Then it explicitly says the container still lacks an init process (or
    equivalently, that SIGTERM still will not be delivered / forwarded), and
    that this means the outcome is still SIGKILL (or "kill"/"killed") — not
    silently overclaiming that the drop-in alone produces a graceful
    shutdown.
    """
    window = _window_after(doc_text, "StopTimeout=", size=2000)
    low = window.lower()
    mentions_missing_init_or_undelivered_signal = (
        "init process" in low
        or "no init" in low
        or "not forwarded" in low
        or "will not be delivered" in low
        or "never reach" in low
        or "does not reach" in low
        or "won't reach" in low
    )
    assert mentions_missing_init_or_undelivered_signal, (
        "docs/db-lifecycle.md must honestly state, near its StopTimeout= "
        f"documentation, that the quadlet-started container still has no init "
        f"process and SIGTERM still will not be delivered to dgraph — a drop-in "
        f"alone must never be presented as a full fix. Text scanned:\n{window!r}"
    )
    mentions_still_sigkilled = "sigkill" in low or "killed" in low
    assert mentions_still_sigkilled, (
        "docs/db-lifecycle.md must state that the outcome, even with a "
        f"StopTimeout= drop-in, is still a SIGKILL (only a longer wait for one) "
        f"— never claim a graceful SIGTERM shutdown for the quadlet path. Text "
        f"scanned:\n{window!r}"
    )
