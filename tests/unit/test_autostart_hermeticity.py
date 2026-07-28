"""
Tests: PR-B2 (feat/db-lazy-autostart) — AC B-10, the suite-wide autostart
safety net.

PR-B2 makes autostart ON by default for every DB-touching command. Without a
hard, suite-wide backstop, a SINGLE unpatched test anywhere in this large
suite — one that invokes a DB-touching command without patching
`subprocess.run`/`partgraph.cli.ensure_running` — could start a REAL
container the moment `pytest` runs it, on whatever host happens to be
running the test suite. AC B-10 asks for exactly that backstop: an autouse
fixture in `tests/conftest.py` forcing `PARTGRAPH_AUTOSTART=0` for every
test by default, PLUS a meta-test (this file) asserting that fixture exists
and is genuinely autouse — not merely present but forgotten to be wired in.

This file's own assertions are GREEN already: the autouse fixture
(`_partgraph_autostart_disabled_by_default`) was added to `tests/conftest.py`
in the SAME change that adds this meta-test, as pure test-suite
infrastructure (never production `src/` code) — unlike the rest of PR-B2
(`ensure_running`, the CLI wiring, `docker/docker-compose.yml`'s `restart:
"no"`), which remains RED until the corresponding `src/`/`docker/` changes
land. This is a DELIBERATE exception to "tests first, then make them pass":
the safety net has to exist BEFORE any autostart-ON test is written, not
after, or the RED phase itself could start a real container.

Behavioural proof that the fixture actually WORKS (not just that it exists)
lives in `tests/unit/test_cli_autostart.py`'s own B-1/B-2/B-3/B-6 tests,
every one of which has to explicitly call `monkeypatch.setenv(
"PARTGRAPH_AUTOSTART", "1")` to reach the autostart-ON path at all — proving
the suite-wide default really is OFF.
"""

from __future__ import annotations

import ast
import pathlib

CONFTEST_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "conftest.py"
)


def _parse_conftest() -> ast.Module:
    source = CONFTEST_PATH.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(CONFTEST_PATH))


def _is_autouse_fixture_decorator(decorator: ast.expr) -> bool:
    """Return True iff *decorator* is `pytest.fixture(..., autouse=True, ...)`."""
    if not isinstance(decorator, ast.Call):
        return False
    func = decorator.func
    is_fixture_call = (
        isinstance(func, ast.Attribute) and func.attr == "fixture"
    ) or (isinstance(func, ast.Name) and func.id == "fixture")
    if not is_fixture_call:
        return False
    return any(
        kw.arg == "autouse"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in decorator.keywords
    )


def _sets_partgraph_autostart_to_zero(func: ast.FunctionDef) -> bool:
    """Return True iff *func*'s body calls `<name>.setenv("PARTGRAPH_AUTOSTART",
    "0")` (any receiver name — the fixture's own monkeypatch parameter name is
    an implementation detail, not part of the contract this test pins).
    """
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "setenv":
            continue
        args = node.args
        if len(args) < 2:
            continue
        name_arg, value_arg = args[0], args[1]
        if not (isinstance(name_arg, ast.Constant) and name_arg.value == "PARTGRAPH_AUTOSTART"):
            continue
        if isinstance(value_arg, ast.Constant) and value_arg.value == "0":
            return True
    return False


def _find_autouse_fixtures(tree: ast.Module) -> list[ast.FunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(_is_autouse_fixture_decorator(dec) for dec in node.decorator_list)
    ]


def test_conftest_declares_at_least_one_autouse_fixture() -> None:
    """AC B-10: Given tests/conftest.py is the shared fixture module for the
    whole suite.
    When its source is parsed.
    Then it declares at least one function decorated with
    `@pytest.fixture(autouse=True, ...)` — the suite-wide safety net cannot
    exist as an opt-in-only fixture; it must apply to every test by
    default.
    """
    tree = _parse_conftest()
    autouse_fixtures = _find_autouse_fixtures(tree)
    assert autouse_fixtures, (
        "tests/conftest.py declares no autouse fixture at all — AC B-10's "
        "suite-wide PARTGRAPH_AUTOSTART=0 safety net is missing."
    )


def test_an_autouse_fixture_forces_partgraph_autostart_off() -> None:
    """AC B-10: Given tests/conftest.py's autouse fixture(s).
    When each is inspected for its own body.
    Then AT LEAST ONE of them calls `monkeypatch.setenv("PARTGRAPH_AUTOSTART",
    "0")` — the specific safety net AC B-10 asks for, not merely SOME
    autouse fixture that happens to exist for an unrelated reason.
    """
    tree = _parse_conftest()
    autouse_fixtures = _find_autouse_fixtures(tree)
    assert autouse_fixtures, (
        "tests/conftest.py declares no autouse fixture at all (see the "
        "sibling test) — cannot check its body."
    )
    matching = [f for f in autouse_fixtures if _sets_partgraph_autostart_to_zero(f)]
    assert matching, (
        "no autouse fixture in tests/conftest.py sets "
        'PARTGRAPH_AUTOSTART="0" via monkeypatch.setenv(...) — the suite-wide '
        "autostart safety net (AC B-10) is missing or does not force the "
        "documented off-value."
    )


def test_conftest_module_actually_imports_pytest() -> None:
    """Given the autouse-fixture detection above relies on recognising
    `@pytest.fixture(...)` decorators.
    When tests/conftest.py's source is parsed.
    Then it genuinely imports `pytest` — a sanity check so this file's own
    scanner cannot pass vacuously against a conftest.py that does not use
    pytest fixtures at all.
    """
    tree = _parse_conftest()
    imports_pytest = any(
        (isinstance(node, ast.Import) and any(alias.name == "pytest" for alias in node.names))
        for node in ast.walk(tree)
    )
    assert imports_pytest, "tests/conftest.py does not import pytest at all."
