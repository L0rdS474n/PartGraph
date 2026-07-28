"""
Tests: PR-B1 (feat/db-lifecycle-doctor-and-docs) — a NEW leaf function
``partgraph.util.lifecycle.volume_exists`` (AC-B3, the "volume exists" bullet).

PR-B1's objective is a read-only diagnostic (``partgraph db doctor``, AC-B3)
that must report "whether the ``partgraph_dgraph_data`` volume exists". No
existing seam in ``partgraph.util.lifecycle`` (landed by PR-A, see
``tests/unit/test_lifecycle.py``) answers that question: ``unit_state()``
covers the quadlet unit, ``find_partgraph_instances()`` covers containers. A
new, narrowly-scoped, READ-ONLY leaf function is required.

DESIGN DECISION (documented here because it is not dictated by any AC, and a
future reader needs the reasoning, not just the assertions): this function is
added to ``partgraph.util.lifecycle`` — NOT inlined into ``partgraph.cli`` —
because it is a generic engine-level read, exactly like ``unit_state()``
(also engine/systemd-level, not CLI-specific), and PR-A's own module
docstring reserves ``partgraph.cli`` for orchestration/printing only (the one
exception, ``_run_compose`` in cli.py, exists because it needs the CLI-level
``COMPOSE_FILE`` path constant — a genuinely CLI-specific concern that does
not apply here). This reuses PR-A's established injected-seam style
(``engine_prefix``/``which``/``environ``, all keyword-only, all resolved at
CALL time) rather than inventing a parallel one.

Pinned contract (NOT YET IMPLEMENTED — collection of THIS file is expected to
ERROR with ImportError until ``partgraph.util.lifecycle.volume_exists``
exists; the correct test-first RED state. Deliberately scoped to import ONLY
the one new symbol plus existing, already-landed PR-A symbols needed for
fixtures, so this file's collection failure is isolated to the NEW addition —
it does not retroactively turn ``tests/unit/test_lifecycle.py`` (PR-A's own,
currently-passing suite) red merely because one more name was appended to a
shared import list):

  ``volume_exists(*, engine_prefix: list[str] | None = None,
  which: Callable[[str], str | None] | None = None,
  environ: Mapping[str, str] | None = None) -> bool | None``

  READ-ONLY. Runs exactly ONE subprocess call:
  ``<engine_prefix> volume inspect --format json <PARTGRAPH_DATA_VOLUME>``
  (mirrors ``_mounts_data_volume``'s ``container inspect --format json``
  shape/spirit — same engine, same "inspect a named thing" pattern). TRI-STATE
  return, exactly like ``UnitState.wanted_by_default`` and the internal
  ``_mounts_data_volume`` tri-state (Gate 5 finding A precedent) — "I could
  not tell" must never collapse into a guessed False:

    - ``True``  — the engine confirmed the volume exists (inspect exit 0).
    - ``False`` — the engine confirmed the volume does NOT exist (inspect
      exits non-zero — a real engine's own "no such volume" outcome).
    - ``None``  — could NOT be determined: the subprocess call itself failed
      (``OSError``/``subprocess.SubprocessError``, e.g. a timeout or a
      missing binary in the narrow post-``which``-check race).

  ``engine_prefix`` defaults to :func:`partgraph.util.container.engine_command`
  when ``None`` (resolved with the given ``which``/``environ``), identical to
  ``find_partgraph_instances``. A ``ContainerEngineError`` raised by that
  detection is NEVER caught here — it propagates uncaught, exactly like
  ``find_partgraph_instances``' own documented behaviour (an enumeration/probe
  that never happened must never degrade to a guessed answer). The CLI layer
  (``partgraph db doctor``) is responsible for catching it, mirroring how
  ``db down`` catches it around its OWN ``engine_command()`` call in cli.py,
  not inside the leaf.

  The call ALWAYS carries: a list argv (never a string), ``shell=False``,
  ``capture_output=True``, ``text=True``, ``check=False``, and a finite,
  positive ``timeout=`` (mirrors every other subprocess call in this module —
  ADR-0007's bounded-constant precedent). The argv never contains ``-f``,
  ``--force``, ``rm``, ``create``, or ``prune`` — this is a pure ``inspect``,
  never a mutation (belt-and-suspenders at the leaf level; the repo-wide
  static guard lives in
  ``tests/unit/test_repo_never_executes_lifecycle_mutations.py``, AC-B2).

Hermetic: every test here patches ONLY ``subprocess.run`` (and, where engine
auto-detection is exercised, ``shutil.which``) — mirrors
``tests/unit/test_lifecycle.py``'s own style. No socket, no sleep, no real
engine, no real wall clock.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

# This import is expected to raise ImportError until
# ``partgraph.util.lifecycle.volume_exists`` exists — the correct test-first
# RED state. Scoped to ONLY the new symbol plus stable, already-landed PR-A
# symbols so a collection failure here never touches test_lifecycle.py.
from partgraph.util.lifecycle import (  # noqa: E402
    PARTGRAPH_DATA_VOLUME,
    volume_exists,
)


class _Proc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _is_volume_inspect_call(argv: list[str]) -> bool:
    return "volume" in argv and "inspect" in argv


# ---------------------------------------------------------------------------
# Tri-state outcome
# ---------------------------------------------------------------------------


def test_volume_exists_true_on_clean_inspect_success() -> None:
    """Given the engine's ``volume inspect`` exits 0 (the volume exists).
    When volume_exists() is called.
    Then it returns True.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Proc(
            returncode=0,
            stdout=f'[{{"Name": "{PARTGRAPH_DATA_VOLUME}"}}]',
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = volume_exists(engine_prefix=["docker"])

    assert result is True
    assert len(calls) == 1
    assert _is_volume_inspect_call(calls[0])
    assert calls[0][-1] == PARTGRAPH_DATA_VOLUME, (
        f"the inspect target must be the exact PARTGRAPH_DATA_VOLUME literal: {calls[0]!r}"
    )


def test_volume_exists_false_on_no_such_volume_nonzero_exit() -> None:
    """Given the engine's ``volume inspect`` exits non-zero (a real engine's
    "no such volume" outcome).
    When volume_exists() is called.
    Then it returns False — a POSITIVE confirmation of absence, not a guess.
    """
    def fake_run(argv, **kwargs):
        return _Proc(returncode=1, stderr="Error: no such volume")

    with patch("subprocess.run", side_effect=fake_run):
        result = volume_exists(engine_prefix=["docker"])

    assert result is False


@pytest.mark.parametrize(
    "raised",
    [
        subprocess.TimeoutExpired(cmd=["docker", "volume", "inspect"], timeout=10),
        OSError("engine binary vanished"),
    ],
    ids=["timeout", "oserror"],
)
def test_volume_exists_none_when_the_call_cannot_be_executed(raised: Exception) -> None:
    """Given the ``volume inspect`` subprocess call itself fails to execute
    (a timeout, or an OSError such as a binary that vanished between the PATH
    check and the exec).
    When volume_exists() is called.
    Then it returns None — UNDETERMINED, never collapsed into False. Reporting
    a genuinely unknown volume state as "confirmed absent" is exactly the
    false-success class of bug PR-A's Gate 5 finding A already forbade for
    Instance.mounts_data_volume; this function must not silently reintroduce
    it for the volume-existence question.
    """
    def fake_run(argv, **kwargs):
        raise raised

    with patch("subprocess.run", side_effect=fake_run):
        result = volume_exists(engine_prefix=["docker"])

    assert result is None


def test_volume_exists_true_even_if_the_success_body_is_unparseable() -> None:
    """Given the engine's ``volume inspect`` exits 0 (success) but its stdout
    body is garbage (a corrupt/truncated response).
    When volume_exists() is called.
    Then it still returns True — existence is decided by the ENGINE's own
    exit code, never by successfully parsing the body (mirrors
    ``_mounts_data_volume``'s "the returncode already answered the yes/no
    question" discipline; a body-parse failure must never downgrade a
    confirmed-positive exit code into an unknown or a false negative).
    """
    def fake_run(argv, **kwargs):
        return _Proc(returncode=0, stdout="{not valid json at all]###")

    with patch("subprocess.run", side_effect=fake_run):
        result = volume_exists(engine_prefix=["docker"])

    assert result is True


# ---------------------------------------------------------------------------
# argv / subprocess-call hygiene
# ---------------------------------------------------------------------------


def test_volume_exists_argv_never_carries_a_mutating_flag_or_verb() -> None:
    """[AC-B2, leaf-level belt-and-suspenders] Given volume_exists() runs
    exactly one subprocess call.
    When the call's argv is inspected.
    Then it never contains '-f', '--force', 'rm', 'create', or 'prune' — the
    verb surface is 'inspect' only, exactly like the module's container-level
    reads.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Proc(returncode=0, stdout="[]")

    with patch("subprocess.run", side_effect=fake_run):
        volume_exists(engine_prefix=["docker"])

    assert len(calls) == 1
    forbidden = {"-f", "--force", "rm", "create", "prune"}
    for token in calls[0]:
        assert token not in forbidden, f"volume_exists() argv carries a mutating token: {calls[0]!r}"


def test_volume_exists_subprocess_call_carries_shell_false_and_a_finite_timeout() -> None:
    """[ADR-0007 bounded-constant precedent] Given every subprocess call this
    module makes is documented to carry a finite, named timeout and
    ``shell=False``.
    When volume_exists() issues its one call.
    Then the kwargs show ``shell=False``, ``capture_output=True``,
    ``text=True``, ``check=False``, and a ``timeout`` that is a positive,
    finite float — never None/unbounded.
    """
    captured_kwargs: dict = {}

    def fake_run(argv, **kwargs):
        captured_kwargs.update(kwargs)
        return _Proc(returncode=0, stdout="[]")

    with patch("subprocess.run", side_effect=fake_run):
        volume_exists(engine_prefix=["docker"])

    assert captured_kwargs.get("shell") is False
    assert captured_kwargs.get("capture_output") is True
    assert captured_kwargs.get("text") is True
    assert captured_kwargs.get("check") is False
    timeout = captured_kwargs.get("timeout")
    assert isinstance(timeout, float)
    assert timeout > 0


def test_volume_exists_never_calls_a_second_subprocess() -> None:
    """Given this is a single, narrow existence check.
    When volume_exists() is called.
    Then EXACTLY one subprocess call is made — no follow-up call of any kind
    (mirrors the "READ-ONLY, one call" contract of unit_state()).
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Proc(returncode=0, stdout="[]")

    with patch("subprocess.run", side_effect=fake_run):
        volume_exists(engine_prefix=["docker"])

    assert len(calls) == 1, f"expected exactly one subprocess call, got: {calls!r}"


# ---------------------------------------------------------------------------
# Engine-prefix resolution (mirrors find_partgraph_instances' own contract)
# ---------------------------------------------------------------------------


def test_volume_exists_auto_detects_engine_prefix_when_none_given() -> None:
    """Given engine_prefix=None (the default).
    When volume_exists() is called with a `which` that reports only "podman"
    on PATH.
    Then the resulting argv is prefixed with "podman" — detection is
    delegated to partgraph.util.container.engine_command(), never hard-coded.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Proc(returncode=0, stdout="[]")

    def fake_which(name: str) -> str | None:
        return "/usr/bin/podman" if name == "podman" else None

    with patch("subprocess.run", side_effect=fake_run):
        volume_exists(which=fake_which, environ={})

    assert len(calls) == 1
    assert calls[0][0] == "podman"


def test_volume_exists_propagates_container_engine_error_uncaught() -> None:
    """Given engine_prefix=None and NEITHER docker nor podman is on PATH.
    When volume_exists() is called.
    Then ContainerEngineError propagates OUT uncaught — mirrors
    find_partgraph_instances()' documented behaviour: a probe that never ran
    must never degrade into a guessed None/False. The CLI layer (partgraph db
    doctor) is responsible for catching this, exactly as db down catches it
    around its own engine_command() call.
    """
    from partgraph.util.container import ContainerEngineError

    def fake_which(name: str) -> str | None:
        return None

    with pytest.raises(ContainerEngineError):
        volume_exists(which=fake_which, environ={})
