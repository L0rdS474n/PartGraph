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
one that hard-codes this machine's own ``/home/<realname>/`` instead of a
templated ``$HOME``/``%h``, or one that recommends the ``StopTimeout=``
drop-in WITHOUT the honest caveat that it does not, by itself, restore
signal delivery — each of those would fail a specific test below, not just
"the file doesn't exist yet".

RED-STATE CONVENTION: the file does not exist yet, so
``test_doc_file_exists_and_is_nonempty`` below is a HARD, non-skipped
failure (the base fact every other test depends on). Every OTHER test in
this file uses a module-scoped fixture that SKIPS individually, with a clear
reason, while the file is absent — mirrors
``tests/unit/test_lifecycle_architecture.py``'s own established convention
in this exact repo for "a file this PR's implementer has not written yet":
skip cleanly, go green the moment the file lands with the right content, and
FAIL (not skip) if it lands with the WRONG content.

AC-PRIV OVERLAP (disclosed, not duplicated): once ``docs/db-lifecycle.md``
exists, ``tests/unit/test_repo_skeleton.py::test_no_operator_home_paths_in_tracked_files``
ALREADY scans every tracked file (this one included) for a real
``/home/<realname>/`` path — that NEGATIVE check is not re-implemented here.
This file adds only the POSITIVE half that scanner does not cover: that a
``$HOME``/``%h`` placeholder is actually PRESENT, i.e. the doc could not
simply omit any home-directory reference at all and still pass.
"""

from __future__ import annotations

import pathlib
import re

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


#: Mirrors tests/unit/test_repo_skeleton.py's own _HOME_PATH_PATTERN exactly
#: (kept as an independent, self-contained copy per this codebase's own
#: documented convention of not sharing internals across test files). The
#: repo-wide scanner in that file already asserts NO tracked file (this one
#: included, once it exists) contains a real, non-placeholder home path; this
#: local copy exists only so THIS file's own test suite is self-contained and
#: independently readable, not to duplicate that scanner's authority.
_HOME_PATH_PATTERN = re.compile(
    r"(/home/(?!(?:user|operator|dev|test|example|you|me|username|admin|vagrant)/)[^/\s\"']+/"
    r"|/Users/(?!(?:user|operator|dev|test|example|you|me|username|admin|vagrant)/)[^/\s\"']+/)"
)


def test_doc_never_hardcodes_a_real_operator_home_path(doc_text: str) -> None:
    """AC-B1: Given the doc must never leak this (or any) machine's actual
    home directory.
    When we scan its text for a concrete '/home/<realname>/' pattern.
    Then none is found (the global scanner in test_repo_skeleton.py already
    enforces this across every tracked file; this is the same rule, checked
    locally and independently for this specific, security-sensitive file).
    """
    match = _HOME_PATH_PATTERN.search(doc_text)
    assert match is None, (
        f"docs/db-lifecycle.md contains a real operator home path: {match.group(0)!r}. "
        "Use '$HOME' or '%h' instead."
    )


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
