"""HTTP ``/health`` liveness probe for the local Dgraph instance.

This is a **leaf** module: its top-level imports are the Python standard library
only (:mod:`dataclasses`, :mod:`collections.abc`, :mod:`typing`). It imports
:mod:`requests` **lazily**, inside :func:`probe_health`, never at module import
time — so importing :mod:`partgraph.util` (which re-exports the container and
resource leaves) pulls in no third-party HTTP dependency, and a consumer that
only needs the constants or the :class:`HealthResult` DTO never pays for the
``requests`` import. It must never import :mod:`partgraph.cli` or any of the
embed/query/load layers, so both the CLI and the integration tests can use it
without an import cycle (mirrors :mod:`partgraph.util.container`).

Why this exists
---------------
``partgraph db status`` used to delegate to ``<engine> compose ps``. That only
sees containers Compose itself labelled: a Dgraph started by a systemd timer or
a bare ``podman run`` / ``docker run`` is invisible to it, so the command
printed an empty table and exited ``0`` even while the database was actually up
(or actually down). This probe instead asks Dgraph's OWN Alpha HTTP ``/health``
endpoint, so the reported state reflects the DATABASE's true liveness —
independent of how, or whether, a container engine started it, and needing no
container engine at all (ADR-0018).

Contract
--------
- :data:`DGRAPH_HTTP_HEALTH_URL` is the single source of truth for the endpoint.
- :data:`HEALTH_PROBE_TIMEOUT_S` is a finite, bounded request timeout (never an
  unbounded/``None`` timeout), extending ADR-0007's bounded-constant precedent
  to this outbound call.
- :func:`probe_health` returns a frozen :class:`HealthResult` on every handled
  outcome, and never leaks a raw exception string, a response body, or a
  filesystem path into :attr:`HealthResult.message` (the message is always safe
  to print verbatim).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DGRAPH_HTTP_HEALTH_URL",
    "HEALTH_PROBE_TIMEOUT_S",
    "HealthResult",
    "probe_health",
]

#: Single source of truth for Dgraph's Alpha HTTP ``/health`` endpoint (host
#: port 8081 -> container 8080; loopback only). Imported (never re-declared) by
#: tests/conftest.py and tests/integration/test_dgraph_lifecycle.py, and matches
#: the endpoint documented in docs/connecting.md.
DGRAPH_HTTP_HEALTH_URL = "http://127.0.0.1:8081/health"

#: Finite, named, bounded per-request timeout (seconds) for the health probe.
#: Deliberately a small positive float, never ``None`` (an unbounded wait):
#: `db status` must return promptly whether Dgraph is up, down, or wedged. This
#: extends ADR-0007's bounded-constant precedent to the outbound health call.
HEALTH_PROBE_TIMEOUT_S = 2.0

#: HTTP status that gates liveness: a 200 means the Alpha is serving requests.
#: Named so the comparison below stays magic-value-free (ruff PLR2004).
_HTTP_OK = 200

#: Fixed, path-free message for an unreachable database (connection refused /
#: DNS / reset — anything but a timeout). Names the remedy without echoing the
#: raw exception, the URL, or any '/'-bearing path, so nothing internal leaks.
_UNREACHABLE_MESSAGE = "Dgraph is not reachable. Start it with partgraph db up."


@dataclass(frozen=True)
class HealthResult:
    """Immutable outcome of a single Dgraph ``/health`` probe.

    Attributes:
        reachable: True iff the HTTP socket answered at all (any status code);
            False on a connection failure or a timeout.
        healthy: True iff Dgraph is live. HTTP 200 is the sole liveness gate — a
            200 with an unparseable body is still healthy; any non-200, a
            timeout, or a connection failure is not.
        http_status: The integer HTTP status code, or None when no response was
            received (timeout / connection failure).
        status: The ``status`` field parsed from the health body (e.g.
            "healthy"), or None when the body was absent or unrecognized.
        version: The ``version`` field parsed from the health body (e.g.
            "v25.3.4"), or None when the body was absent or unrecognized.
        message: A human-readable, path-free, single-line summary safe to print
            verbatim. Never contains a raw exception string or a response body.
    """

    reachable: bool
    healthy: bool
    http_status: int | None
    status: str | None
    version: str | None
    message: str


def probe_health(
    *,
    url: str = DGRAPH_HTTP_HEALTH_URL,
    timeout: float = HEALTH_PROBE_TIMEOUT_S,
    http_get: Callable[..., Any] | None = None,
) -> HealthResult:
    """Probe Dgraph's HTTP ``/health`` endpoint and classify the outcome.

    All parameters are keyword-only, so a future refactor cannot silently
    reorder them into positional arguments. ``http_get`` is the injectable HTTP
    seam (mirroring :mod:`partgraph.ingest.fetch` / :mod:`partgraph.refresh.links`
    discipline): tests pass a fake that never opens a real socket. It defaults to
    ``None`` so the real :func:`requests.get` is resolved **lazily** — this leaf
    never imports :mod:`requests` merely by being imported. The seam is invoked
    exactly as ``http_get(url, timeout=timeout)`` and must return an object
    exposing ``.status_code`` and ``.json()``.

    Classification (ADR-0018):
    - HTTP 200 -> healthy (the sole liveness gate); the body is parsed
      best-effort for ``status``/``version``, but a missing or malformed body
      does NOT downgrade health.
    - Any non-200 -> reachable but not healthy; the message names the code.
    - :class:`requests.exceptions.Timeout` -> unreachable, with a dedicated
      timeout message distinct from the generic connection message.
    - :class:`requests.exceptions.RequestException` (connection refused, DNS,
      reset, ...) -> unreachable, with the fixed ``partgraph db up`` hint.

    Any exception that is NOT a :class:`requests.exceptions.RequestException`
    (e.g. a programming error in an injected seam) is deliberately NOT caught: it
    propagates rather than being coerced into a misleading "unreachable" result.

    Returns:
        A frozen :class:`HealthResult`. The ``message`` is always safe to print
        verbatim: it never contains a raw exception string, a response body, or a
        filesystem path.
    """
    # Lazy import: keeps this leaf free of a module-level third-party dependency
    # (a subprocess test pins that ``import partgraph.util.health`` does not pull
    # in ``requests``). ``requests`` is resolved only when a probe actually runs
    # — needed both for the default seam AND for the specific
    # ``requests.exceptions.*`` classes caught below. ``requests`` is a declared
    # core dependency (pyproject.toml), so this import cannot fail at runtime.
    import requests  # noqa: PLC0415 — deliberate lazy import; see docstring.

    if http_get is None:
        http_get = requests.get

    try:
        response = http_get(url, timeout=timeout)
    except requests.exceptions.Timeout:
        return HealthResult(
            reachable=False,
            healthy=False,
            http_status=None,
            status=None,
            version=None,
            message=f"Dgraph health probe timed out after {timeout}s.",
        )
    except requests.exceptions.RequestException:
        return HealthResult(
            reachable=False,
            healthy=False,
            http_status=None,
            status=None,
            version=None,
            message=_UNREACHABLE_MESSAGE,
        )

    http_status = response.status_code
    if http_status != _HTTP_OK:
        # Reachable (the socket answered) but not healthy. Name only the integer
        # code; deliberately do NOT read the body, so no body text can leak.
        return HealthResult(
            reachable=True,
            healthy=False,
            http_status=http_status,
            status=None,
            version=None,
            message=f"Dgraph responded with HTTP {http_status} (not healthy).",
        )

    # HTTP 200 is the sole liveness gate: healthy regardless of body shape.
    status_value, version_value = _parse_health_body(response)
    if version_value is not None:
        message = f"Dgraph is healthy ({version_value})."
    elif status_value is not None:
        message = "Dgraph is healthy."
    else:
        message = "Dgraph is healthy (HTTP 200); response payload unrecognized."
    return HealthResult(
        reachable=True,
        healthy=True,
        http_status=http_status,
        status=status_value,
        version=version_value,
        message=message,
    )


def _parse_health_body(response: Any) -> tuple[str | None, str | None]:
    """Best-effort extract ``(status, version)`` from a 200 health response.

    The documented body shape is a non-empty JSON list whose first element is a
    dict, e.g. ``[{"instance": "alpha", "status": "healthy", "version":
    "v25.3.4", ...}]`` (docs/connecting.md). Returns ``(None, None)`` when the
    body is empty / non-list / non-dict, or when ``.json()`` raises
    :class:`ValueError` — those are NOT errors here (HTTP 200 already decided
    liveness); the raw body and exception are intentionally discarded so nothing
    leaks into the message.
    """
    try:
        body = response.json()
    except ValueError:
        return None, None
    if not (isinstance(body, list) and body and isinstance(body[0], dict)):
        return None, None
    first = body[0]
    raw_status = first.get("status")
    raw_version = first.get("version")
    status_value = raw_status if isinstance(raw_status, str) else None
    version_value = raw_version if isinstance(raw_version, str) else None
    return status_value, version_value
