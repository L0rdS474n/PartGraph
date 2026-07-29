"""
Tests: dependency-pinning policy for `[project.dependencies]` / dev extras
(build/pin-runtime-dependencies).

Context (the evidence this policy is argued from, not asserted from memory):

- `ruff` is the ONE runtime/dev dependency already pinned exactly, and it is
  pinned for a documented reason (see its own comment in pyproject.toml): an
  unpinned lint gate resolved ruff 0.16.0, which promoted `PLR0917` out of
  preview, and CI failed on code that had been on `main`, unchanged and
  green, for months. That incident is the template this file generalises
  from: a dependency earns a version bound when there is EVIDENCE it can
  silently change behaviour the repository relies on, not merely because it
  is a runtime dependency.

- `psutil` is load-bearing for a stop-or-not decision since ADR-0023: `db
  idle-stop` decides whether a lease's process is genuinely alive using
  `(pid, create_time)`, and only a *confirmed* dead process (a clean
  `NoSuchProcess`, or a live PID whose `create_time` differs from the
  recorded one) lets a lease be cleaned. `activity.py`'s own tolerance
  comment names the unsafe direction explicitly: "a false 'dead' would let a
  stop through while real work is in flight."

  Read directly from psutil's own published changelog
  (https://psutil.io/changelog/, fetched during this analysis):

    * `7.1.0` (2025-09-17): "[Linux]: `Process.create_time()` now uses a
      monotonic clock, preventing `Process.is_running()` from returning
      wrong results after system clock updates." (#2526)
    * `7.1.1`/`7.1.2`/`7.1.3` (2025-10-19 .. 2025-11-02): "[Linux], [macOS],
      [NetBSD]: `Process.create_time()` does not reflect system clock
      updates." (#2541, #2570, #2578)

  Together these confirm that, on Linux (this repository's actual target
  platform), every psutil release BEFORE `7.1.0` could return a
  `create_time()` value for a still-running process that shifts after an
  ordinary NTP correction — which is exactly the "false dead" misclassification
  `activity.py`'s tolerance comment calls out as the unsafe direction. `7.1.3`
  is used as the floor here (the last release in that specific fix cluster,
  comfortably below the installed `7.2.2`).

  The same changelog's `8.0.0 (IN DEVELOPMENT)` section opens with: "psutil
  8.0 introduces breaking API changes. See the migration guide if upgrading
  from 7.x." — a direct, textual statement (not an inference) that the next
  major release is unsafe to resolve implicitly.

- `pydgraph`'s own CHANGELOG.md (github.com/dgraph-io/pydgraph, fetched
  during this analysis) shows its major version tracks the Dgraph SERVER's
  own release line: `v25.0.0` ships "Updated proto definitions to support
  Dgraph v25 API", and `v25.1.0` marks `DgraphClientStub.from_cloud()` /
  `.parse_host()` deprecated with "removal planned for 26.0.0". This
  repository's own `docker/docker-compose.yml` pins the server image to
  `dgraph/standalone:v25.3.4` — an unbound client resolving ahead of that
  server generation is a real, evidenced risk pattern, not a hypothetical
  one; it is weighted [PROBABLE] rather than [CONFIRMED] because, unlike
  psutil, no already-shipped release is documented as having actually broken
  this repository's usage — the evidence is a forward-looking deprecation
  notice plus a consistent versioning convention, not a retroactive incident.

- `typer`, `rich`, `httpx`, `pyyaml`, and `requests` are treated as
  INCIDENTAL here (no version bound), each for a reason established by
  reading what the code actually depends on rather than what it imports:

    * `rich` is presentation-only (progress bars, tables); a break here is
      cosmetic and loud, never a silently wrong answer.
    * `typer`'s surface (`typer.Typer`, `typer.Option`, `typer.Exit`) is
      exercised by the entire CLI test suite on every run; a breaking change
      fails loudly at collection/runtime, not silently.
    * `httpx` is always called with EXPLICIT `follow_redirects=`/`timeout=`
      (`src/partgraph/ingest/fetch.py`, `src/partgraph/cli.py`), so the one
      documented httpx default that famously changed across versions
      (`allow_redirects`/`follow_redirects` flipping to `False` by default)
      is already neutralised by never relying on the default. No code in
      this repository catches a specific `httpx.*` exception class; every
      call site catches broad `Exception`, so httpx's exception hierarchy is
      not load-bearing either.
    * `pydgraph`'s OTHER documented risk — its default gRPC message-size
      ceiling (ADR-0010) — is also already neutralised: `cli.py` builds the
      stub with explicit `grpc.max_receive_message_length` /
      `grpc.max_send_message_length` options, never relying on pydgraph's
      own default. Only the server-protocol-generation coupling above
      remains a live risk.
    * `pyyaml` is declared as a RUNTIME dependency but has zero runtime
      usage: `grep -rn "yaml" src/` (this analysis) finds nothing. Its only
      consumers are `tests/unit/test_ci_workflow.py` and
      `tests/unit/test_docker_compose.py`. This is a real, separate finding
      about WHERE the dependency is declared, not about its version, and is
      out of scope for a version-pinning change; it is recorded here rather
      than silently left unmentioned.
    * `requests`' specific relied-upon behaviour (the timeout-float-splits
      claim, and the `Timeout`/`RequestException` exception hierarchy
      `partgraph.util.health`/`index_health` catch in order) is pinned
      BEHAVIOURALLY, against the real installed library, in
      `tests/unit/test_requests_timeout_semantics_real.py` — the stronger
      "property, not proxy" alternative to a version bound. `requests` also
      ships frequent CVE fixes (two in the release history read during this
      analysis: CVE-2024-47081, CVE-2026-25645); pinning it defensively
      would trade those patches away for a risk this repository has not
      observed materialising.

This file is a MANIFEST CONTRACT (a proxy, by necessity: a version bound is
declared text, not runtime behaviour) — it exists specifically so a future
edit that drops or widens a bound is caught, which is the one thing a
behavioural test cannot do on its own. Where the underlying behaviour CAN
also be pinned directly against a real, installed library, see
`tests/unit/test_psutil_process_identity_real.py` and
`tests/unit/test_requests_timeout_semantics_real.py`.

These tests are RED until pyproject.toml is updated with the bounds argued
above; they do not require psutil/pydgraph/etc. to be installed at any
particular version to parse the manifest, only `packaging` (already an
indirect dependency of `pytest`, per `pytest`'s own `Requires:` metadata,
confirmed via `pip show pytest` against the real environment).
"""

from __future__ import annotations

import pathlib
import tomllib

import pytest
from packaging.requirements import Requirement

_TESTS_UNIT_DIR = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_UNIT_DIR.parent.parent
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"

#: Runtime dependencies this policy deliberately leaves BARE (no version
#: specifier) — see the module docstring for the argued reason behind each.
#: A behavioural pin exists separately for `requests` (see module docstring).
_INCIDENTAL_UNBOUNDED_DEPENDENCIES = ("typer", "rich", "httpx", "pyyaml", "requests")


def _load_pyproject() -> dict:
    """Load and return the parsed pyproject.toml content."""
    assert _PYPROJECT_PATH.is_file(), (
        f"pyproject.toml not found at {_PYPROJECT_PATH}. "
        "Test must be run from within the PartGraph repository."
    )
    return tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))


def _runtime_dependencies() -> list[str]:
    data = _load_pyproject()
    deps = data.get("project", {}).get("dependencies", [])
    assert isinstance(deps, list), f"project.dependencies must be a list; got {type(deps)!r}"
    return deps


def _dev_dependencies() -> list[str]:
    data = _load_pyproject()
    deps = data.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    assert isinstance(deps, list), f"optional-dependencies.dev must be a list; got {type(deps)!r}"
    return deps


def _requirement_named(deps: list[str], name: str) -> Requirement:
    """Parse *deps* as PEP 508 requirement strings and return the one named
    *name* (case-insensitive, matching PyPI's own normalisation).
    """
    matches = [Requirement(dep) for dep in deps if Requirement(dep).name.lower() == name.lower()]
    assert matches, f"{name!r} not found. Declared dependencies were: {deps!r}"
    assert len(matches) == 1, f"{name!r} declared more than once: {deps!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# Sanity: every declared runtime dependency parses as a valid requirement
# ---------------------------------------------------------------------------


def test_every_runtime_dependency_parses_as_a_valid_pep_508_requirement() -> None:
    """Given [project.dependencies].
    When each entry is parsed with packaging.requirements.Requirement.
    Then none raises — a typo here (e.g. a stray comma, an unclosed bracket)
    must fail loudly in CI, not silently resolve to something unintended by
    pip's own more lenient-looking error surface.
    """
    for dep in _runtime_dependencies():
        Requirement(dep)  # raises InvalidRequirement on a malformed entry


# ---------------------------------------------------------------------------
# psutil — floor AND ceiling, both evidenced (ADR-0023 limitation 2)
# ---------------------------------------------------------------------------


def test_psutil_dependency_excludes_the_pre_7_1_3_create_time_ntp_bug() -> None:
    """Given [project.dependencies]'s psutil entry.
    When its specifier is checked against 7.1.2 — the newest release still
    carrying the confirmed create_time()-drifts-across-a-system-clock-update
    bug psutil's own changelog documents as fixed across 7.1.0-7.1.3
    (#2526, #2541, #2570, #2578).
    Then 7.1.2 must NOT satisfy the declared specifier: a bare "psutil" (the
    status quo) happily resolves it, and that release's create_time() can
    misclassify a still-running lease as a recycled (dead) PID after an
    ordinary NTP correction — the exact unsafe direction activity.py's own
    tolerance comment names ("a false 'dead' would let a stop through while
    real work is in flight").
    """
    req = _requirement_named(_runtime_dependencies(), "psutil")
    assert not req.specifier.contains("7.1.2", prereleases=True), (
        f"psutil's pyproject.toml specifier {req.specifier!r} still admits 7.1.2, which "
        "carries the create_time()/NTP-update bug psutil fixed in 7.1.0-7.1.3. It must "
        "declare a floor of at least psutil>=7.1.3."
    )


def test_psutil_dependency_admits_the_fixed_and_currently_installed_versions() -> None:
    """Given the psutil specifier.
    When checked against 7.1.3 (the fix's last release) and 7.2.2 (the
    version actually installed in this repository's environment, confirmed
    via `importlib.metadata.version("psutil")`).
    Then both must satisfy it — the floor must not be so tight it excludes
    the version already verified to work.
    """
    req = _requirement_named(_runtime_dependencies(), "psutil")
    assert req.specifier.contains("7.1.3", prereleases=True), (
        f"psutil's specifier {req.specifier!r} excludes 7.1.3, the fixed release "
        "the recommended floor is meant to admit."
    )
    assert req.specifier.contains("7.2.2", prereleases=True), (
        f"psutil's specifier {req.specifier!r} excludes 7.2.2, the version actually "
        "installed and exercised by this test suite today."
    )


def test_psutil_dependency_excludes_the_documented_breaking_major_version() -> None:
    """Given psutil's own changelog names `8.0.0` (IN DEVELOPMENT at the time
    of this analysis) as introducing breaking API changes ("psutil 8.0
    introduces breaking API changes. See the migration guide if upgrading
    from 7.x.").
    When the psutil specifier is checked against 8.0.0.
    Then it must NOT satisfy the declared specifier.
    """
    req = _requirement_named(_runtime_dependencies(), "psutil")
    assert not req.specifier.contains("8.0.0", prereleases=True), (
        f"psutil's specifier {req.specifier!r} admits 8.0.0, which psutil's own changelog "
        "documents as introducing breaking API changes. It must declare a ceiling of psutil<8."
    )


# ---------------------------------------------------------------------------
# pydgraph — [PROBABLE] evidence: server-generation coupling, not a
# retroactive incident. See module docstring for the confidence distinction.
# ---------------------------------------------------------------------------


def test_pydgraph_dependency_excludes_a_pre_25_2_release() -> None:
    """Given [project.dependencies]'s pydgraph entry.
    When checked against 24.3.0 (a real, published pydgraph release that
    predates the "v25 API" proto regeneration pydgraph's own CHANGELOG.md
    documents for 25.0.0) and 25.2.0 (the version actually installed here).
    Then 24.3.0 must NOT satisfy the specifier, and 25.2.0 must.
    """
    req = _requirement_named(_runtime_dependencies(), "pydgraph")
    assert not req.specifier.contains("24.3.0", prereleases=True), (
        f"pydgraph's specifier {req.specifier!r} admits 24.3.0, predating the v25 API proto "
        "regeneration; it must declare a floor of at least pydgraph>=25.2.0."
    )
    assert req.specifier.contains("25.2.0", prereleases=True), (
        f"pydgraph's specifier {req.specifier!r} excludes 25.2.0, the version actually "
        "installed and exercised by this test suite today."
    )


def test_pydgraph_dependency_excludes_the_next_major_release_line() -> None:
    """Given pydgraph's own CHANGELOG.md marks `DgraphClientStub.from_cloud()`
    / `.parse_host()` deprecated in 25.1.0 with "removal planned for
    26.0.0", and this repository's `docker/docker-compose.yml` pins the
    Dgraph SERVER image to `dgraph/standalone:v25.3.4`.
    When the pydgraph specifier is checked against 26.0.0.
    Then it must NOT satisfy the declared specifier — an unbound client
    resolving a major line ahead of the pinned server generation is the
    real risk pattern pydgraph's own versioning convention (major version
    tracks the Dgraph server's major release line) predicts.
    """
    req = _requirement_named(_runtime_dependencies(), "pydgraph")
    assert not req.specifier.contains("26.0.0", prereleases=True), (
        f"pydgraph's specifier {req.specifier!r} admits 26.0.0, the next major release line; "
        "it must declare a ceiling of pydgraph<26 to stay paired with the pinned v25 server."
    )


# ---------------------------------------------------------------------------
# The five incidental dependencies stay bare — a ratchet, not a proxy for
# "these can never be pinned": it forces any FUTURE bound to be a conscious,
# reasoned edit to this test rather than a silent `pip freeze`-style pin.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _INCIDENTAL_UNBOUNDED_DEPENDENCIES)
def test_incidental_runtime_dependency_stays_unpinned_without_a_documented_cause(
    name: str,
) -> None:
    """Given a runtime dependency this analysis found no evidenced,
    load-bearing, version-sensitive semantic for (see the module docstring
    for the reasoning behind each of the five).
    When its pyproject.toml entry is parsed.
    Then it carries NO version specifier at all. This is deliberately a
    trip-wire, not a ceiling: if a future change adds a bound to one of
    these five, this test goes red, and whoever added the bound must argue
    it here (or in a follow-up ADR) rather than have it land unreviewed.
    """
    req = _requirement_named(_runtime_dependencies(), name)
    assert str(req.specifier) == "", (
        f"{name} now carries a version constraint ({req.specifier!r}) this policy did not "
        "anticipate. If there is a real, evidenced reason (a documented incident, or a "
        "confirmed load-bearing semantic — see the psutil/pydgraph tests above for the "
        "pattern this repository expects), update this test's parametrize list and its "
        "docstring to record the reasoning. Do not let an unreasoned pin land silently."
    )


# ---------------------------------------------------------------------------
# ruff — regression guard for the incident that started this policy
# ---------------------------------------------------------------------------


def test_ruff_dev_dependency_stays_exactly_pinned() -> None:
    """Given ruff is pinned because an unpinned lint gate once resolved
    0.16.0, which promoted PLR0917 out of preview and broke CI on code that
    had been on main, unchanged and green, for months (pyproject.toml's own
    comment on this line).
    When [project.optional-dependencies.dev]'s ruff entry is read.
    Then it carries EXACTLY ONE specifier and that specifier's operator is
    "==" — never a range, never bare. A range would let the exact same
    incident recur; this is the regression guard for it.
    """
    req = _requirement_named(_dev_dependencies(), "ruff")
    specifiers = list(req.specifier)
    assert len(specifiers) == 1, (
        f"ruff must carry exactly one version specifier; got {req.specifier!r}"
    )
    assert specifiers[0].operator == "==", (
        f"ruff must be pinned with '==', never a range; got {req.specifier!r}"
    )
