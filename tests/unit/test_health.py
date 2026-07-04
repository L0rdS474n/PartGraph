"""
Tests: AC-1..AC-9 — partgraph.util.health (leaf module) + `partgraph db status`
health-probe rewrite (ADR-0018).

Specifies the behaviour of the NEW leaf module ``partgraph.util.health``,
which supersedes `db status`'s current `compose ps` delegation (cli.py:
236-239) with an HTTP `/health` probe against Dgraph's own Alpha HTTP
endpoint — so the reported state reflects the DATABASE's true running/healthy
state, independent of how the container was started (compose, systemd timer,
or a bare `podman run` / `docker run`). This is the CORE FIX (AC-2): `db
status` must never depend on `compose_command()` / the container engine at
all.

Pinned contract (leaf module ``partgraph.util.health`` — NOT YET
IMPLEMENTED; collection of THIS file is expected to ERROR with
ModuleNotFoundError until it exists, and that is the correct test-first red
state):
  - ``DGRAPH_HTTP_HEALTH_URL: str`` = "http://127.0.0.1:8081/health" — the
    SINGLE source of truth for the Dgraph HTTP health endpoint (mirrors the
    literal already hard-coded in tests/conftest.py, which is updated in
    lockstep — AC-8 — to import this constant instead of re-declaring it, and
    matches the documented endpoint/response shape in docs/connecting.md).
  - ``HEALTH_PROBE_TIMEOUT_S: float`` = 2.0 — a finite, named, bounded
    timeout (mirrors the repo's existing bounded-constant convention, e.g.
    ``_GRPC_MAX_MESSAGE_BYTES`` in cli.py / ADR-0007's DoS-bounds precedent).
  - ``@dataclass(frozen=True) class HealthResult`` with EXACTLY six fields:
    ``reachable: bool``, ``healthy: bool``, ``http_status: int | None``,
    ``status: str | None``, ``version: str | None``, ``message: str``.
  - ``def probe_health(*, url=DGRAPH_HTTP_HEALTH_URL,
    timeout=HEALTH_PROBE_TIMEOUT_S, http_get=None) -> HealthResult`` —
    ``http_get`` is the INJECTABLE SEAM (mirrors ``partgraph.ingest.fetch``'s
    and ``partgraph.refresh.links``'s injectable-http-client discipline;
    defaults to a LAZILY-imported ``requests.get`` so this leaf never imports
    ``requests`` eagerly just by being imported). It is invoked EXACTLY as
    ``http_get(url, timeout=timeout)`` and must return an object exposing
    ``.status_code`` and ``.json()``.
  - ``db status`` (cli.py:236-239) is rewritten to call ``probe_health()``,
    print ``result.message``, then ``raise typer.Exit(code=0 if
    result.healthy else 1)`` — it no longer calls ``_run_compose`` /
    ``compose_command`` / ``subprocess.run`` at all (AC-2).

This file mirrors the hermetic, injected-seam style of test_container.py
(fake ``which``) for the leaf-level tests, and the ``CliRunner()`` /
``_invoke()`` + ``stub_compose_command``-style patching from test_cli.py for
the CLI-level `db status` tests (patching ``partgraph.cli.probe_health`` at
the SAME namespace ``compose_command`` is already patched at). No test in
this file opens a real socket, sleeps, or reads the real wall clock: every
HTTP outcome is injected via a fake/spy ``http_get`` callable, and every
CLI-level test patches ``partgraph.cli.probe_health`` / ``partgraph.cli.
compose_command`` directly rather than exercising a real network call or a
real container engine.

Regression pins living in tests/unit/test_cli.py (UNCHANGED by this file):
  - test_cli.py:118/134 — `db status --help` continues to exit 0 and be
    English, over the SAME ["up", "down", "status", "apply-schema"]
    parametrization (AC-9). This file's own `db status --help` test (below)
    is a redundant, self-contained anchor for the same guarantee.
  - test_cli.py:162/245 — the `subprocess.run` argv / absolute-path
    parametrizations are narrowed from ["up", "down", "status"] to
    ["up", "down"]: `status` no longer delegates to Compose at all (ADR-0018).
"""

from __future__ import annotations

import dataclasses
import inspect
import math
import subprocess
import sys
from unittest.mock import patch

import pytest
import requests
from typer.testing import CliRunner

from partgraph.cli import app
from partgraph.util.container import ContainerEngineError
from partgraph.util.health import (
    DGRAPH_HTTP_HEALTH_URL,
    HEALTH_PROBE_TIMEOUT_S,
    HealthResult,
    probe_health,
)

RUNNER = CliRunner()


def _invoke(args: list[str]):
    """Invoke the CLI app with the given args and return the result."""
    return RUNNER.invoke(app, args)


# ---------------------------------------------------------------------------
# Fakes — an injectable `http_get` seam that never opens a real socket.
# Modeled on tests/unit/test_refresh_links.py's _FakeResponse/_FakeHttpClient.
# ---------------------------------------------------------------------------

class _FakeHealthResponse:
    """Minimal fake HTTP response exposing ``.status_code`` and ``.json()``.

    ``payload`` is returned verbatim by ``.json()``; when ``json_error`` is
    given, ``.json()`` raises it instead (models a malformed/non-JSON body,
    AC-6).
    """

    def __init__(
        self,
        status_code: int,
        payload: object = None,
        *,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakeHttpGet:
    """Injectable ``http_get`` seam that never opens a real socket.

    Scriptable per instance: returns a fixed ``_FakeHealthResponse`` (success)
    or raises a fixed ``Exception`` (network failure) on every call. Records
    every call's ``(url, kwargs)`` so tests can assert exactly what
    ``probe_health`` forwarded — e.g. the ``timeout=`` kwarg (AC-7).
    """

    def __init__(self, result: _FakeHealthResponse | Exception) -> None:
        self._result = result
        self.calls: list[dict] = []

    def __call__(self, url: str, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


# ---------------------------------------------------------------------------
# CONTRACT — module constants, HealthResult shape, probe_health signature
# ---------------------------------------------------------------------------

def test_dgraph_http_health_url_constant_matches_documented_endpoint() -> None:
    """AC-8 (no drift): Given DGRAPH_HTTP_HEALTH_URL is the single source of
    truth for the Dgraph HTTP health endpoint.
    When the constant is read directly.
    Then it equals "http://127.0.0.1:8081/health" — the exact literal
    documented in docs/connecting.md and previously re-declared (now removed)
    in tests/conftest.py.
    """
    assert DGRAPH_HTTP_HEALTH_URL == "http://127.0.0.1:8081/health"


def test_health_probe_timeout_constant_is_a_finite_bounded_float() -> None:
    """AC-7: Given HEALTH_PROBE_TIMEOUT_S is the module's named request
    timeout.
    When the constant is read directly.
    Then it is a finite float strictly greater than zero — a bounded
    constant, never an unbounded/None timeout (mirrors
    _GRPC_MAX_MESSAGE_BYTES in cli.py / ADR-0007's bounded-constant
    precedent).
    """
    assert isinstance(HEALTH_PROBE_TIMEOUT_S, float)
    assert math.isfinite(HEALTH_PROBE_TIMEOUT_S)
    assert HEALTH_PROBE_TIMEOUT_S > 0


def test_health_result_dataclass_has_exact_contract_fields_and_is_frozen() -> None:
    """CONTRACT: Given HealthResult is the DTO returned by probe_health().
    When it is instantiated.
    Then it exposes EXACTLY the six pinned fields (reachable, healthy,
    http_status, status, version, message) and is frozen — a consumer (e.g.
    cli.py's `status()` command) cannot mutate a probe result after the fact.
    """
    result = HealthResult(
        reachable=True,
        healthy=True,
        http_status=200,
        status="healthy",
        version="v25.3.4",
        message="ok",
    )
    field_names = {f.name for f in dataclasses.fields(result)}
    assert field_names == {
        "reachable",
        "healthy",
        "http_status",
        "status",
        "version",
        "message",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.healthy = False  # type: ignore[misc]


def test_probe_health_parameters_are_keyword_only() -> None:
    """CONTRACT: Given probe_health's parameters are declared keyword-only
    (`*, url=..., timeout=..., http_get=...`).
    When the signature is inspected.
    Then every one of url/timeout/http_get is KEYWORD_ONLY, so a future
    refactor cannot silently reorder them into positional arguments.
    """
    sig = inspect.signature(probe_health)
    for name in ("url", "timeout", "http_get"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"probe_health's {name!r} parameter must be keyword-only per contract."
        )


def test_probe_health_signature_defaults_match_module_constants() -> None:
    """CONTRACT: Given probe_health's `url`/`timeout` keyword defaults.
    When the signature is inspected (never calling the function with no
    injected http_get, which would open a real socket).
    Then they equal DGRAPH_HTTP_HEALTH_URL / HEALTH_PROBE_TIMEOUT_S exactly.
    """
    sig = inspect.signature(probe_health)
    assert sig.parameters["url"].default == DGRAPH_HTTP_HEALTH_URL
    assert sig.parameters["timeout"].default == HEALTH_PROBE_TIMEOUT_S


def test_probe_health_http_get_defaults_to_none_for_lazy_requests_import() -> None:
    """CONTRACT: Given http_get defaults to None so the real `requests`
    module is resolved lazily only when a probe actually runs (never eagerly
    at import time, and never when a test injects its own http_get).
    When the signature is inspected.
    Then http_get's default is exactly None.
    """
    sig = inspect.signature(probe_health)
    assert sig.parameters["http_get"].default is None


def test_probe_health_returns_a_health_result_instance() -> None:
    """CONTRACT: Given probe_health() completes (success or failure path).
    When it returns.
    Then the return value is always a HealthResult instance — never None, a
    dict, or a bare tuple — the one stable return type `db status` consumes.
    """
    fake_get = _FakeHttpGet(_FakeHealthResponse(200, [{"status": "healthy"}]))
    result = probe_health(http_get=fake_get)
    assert isinstance(result, HealthResult)


def test_probe_health_default_url_is_the_single_source_of_truth_constant() -> None:
    """CONTRACT: Given probe_health() is called without an explicit `url=`.
    When the injected http_get spy records the call.
    Then it was called against DGRAPH_HTTP_HEALTH_URL exactly — no
    independently hard-coded URL string anywhere in this leaf.
    """
    fake_get = _FakeHttpGet(_FakeHealthResponse(200, [{"status": "healthy"}]))
    probe_health(http_get=fake_get)
    assert fake_get.calls[0]["url"] == DGRAPH_HTTP_HEALTH_URL


def test_probe_health_calls_seam_with_url_positional_and_timeout_keyword() -> None:
    """CONTRACT: Given the injectable http_get seam.
    When probe_health() invokes it.
    Then it is called as `http_get(url, timeout=timeout)` — url positional,
    timeout as a keyword — exactly the calling convention real
    `requests.get` supports, so the `http_get=None -> requests.get` default
    swap is safe.
    """
    fake_get = _FakeHttpGet(_FakeHealthResponse(200, [{"status": "healthy"}]))
    probe_health(url="http://127.0.0.1:8081/health", http_get=fake_get)
    assert len(fake_get.calls) == 1
    call = fake_get.calls[0]
    assert call["url"] == "http://127.0.0.1:8081/health"
    assert "timeout" in call["kwargs"]


# ---------------------------------------------------------------------------
# AC-1 — healthy: HTTP 200 + a well-formed healthy body
# ---------------------------------------------------------------------------

def test_probe_health_ac1_200_healthy_body_returns_healthy_true() -> None:
    """AC-1: Given the injected http_get returns HTTP 200 with the documented
    Dgraph /health body shape (docs/connecting.md):
    `[{"instance":"alpha","status":"healthy","version":"v25.3.4","uptime":59070}]`.
    When probe_health() is called.
    Then HealthResult.reachable and .healthy are both True, http_status is
    200, status is "healthy" and version is "v25.3.4".
    """
    payload = [
        {
            "instance": "alpha",
            "status": "healthy",
            "version": "v25.3.4",
            "uptime": 59070,
        }
    ]
    fake_get = _FakeHttpGet(_FakeHealthResponse(200, payload))

    result = probe_health(http_get=fake_get)

    assert result.reachable is True
    assert result.healthy is True
    assert result.http_status == 200
    assert result.status == "healthy"
    assert result.version == "v25.3.4"
    assert isinstance(result.message, str) and result.message


def test_db_status_ac1_prints_healthy_line_and_exits_zero() -> None:
    """AC-1: Given probe_health() reports a healthy Dgraph instance.
    When we invoke `partgraph db status` with the health seam stubbed at the
    partgraph.cli namespace (mirroring how stub_compose_command patches
    compose_command in test_cli.py).
    Then the command prints a line containing "healthy" and exits 0.
    """
    healthy_result = HealthResult(
        reachable=True,
        healthy=True,
        http_status=200,
        status="healthy",
        version="v25.3.4",
        message="Dgraph is healthy (v25.3.4).",
    )
    with patch(
        "partgraph.cli.probe_health", return_value=healthy_result
    ) as mock_probe:
        result = _invoke(["db", "status"])

    assert result.exit_code == 0, (
        f"`db status` should exit 0 when healthy, got {result.exit_code}.\n"
        f"Output:\n{result.output!r}"
    )
    assert "healthy" in result.output, (
        f"`db status` output should mention 'healthy'. Got:\n{result.output!r}"
    )
    # HARDENING: `db status` must call probe_health() with ZERO arguments — it
    # never threads a url/timeout/http_get override. This locks the loopback-only,
    # no-override contract (the default DGRAPH_HTTP_HEALTH_URL / bounded timeout /
    # lazy requests.get are always used).
    mock_probe.assert_called_once_with()


# ---------------------------------------------------------------------------
# AC-2 — CORE FIX: `db status` is engine-independent
# ---------------------------------------------------------------------------

def test_db_status_ac2_exits_zero_without_engine_when_probe_is_healthy() -> None:
    """AC-2 (CORE FIX): Given no container engine is on PATH — compose_command
    raises ContainerEngineError, exactly as test_cli.py's existing
    test_db_up_exits_cleanly_when_no_engine_available patches it — but the
    Dgraph HTTP health probe reports healthy.
    When we invoke `partgraph db status`.
    Then the command exits 0 AND subprocess.run is NEVER called: `db status`
    must be engine-independent, no longer delegating to `compose ps`
    (ADR-0018) — health is derived solely from the HTTP probe.
    """
    healthy_result = HealthResult(
        reachable=True,
        healthy=True,
        http_status=200,
        status="healthy",
        version="v25.3.4",
        message="Dgraph is healthy (v25.3.4).",
    )
    with (
        patch(
            "partgraph.cli.compose_command",
            side_effect=ContainerEngineError("no engine"),
        ),
        patch("partgraph.cli.probe_health", return_value=healthy_result),
        patch("subprocess.run") as mock_run,
    ):
        result = _invoke(["db", "status"])

    assert result.exit_code == 0, (
        "`db status` must exit 0 from the health probe alone, independent of "
        f"the container engine. Got {result.exit_code}.\nOutput:\n{result.output!r}"
    )
    assert not mock_run.called, (
        "`db status` called subprocess.run even though no container engine is "
        "on PATH. It must be engine-independent (HTTP health-probe only, ADR-0018)."
    )


# ---------------------------------------------------------------------------
# AC-3 — unreachable: the injected http_get raises a connection error
# ---------------------------------------------------------------------------

def test_probe_health_ac3_connection_error_is_unreachable_and_unhealthy() -> None:
    """AC-3: Given the injected http_get raises requests.ConnectionError
    (Dgraph is not running / port 8081 is not listening).
    When probe_health() is called.
    Then HealthResult.reachable and .healthy are both False, and the message
    is a fixed, path-free string suggesting `partgraph db up` that contains
    NEITHER the raw exception text NOR any "/" character.
    """
    exc = requests.ConnectionError(
        "HTTPConnectionPool(host='127.0.0.1', port=8081): "
        "Max retries exceeded (Caused by NewConnectionError(...))"
    )
    fake_get = _FakeHttpGet(exc)

    result = probe_health(http_get=fake_get)

    assert result.reachable is False
    assert result.healthy is False
    assert "partgraph db up" in result.message
    assert str(exc) not in result.message, (
        f"HealthResult.message must never leak the raw exception text. Got: "
        f"{result.message!r}"
    )
    assert "/" not in result.message, (
        f"HealthResult.message must be path-free (no '/'). Got: {result.message!r}"
    )


def test_db_status_ac3_exits_one_with_no_traceback_on_unreachable() -> None:
    """AC-3: Given probe_health() reports the database unreachable.
    When we invoke `partgraph db status`.
    Then the command exits 1, prints the fixed path-free message (mentioning
    `partgraph db up`), and no raw traceback/exception ever reaches the
    user's terminal.
    """
    unreachable = HealthResult(
        reachable=False,
        healthy=False,
        http_status=None,
        status=None,
        version=None,
        message="Dgraph is not reachable. Start it with partgraph db up.",
    )
    with patch("partgraph.cli.probe_health", return_value=unreachable):
        result = _invoke(["db", "status"])

    assert result.exit_code == 1, (
        f"`db status` should exit 1 when unreachable, got {result.exit_code}."
    )
    assert "partgraph db up" in result.output
    assert "Traceback" not in result.output
    if result.exception is not None:
        assert isinstance(result.exception, SystemExit), (
            "An unhandled exception leaked to the CLI surface instead of a "
            f"clean typer.Exit. Got: {result.exception!r}"
        )


# ---------------------------------------------------------------------------
# AC-4 — timeout: the injected http_get raises requests.exceptions.Timeout
# ---------------------------------------------------------------------------

def test_probe_health_ac4_timeout_is_unreachable_and_unhealthy() -> None:
    """AC-4: Given the injected http_get raises requests.exceptions.Timeout.
    When probe_health() is called.
    Then HealthResult.reachable and .healthy are both False, and the message
    contains neither the raw exception text nor any "/" character.
    """
    exc = requests.exceptions.Timeout("Read timed out. (read timeout=2.0)")
    fake_get = _FakeHttpGet(exc)

    result = probe_health(http_get=fake_get)

    assert result.reachable is False
    assert result.healthy is False
    assert str(exc) not in result.message
    assert "/" not in result.message


def test_probe_health_ac4_timeout_message_is_dedicated_not_generic() -> None:
    """AC-4: Given a requests.exceptions.Timeout vs a generic
    requests.ConnectionError.
    When probe_health() is called once for each (hermetically, via two
    independently injected fakes).
    Then the two produce DIFFERENT messages: the timeout case has its own
    dedicated wording, not the generic "not reachable" message (AC-3), and it
    names the concept of a timeout.
    """
    timeout_result = probe_health(
        http_get=_FakeHttpGet(requests.exceptions.Timeout("t"))
    )
    conn_result = probe_health(http_get=_FakeHttpGet(requests.ConnectionError("c")))

    assert timeout_result.message != conn_result.message, (
        "The timeout message must be dedicated, not the generic unreachable "
        f"message. Both were: {timeout_result.message!r}"
    )
    assert (
        "timeout" in timeout_result.message.lower()
        or "timed out" in timeout_result.message.lower()
    )


def test_db_status_ac4_exits_one_on_timeout() -> None:
    """AC-4: Given probe_health() reports a timeout.
    When we invoke `partgraph db status`.
    Then the command exits 1 and the printed message is forwarded verbatim
    (mentions timeout).
    """
    timeout_result = HealthResult(
        reachable=False,
        healthy=False,
        http_status=None,
        status=None,
        version=None,
        message="Dgraph health probe timed out after 2.0s.",
    )
    with patch("partgraph.cli.probe_health", return_value=timeout_result):
        result = _invoke(["db", "status"])

    assert result.exit_code == 1
    assert "timed out" in result.output.lower() or "timeout" in result.output.lower()


# ---------------------------------------------------------------------------
# AC-5 — non-200: the injected http_get returns HTTP 503
# ---------------------------------------------------------------------------

def test_probe_health_ac5_non_200_status_is_reachable_but_unhealthy() -> None:
    """AC-5: Given the injected http_get returns HTTP 503 (Service
    Unavailable — the Dgraph process is up but not ready).
    When probe_health() is called.
    Then HealthResult.reachable is True (the socket answered), .healthy is
    False, http_status is 503, and the message names the integer 503 without
    leaking any path or exception text.
    """
    fake_get = _FakeHttpGet(_FakeHealthResponse(503, [{"status": "not ready"}]))

    result = probe_health(http_get=fake_get)

    assert result.reachable is True
    assert result.healthy is False
    assert result.http_status == 503
    assert "503" in result.message
    assert "/" not in result.message
    # HARDENING: the non-200 message names only the code — it must NEVER echo the
    # response body. "not ready" is the distinguishing marker in this fake's
    # payload; its absence proves the body was not read/leaked into the message.
    assert "not ready" not in result.message, (
        "AC-5: the message must name only the status code, never echo the "
        f"response body. Got: {result.message!r}"
    )


def test_db_status_ac5_exits_one_on_non_200_and_names_the_status_code() -> None:
    """AC-5: Given probe_health() reports HTTP 503 (reachable, not healthy).
    When we invoke `partgraph db status`.
    Then the command exits 1 and the printed message names 503.
    """
    result_503 = HealthResult(
        reachable=True,
        healthy=False,
        http_status=503,
        status=None,
        version=None,
        message="Dgraph responded with HTTP 503 (not healthy).",
    )
    with patch("partgraph.cli.probe_health", return_value=result_503):
        result = _invoke(["db", "status"])

    assert result.exit_code == 1
    assert "503" in result.output


# ---------------------------------------------------------------------------
# AC-6 — HTTP 200 gate: malformed/empty/non-list body is still "healthy"
# ---------------------------------------------------------------------------

_AC6_EMPTY_LIST = _FakeHealthResponse(200, [])
_AC6_EMPTY_DICT_NON_LIST = _FakeHealthResponse(200, {})
_AC6_JSON_VALUE_ERROR = _FakeHealthResponse(
    200, None, json_error=ValueError("Expecting value: line 1 column 1 (char 0)")
)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(_AC6_EMPTY_LIST, id="empty_list"),
        pytest.param(_AC6_EMPTY_DICT_NON_LIST, id="empty_dict_non_list"),
        pytest.param(_AC6_JSON_VALUE_ERROR, id="json_raises_value_error"),
    ],
)
def test_probe_health_ac6_200_with_unrecognized_body_is_still_healthy(response) -> None:
    """AC-6: Given HTTP 200 but a malformed/empty/non-list JSON body (an
    empty list, an empty dict, or .json() raising ValueError).
    When probe_health() is called.
    Then HealthResult.healthy is True regardless — HTTP 200 alone is the
    health gate — status and version are None (nothing recognizable was
    parsed), and the message notes the payload was not recognized.
    """
    fake_get = _FakeHttpGet(response)

    result = probe_health(http_get=fake_get)

    assert result.reachable is True
    assert result.healthy is True
    assert result.http_status == 200
    assert result.status is None
    assert result.version is None
    message_lower = result.message.lower()
    assert any(
        keyword in message_lower
        for keyword in ("unrecognized", "unexpected", "unknown", "malformed")
    ), f"AC-6: message should note an unrecognized payload. Got: {result.message!r}"
    # HARDENING: the "unrecognized payload" message must stay path-free (no '/')
    # AND must never leak the raw .json() ValueError text ("Expecting value ..."
    # is the distinguishing marker of the json_raises_value_error case; trivially
    # absent for the empty-list/empty-dict cases).
    assert "/" not in result.message, (
        f"AC-6: message must be path-free (no '/'). Got: {result.message!r}"
    )
    assert "Expecting value" not in result.message, (
        "AC-6: the raw .json() ValueError text must never leak into the "
        f"message. Got: {result.message!r}"
    )


def test_db_status_ac6_exits_zero_on_200_with_unrecognized_payload() -> None:
    """AC-6: Given probe_health() reports HTTP 200 with status/version both
    None (an unrecognized payload) but healthy still True.
    When we invoke `partgraph db status`.
    Then the command still exits 0 — the exit code is driven solely by
    HealthResult.healthy, never by status/version.
    """
    unrecognized = HealthResult(
        reachable=True,
        healthy=True,
        http_status=200,
        status=None,
        version=None,
        message="Dgraph is healthy (HTTP 200); response body not recognized.",
    )
    with patch("partgraph.cli.probe_health", return_value=unrecognized):
        result = _invoke(["db", "status"])

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# AC-7 — bounded timeout is forwarded to the injected seam
# (test_health_probe_timeout_constant_is_a_finite_bounded_float above also
# covers AC-7's "HEALTH_PROBE_TIMEOUT_S is a finite float" half.)
# ---------------------------------------------------------------------------

def test_probe_health_ac7_forwards_default_timeout_kwarg_to_http_get() -> None:
    """AC-7: Given probe_health() is called with its default timeout (no
    explicit `timeout=` override).
    When the injected http_get spy records its call.
    Then HEALTH_PROBE_TIMEOUT_S was forwarded as the exact `timeout=` kwarg.
    """
    fake_get = _FakeHttpGet(_FakeHealthResponse(200, [{"status": "healthy"}]))

    probe_health(http_get=fake_get)

    assert len(fake_get.calls) == 1
    assert fake_get.calls[0]["kwargs"].get("timeout") == HEALTH_PROBE_TIMEOUT_S


def test_probe_health_ac7_forwards_a_custom_timeout_override() -> None:
    """AC-7: Given probe_health() is called with an explicit, non-default
    timeout value.
    When the injected http_get spy records its call.
    Then that exact custom value — not the module default — is forwarded as
    the `timeout=` kwarg.
    """
    fake_get = _FakeHttpGet(_FakeHealthResponse(200, [{"status": "healthy"}]))

    probe_health(http_get=fake_get, timeout=9.5)

    assert fake_get.calls[0]["kwargs"].get("timeout") == 9.5


# ---------------------------------------------------------------------------
# Robustness / leaf discipline — no blind swallowing; no eager requests import
# ---------------------------------------------------------------------------

def test_probe_health_unexpected_error_propagates_and_is_not_swallowed() -> None:
    """ROBUSTNESS: Given the injected http_get raises an exception that is NOT a
    requests.exceptions.RequestException (a RuntimeError here — modelling a
    programming error in the seam, never a network condition).
    When probe_health() is called.
    Then that exception PROPAGATES unchanged — it is never coerced into a generic
    unreachable/unhealthy HealthResult. probe_health catches ONLY the specific
    requests timeout/connection families (no blind `except Exception`, ruff BLE);
    anything else must surface so a real bug is never silently masked as
    "database down".
    """
    fake_get = _FakeHttpGet(RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        probe_health(http_get=fake_get)


def test_importing_health_module_does_not_eagerly_import_requests() -> None:
    """ARCH/SEC (leaf discipline): Given partgraph.util.health declares
    `requests` as a LAZY import inside probe_health only (never at module top
    level).
    When the module is imported in a FRESH interpreter — subprocess-isolated, so
    this file's own eager `import requests` (and conftest's) cannot mask the
    check.
    Then `requests` is ABSENT from sys.modules: importing the package pulls in no
    third-party HTTP dependency; the requests import is paid for only when a probe
    actually runs. Deterministic and network-free.
    """
    probe_source = (
        "import sys\n"
        "import partgraph.util.health\n"
        "assert 'requests' not in sys.modules, "
        "'requests was imported eagerly by partgraph.util.health'\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe_source],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, (
        "Importing partgraph.util.health must NOT eagerly import `requests`.\n"
        f"returncode={completed.returncode}\n"
        f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
    )


# ---------------------------------------------------------------------------
# AC-9 — `db status --help` is unchanged (redundant anchor; the authoritative
# regression pin remains tests/unit/test_cli.py:118/134, left UNCHANGED)
# ---------------------------------------------------------------------------

def test_db_status_ac9_help_still_exits_zero_and_is_english() -> None:
    """AC-9: Given `db status` is rewritten to call probe_health() instead of
    delegating to `compose ps`.
    When we invoke `partgraph db status --help`.
    Then the exit code is still 0 and the output is still English (contains
    "sage", matching Typer's auto-generated Usage/usage banner) — rewriting
    the command BODY must never affect its --help text. `--help`
    short-circuits before the command body runs, so this test needs no
    health-seam patch.
    """
    result = _invoke(["db", "status", "--help"])
    assert result.exit_code == 0
    assert "sage" in result.output
