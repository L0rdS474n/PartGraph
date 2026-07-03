"""
Tests: AC-RL-5..13, AC-RL-17 — partgraph.refresh.links (leaf logic)

Specifies the behaviour of the NEW leaf module ``partgraph.refresh.links``,
which underpins the `partgraph refresh-links` CLI command (issue #11, PR 1).
This module must depend only on the injected ``http_client`` / ``client``
(pydgraph) / ``clock`` / ``sleep`` seams — never a real socket, a real Dgraph
connection or the real wall clock — mirroring the embed pipeline's
(``partgraph.embed``) leaf-module discipline (ADR-0010) and the fetch module's
(``partgraph.ingest.fetch``) injectable-http-client discipline.

Contract pinned by this file (leaf module ``partgraph.refresh.links``):
  - ``USER_AGENT: str`` — dedicated User-Agent sent on every HTTP call.
  - ``DEFAULT_MAX_FAILURES: int`` / ``DEFAULT_TIMEOUT: float`` — module defaults.
  - ``HostRateLimiter(min_interval, *, clock, sleep)`` with ``.acquire(host)``:
    a monotonic-seconds clock/sleep pair (distinct from the wall-clock ``clock``
    used for ``verified_at`` below), so two checks against the SAME host within
    ``min_interval`` seconds pause via the injected ``sleep`` — never
    ``time.sleep`` directly.
  - ``is_checkable_url(url) -> bool`` — validate-before-I/O URL policy.
  - ``classify_url(url, http_client, *, timeout) -> tuple[bool, int]`` —
    ``(alive, http_status)``; HEAD first, GET fallback ONLY on 405/501.
  - ``format_verified_at(moment: datetime) -> str`` — deterministic RFC-3339
    UTC ('Z'-suffixed) string for a given (injected) datetime.
  - ``refresh_links_write(datasheets_iter, client, *, http_client, clock,
    max_failures=DEFAULT_MAX_FAILURES, timeout=DEFAULT_TIMEOUT,
    rate_limiter=None, on_purge=None) -> dict`` — the main leaf entry point.
    Returns EXACTLY ``{"checked": int, "alive": int, "dead": int, "purged": int}``
    (AC-RL-17) — purge EVENT detail (which uid, its fail_count, how many Parts
    were unlinked) is NOT in this summary dict; it is reported via the optional
    ``on_purge(uid: str, fail_count: int, parts_unlinked: int)`` callback,
    mirroring how ``embed_write``'s ``progress`` callback reports detail the
    CLI renders but the leaf's own summary dict does not carry (ADR-0010
    precedent). The CLI (tested separately in test_cli_refresh_links.py) is
    responsible for turning an ``on_purge`` invocation into the actual
    path-free destructive console notice; this file instead asserts the
    callback receives only plain, path-free, exception-free primitives.

Design choice flagged for the impl gate: the purge mutation is modelled here
via ``txn.mutate(del_nquads=...)`` (raw RDF n-quads: one
``<part_uid> <datasheet> <datasheet_uid> .`` triple per referencing Part, plus
one ``<datasheet_uid> * * .`` node-delete triple) — mirroring the ONLY existing
in-repo node-deletion precedent, ``tests/conftest.py``'s
``cleanup_marker_nodes`` fixture (which builds exactly this ``<uid> * * .``
form via ``pydgraph.Mutation(del_nquads=...)``). ``del_obj`` (JSON) was
considered but rejected: this repo has no verified precedent that a
JSON-delete-mutation containing only ``{"uid": ...}`` deletes an entire node's
predicates in this pydgraph/Dgraph version, whereas ``<uid> * * .`` is the
documented, already-used-here RDF form. If the impl gate prefers ``del_obj``,
this test file must be updated in lockstep.

Idempotency (AC-RL-13) split across files: this file pins the LEAF's own
defensive contribution — a uid appearing twice within a single
``refresh_links_write`` call is processed (checked/written) exactly once. The
cross-page "each Datasheet selected at most once per run" guarantee (the uid
keyset cursor) is a CLI-level concern and is pinned in
test_cli_refresh_links.py (AC-RL-4), mirroring embed's AC-EC-8 cursor tests.

Gate 3 hardening pass (security FAIL + architecture flag) — closed gaps:
  - SSRF fail-closed (AC-RL-6 extended): ``ipaddress.ip_address()`` raises
    ValueError for numeric/alt-encoding IPv4 forms (decimal ``2130706433``,
    hex ``0x7f000001``, octal ``017700000001``/``0177.0.0.1``, shorthand
    ``127.1``, bare ``0``) — VERIFIED empirically (see the dispatcher-run
    check that seeded these tests) — so "ValueError ⇒ treat as a plain
    hostname, allow" is itself the exact bypass a permissive OS resolver or
    HTTP library can still resolve to loopback. ``is_checkable_url`` must
    fail closed on these forms, never fail open. ``::ffff:127.0.0.1``
    (IPv4-mapped IPv6 loopback) DOES parse via ``ipaddress`` with
    ``is_loopback=True`` — verified and locked in as a straightforward
    rejection. A wider scheme-denylist SHAPE (sftp/ws/jar/dict, beyond the
    original 4 examples) forces an allow-list-of-{http,https}
    implementation rather than a hardcoded small deny-list.
  - fail_count None/absent (AC-RL-9 extended): fail_count is a NEW
    predicate, so every pre-existing Datasheet row has it UNSET on the first
    run ever. A selection row with fail_count missing/None must be treated
    as 0 (dead -> 1, alive -> 0) — never ``None + 1`` (TypeError).
  - Post-crash purge, ``>=`` not ``==`` (AC-RL-10 extended): a node stranded
    by a crash between the write-back commit and the purge txn can already
    carry a prior fail_count AT/ABOVE max_failures; this run's dead check
    then makes new_fail_count STRICTLY GREATER than max_failures, which
    must still purge — forcing ``>=``, not ``==``.
  - Rate limiter actually invoked (AC-RL-7 extended): a leaf-level
    call-order trace proves ``rate_limiter.acquire(host)`` is called
    BEFORE each row's HTTP check, not merely accepted as an unused
    parameter. The CLI-level "real HostRateLimiter constructed and
    threaded through" half lives in test_cli_refresh_links.py.
  - Outbound headers (AC-RL-5 extended): asserts no credential-like header
    key (Authorization/Cookie/Proxy-Authorization) is ever sent, and that
    User-Agent is the only header key present.
  - Write-back/purge mutation failures propagate (not swallowed), so the
    CLI's try/except can convert them to the path-free _REFRESH_DB_ERROR
    (the CLI-side exit-1 assertion lives in test_cli_refresh_links.py).

NOTE: Collection will ERROR because partgraph.refresh.links does not yet
exist. That is the expected RED state before implementation.
"""

from __future__ import annotations

import json
import socket
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# --- Module under test (will be red until implementation exists) ---
from partgraph.refresh.links import (  # noqa: F401
    DEFAULT_MAX_FAILURES,
    DEFAULT_TIMEOUT,
    USER_AGENT,
    HostRateLimiter,
    classify_url,
    format_verified_at,
    is_checkable_url,
    refresh_links_write,
)

# ---------------------------------------------------------------------------
# Fixed test instants (deliberately NOT "now" in any real sense, so a test
# that accidentally reads the real wall clock fails an exact-match assertion
# instead of passing by coincidence).
# ---------------------------------------------------------------------------

_FIXED_MOMENT = datetime(2030, 1, 15, 8, 30, 0, tzinfo=UTC)
_FIXED_MOMENT_STR = "2030-01-15T08:30:00Z"


# ---------------------------------------------------------------------------
# Fake HTTP client (HEAD + GET methods; scriptable status codes / exceptions).
# Modeled on tests/unit/test_fetch.py's _FakeHttpClient/_FakeResponse, but
# exposes .head()/.get() directly (mirroring real httpx.Client.head/.get)
# rather than .stream(), since link classification never streams a body.
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal fake HTTP response exposing only .status_code."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHttpClient:
    """Injectable HTTP client that never opens a real socket.

    ``head_result``/``get_result`` are each either a ``_FakeResponse`` instance
    (returned) or an ``Exception`` instance (raised) — scriptable per call.
    """

    def __init__(
        self,
        *,
        head_result: _FakeResponse | Exception | None = None,
        get_result: _FakeResponse | Exception | None = None,
    ) -> None:
        self.head_result = head_result
        self.get_result = get_result
        self.calls: list[dict] = []

    def head(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append({"method": "HEAD", "url": url, "kwargs": kwargs})
        if isinstance(self.head_result, Exception):
            raise self.head_result
        return self.head_result

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, "kwargs": kwargs})
        if isinstance(self.get_result, Exception):
            raise self.get_result
        return self.get_result


class _TimeoutLikeError(Exception):
    """Stand-in for a network timeout (no httpx dependency needed in tests)."""


class _TlsLikeError(Exception):
    """Stand-in for a TLS/certificate failure."""


class _ConnectLikeError(Exception):
    """Stand-in for a connection-refused/DNS failure."""


# ---------------------------------------------------------------------------
# Fake pydgraph client/txn builders
# ---------------------------------------------------------------------------

def _make_txn() -> MagicMock:
    txn = MagicMock()
    txn.discard.return_value = None
    txn.commit.return_value = None
    txn.mutate.return_value = MagicMock()
    txn.__enter__ = MagicMock(return_value=txn)
    txn.__exit__ = MagicMock(return_value=False)
    return txn


def _make_reverse_lookup_read_txn(datasheet_uid: str, part_uids: list[str]) -> MagicMock:
    """Return a read-only txn answering a '~datasheet' reverse-edge query."""
    txn = _make_txn()

    def _query_side_effect(query_text, *args, **kwargs):
        resp = MagicMock()
        if "~datasheet" in query_text and datasheet_uid in query_text:
            resp.json = json.dumps(
                {"q": [{"~datasheet": [{"uid": u} for u in part_uids]}]}
            ).encode()
        else:
            resp.json = json.dumps({"q": []}).encode()
        return resp

    txn.query.side_effect = _query_side_effect
    return txn


def _make_refresh_client(
    read_txn: MagicMock,
    write_type_txns: list[MagicMock],
) -> MagicMock:
    """Return a mock client dispatching by the ``read_only`` kwarg.

    Every ``client.txn(read_only=True)`` call returns *read_txn* (used for the
    purge reverse-edge lookup). Every plain ``client.txn()`` call pops the next
    mock from *write_type_txns* in order (first = the fail_count write-back
    txn, second = the purge txn), mirroring test_cli_embed.py's
    ``_make_paged_mock_client`` dispatch-by-kwarg pattern.
    """
    client = MagicMock()
    queue = list(write_type_txns)

    def _txn_factory(*args, **kwargs):
        if kwargs.get("read_only"):
            return read_txn
        return queue.pop(0) if queue else _make_txn()

    client.txn.side_effect = _txn_factory
    return client


def _make_simple_client(write_txn: MagicMock) -> MagicMock:
    """Return a mock client where every client.txn() call returns *write_txn*."""
    client = MagicMock()
    client.txn.return_value = write_txn
    return client


def _written_payload_items(write_txn: MagicMock) -> list[dict]:
    """Flatten every set_obj payload item written via *write_txn*.mutate()."""
    items: list[dict] = []
    for call_obj in write_txn.mutate.call_args_list:
        _, kwargs = call_obj
        set_obj = kwargs.get("set_obj")
        if set_obj is None:
            continue
        items.extend(set_obj if isinstance(set_obj, list) else [set_obj])
    return items


# ---------------------------------------------------------------------------
# Fake datasheet row builder
# ---------------------------------------------------------------------------

def _make_row(
    *,
    uid: str = "0xD001",
    url: str = "https://lcsc.com/datasheet/foo.pdf",
    http_status: int | None = 0,
    fail_count: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(uid=uid, url=url, http_status=http_status, fail_count=fail_count)


def _fixed_clock() -> datetime:
    return _FIXED_MOMENT


# ===========================================================================
# AC-RL-5: HTTP classify (HEAD first, GET fallback only on 405/501)
# ===========================================================================

@pytest.mark.parametrize("status", [200, 201, 204, 301, 302, 304, 399])
def test_ac_rl_5_head_2xx_3xx_is_alive_no_get_fallback(status: int) -> None:
    """AC-RL-5: Given a HEAD response with a 2xx/3xx status.
    When classify_url is called.
    Then it returns (alive=True, http_status=status) and issues NO GET call.
    """
    client = _FakeHttpClient(head_result=_FakeResponse(status))
    alive, http_status = classify_url(
        "https://lcsc.com/x.pdf", client, timeout=DEFAULT_TIMEOUT
    )
    assert alive is True, f"AC-RL-5: status {status} must classify alive."
    assert http_status == status
    get_calls = [c for c in client.calls if c["method"] == "GET"]
    assert not get_calls, f"AC-RL-5: no GET fallback expected for status {status}."


@pytest.mark.parametrize("status", [400, 403, 404, 410, 429, 500, 502, 503])
def test_ac_rl_5_head_4xx_5xx_is_dead_no_get_fallback(status: int) -> None:
    """AC-RL-5: Given a HEAD response with a 4xx/5xx status (excluding 405/501).
    When classify_url is called.
    Then it returns (alive=False, http_status=status) and issues NO GET call.
    """
    client = _FakeHttpClient(head_result=_FakeResponse(status))
    alive, http_status = classify_url(
        "https://lcsc.com/x.pdf", client, timeout=DEFAULT_TIMEOUT
    )
    assert alive is False, f"AC-RL-5: status {status} must classify dead."
    assert http_status == status
    get_calls = [c for c in client.calls if c["method"] == "GET"]
    assert not get_calls, f"AC-RL-5: no GET fallback expected for status {status}."


@pytest.mark.parametrize("status", [405, 501])
def test_ac_rl_5_head_405_501_falls_back_to_get(status: int) -> None:
    """AC-RL-5: Given a HEAD response with status 405 or 501.
    When classify_url is called.
    Then exactly one GET call is issued (fallback), and the returned
    classification is based on the GET response, not the HEAD response.
    """
    client = _FakeHttpClient(
        head_result=_FakeResponse(status), get_result=_FakeResponse(200)
    )
    alive, http_status = classify_url(
        "https://lcsc.com/x.pdf", client, timeout=DEFAULT_TIMEOUT
    )
    get_calls = [c for c in client.calls if c["method"] == "GET"]
    assert len(get_calls) == 1, (
        f"AC-RL-5: HEAD status {status} must trigger exactly one GET fallback. "
        f"Calls: {client.calls!r}"
    )
    assert alive is True and http_status == 200, (
        f"AC-RL-5: classification must reflect the GET fallback response "
        f"(200), not the original HEAD status ({status}). "
        f"Got alive={alive}, http_status={http_status}."
    )


def test_ac_rl_5_get_fallback_dead_result_used() -> None:
    """AC-RL-5: Given HEAD returns 405 and the GET fallback returns 404.
    When classify_url is called.
    Then the result is dead with http_status 404 (the GET result), not 405.
    """
    client = _FakeHttpClient(
        head_result=_FakeResponse(405), get_result=_FakeResponse(404)
    )
    alive, http_status = classify_url("https://lcsc.com/x.pdf", client, timeout=5.0)
    assert alive is False
    assert http_status == 404


@pytest.mark.parametrize(
    "exc",
    [
        _TimeoutLikeError("timed out"),
        _TlsLikeError("certificate verify failed"),
        _ConnectLikeError("connection refused"),
    ],
)
def test_ac_rl_5_head_exception_is_dead_status_zero(exc: Exception) -> None:
    """AC-RL-5: Given the HEAD call raises (timeout/TLS/connect failure).
    When classify_url is called.
    Then it returns (alive=False, http_status=0) — never propagates the
    exception — and issues NO GET call.
    """
    client = _FakeHttpClient(head_result=exc)
    alive, http_status = classify_url("https://lcsc.com/x.pdf", client, timeout=5.0)
    assert alive is False, "AC-RL-5: a HEAD transport failure must classify dead."
    assert http_status == 0, (
        f"AC-RL-5: a HEAD transport failure must report http_status 0. "
        f"Got {http_status!r}."
    )
    get_calls = [c for c in client.calls if c["method"] == "GET"]
    assert not get_calls, "AC-RL-5: a HEAD transport failure must not trigger GET."


def test_ac_rl_5_get_fallback_exception_is_dead_status_zero() -> None:
    """AC-RL-5: Given HEAD returns 405 (triggering fallback) and the GET call
    itself raises a transport error.
    When classify_url is called.
    Then it returns (alive=False, http_status=0) without propagating.
    """
    client = _FakeHttpClient(
        head_result=_FakeResponse(405),
        get_result=_ConnectLikeError("connection reset"),
    )
    alive, http_status = classify_url("https://lcsc.com/x.pdf", client, timeout=5.0)
    assert alive is False
    assert http_status == 0


def test_ac_rl_5_socket_create_connection_never_called() -> None:
    """AC-RL-5: Given a fully injected http_client.
    When classify_url is called (HEAD 200 path).
    Then socket.create_connection is never called (no real OS-level socket),
    mirroring test_fetch.py's no-real-socket guarantee.
    """
    client = _FakeHttpClient(head_result=_FakeResponse(200))
    with patch("socket.create_connection") as mock_conn:
        classify_url("https://lcsc.com/x.pdf", client, timeout=5.0)
    assert not mock_conn.called, (
        "AC-RL-5: socket.create_connection must never be called when an "
        "injected http_client is supplied."
    )


def test_ac_rl_5_dedicated_user_agent_header_sent_on_head_and_get() -> None:
    """AC-RL-5: Given a HEAD call (and a GET fallback).
    When classify_url is called.
    Then both the HEAD call and the GET fallback call carry a 'headers' kwarg
    whose 'User-Agent' equals the module's dedicated USER_AGENT constant (not
    a generic/default httpx user agent).
    """
    assert isinstance(USER_AGENT, str) and USER_AGENT, (
        "AC-RL-5: USER_AGENT must be a non-empty string constant."
    )
    assert "python-requests" not in USER_AGENT and "httpx" not in USER_AGENT.lower(), (
        f"AC-RL-5: USER_AGENT must be a DEDICATED value, not a library default. "
        f"Got: {USER_AGENT!r}"
    )
    client = _FakeHttpClient(
        head_result=_FakeResponse(405), get_result=_FakeResponse(200)
    )
    classify_url("https://lcsc.com/x.pdf", client, timeout=5.0)

    assert len(client.calls) == 2, f"Expected HEAD then GET. Got: {client.calls!r}"
    for call in client.calls:
        headers = call["kwargs"].get("headers") or {}
        assert headers.get("User-Agent") == USER_AGENT, (
            f"AC-RL-5: {call['method']} call must send headers={{'User-Agent': "
            f"{USER_AGENT!r}}}. Got headers: {headers!r}"
        )


def test_ac_rl_5_timeout_propagated_to_head_call() -> None:
    """AC-RL-5: Given classify_url is called with a specific timeout value.
    Then that value is forwarded to the underlying HEAD call (so a real
    network client actually bounds the request).
    """
    client = _FakeHttpClient(head_result=_FakeResponse(200))
    classify_url("https://lcsc.com/x.pdf", client, timeout=3.5)
    assert client.calls[0]["kwargs"].get("timeout") == 3.5, (
        f"AC-RL-5: timeout=3.5 must be forwarded to the HEAD call. "
        f"Got kwargs: {client.calls[0]['kwargs']!r}"
    )


def test_ac_rl_5_outbound_headers_carry_no_credential_like_keys() -> None:
    """AC-RL-5 (Gate 3): Given a HEAD call (and its GET fallback).
    When classify_url is called.
    Then the outbound 'headers' dict contains ONLY the User-Agent key —
    case-insensitively, NO Authorization/Cookie/Proxy-Authorization (or any
    other credential-like) header is ever sent to a third-party datasheet
    host. A link-checker has no business forwarding credentials anywhere,
    and a future refactor that threads some ambient session/header dict
    through by accident must not silently leak one.
    """
    client = _FakeHttpClient(
        head_result=_FakeResponse(405), get_result=_FakeResponse(200)
    )
    classify_url("https://lcsc.com/x.pdf", client, timeout=5.0)

    assert len(client.calls) == 2, f"Expected HEAD then GET. Got: {client.calls!r}"
    forbidden = {"authorization", "cookie", "proxy-authorization"}
    for call in client.calls:
        headers = call["kwargs"].get("headers") or {}
        lower_keys = {k.lower() for k in headers}
        assert lower_keys == {"user-agent"}, (
            f"AC-RL-5: {call['method']} headers must contain ONLY "
            f"'User-Agent' (case-insensitive), got keys: {sorted(headers)!r}"
        )
        assert not (lower_keys & forbidden), (
            f"AC-RL-5: {call['method']} headers must never carry a "
            f"credential-like key. Got: {sorted(headers)!r}"
        )


# ===========================================================================
# AC-RL-6: URL policy (validate-before-I/O)
# ===========================================================================

@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x.pdf",
        "gopher://example.com/x",
        "data:text/plain;base64,QQ==",
        "sftp://example.com/x",
        "ws://example.com/x",
        "jar://example.com/x",
        "dict://example.com/x",
    ],
)
def test_ac_rl_6_non_http_scheme_rejected(url: str) -> None:
    """AC-RL-6 (Gate 3: wider scheme-denylist SHAPE): Given a URL whose
    scheme is not http/https — including sftp/ws/jar/dict alongside the
    original file/ftp/gopher/data examples.
    When is_checkable_url(url) is called.
    Then it returns False (rejected before any I/O). This wider set of
    schemes forces an ALLOW-LIST-of-{http,https} implementation rather than
    a hardcoded small deny-list of 4 schemes.
    """
    assert is_checkable_url(url) is False, (
        f"AC-RL-6: non-http(s) scheme must be rejected: {url!r}"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/datasheet.pdf",       # loopback IPv4
        "http://[::1]/datasheet.pdf",           # loopback IPv6
        "http://169.254.1.1/x",                 # link-local IPv4
        "http://[fe80::1]/x",                   # link-local IPv6
        "http://10.0.0.5/x",                    # private IPv4 (RFC1918)
        "http://192.168.1.1/x",                 # private IPv4 (RFC1918)
        "http://172.16.0.1/x",                  # private IPv4 (RFC1918)
        "http://0.0.0.0/x",                     # unspecified IPv4
        "http://[::]/x",                        # unspecified IPv6
    ],
)
def test_ac_rl_6_literal_loopback_linklocal_private_unspecified_ip_rejected(
    url: str,
) -> None:
    """AC-RL-6: Given a URL whose host is a LITERAL loopback/link-local/
    private/unspecified IP address (verified against stdlib ipaddress).
    When is_checkable_url(url) is called.
    Then it returns False.
    """
    assert is_checkable_url(url) is False, (
        f"AC-RL-6: literal loopback/link-local/private/unspecified IP host "
        f"must be rejected: {url!r}"
    )


def test_ac_rl_6_ipv4_mapped_ipv6_loopback_rejected() -> None:
    """AC-RL-6 (Gate 3): Given a URL whose host is the IPv4-mapped IPv6
    loopback literal ``::ffff:127.0.0.1`` (bracketed).
    When is_checkable_url(url) is called.
    Then it returns False.

    Verified directly against stdlib ipaddress before writing this
    assertion: ``ipaddress.ip_address("::ffff:127.0.0.1")`` parses
    successfully as an IPv6Address with ``is_loopback == True`` — so this is
    a straightforward, catchable case (unlike the numeric/alt-encoding IPv4
    forms below, which ipaddress cannot parse at all).
    """
    assert is_checkable_url("http://[::ffff:127.0.0.1]/x") is False, (
        "AC-RL-6: the IPv4-mapped IPv6 loopback literal '::ffff:127.0.0.1' "
        "must be rejected (ipaddress correctly classifies it as loopback)."
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/x",       # decimal encoding of 127.0.0.1
        "http://0x7f000001/x",       # hex encoding of 127.0.0.1
        "http://017700000001/x",    # single-token octal encoding of 127.0.0.1
        "http://127.1/x",           # BSD-style shorthand for 127.0.0.1
        "http://0/x",               # bare "0", many resolvers treat as 0.0.0.0
        "http://0177.0.0.1/x",      # per-octet octal encoding of 127.0.0.1
    ],
)
def test_ac_rl_6_numeric_alt_encoding_ipv4_bypass_forms_rejected(url: str) -> None:
    """AC-RL-6 (Gate 3, SECURITY FAIL from prior review): Given a URL whose
    host is a numeric/alternate-encoding form of a loopback address that
    ``ipaddress.ip_address()`` itself CANNOT parse (raises ValueError) but
    that a permissive OS resolver or HTTP library's own connect-time
    getaddrinfo() may still resolve to 127.0.0.1 (decimal/hex/octal/
    shorthand IPv4 encodings are a well-known SSRF bypass class).

    Empirically verified before writing this assertion: every one of these
    six host strings makes ``ipaddress.ip_address(host)`` raise
    ``ValueError: '...' does not appear to be an IPv4 or IPv6 address``.

    When is_checkable_url(url) is called.
    Then it returns False — the implementation MUST fail closed on a host
    ipaddress cannot parse but that LOOKS numeric/hex/octal-shaped, rather
    than falling back to "ValueError from ipaddress -> treat as a plain
    hostname -> allow". That fallback is the exact bypass this test exists
    to close.
    """
    assert is_checkable_url(url) is False, (
        f"AC-RL-6 (Gate 3): numeric/alt-encoding IPv4 bypass host must be "
        f"rejected (fail closed), not allowed through as a 'hostname': {url!r}"
    )


def test_ac_rl_6_end_to_end_decimal_encoded_loopback_never_calls_http_client() -> None:
    """AC-RL-6 (Gate 3): Given a Datasheet row whose url uses the decimal
    numeric encoding of the loopback address (2130706433 == 127.0.0.1) — a
    host string ipaddress.ip_address() cannot parse at all.
    When refresh_links_write processes it.
    Then the injected http_client receives ZERO calls (fail closed end to
    end, not just at the is_checkable_url unit level), and it is still
    written as dead/http_status=0/fail_count=prev+1.
    """
    row = _make_row(url="http://2130706433/internal.pdf", fail_count=0)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock
    )

    assert not http_client.calls, (
        f"AC-RL-6 (Gate 3): a decimal-encoded loopback URL must never reach "
        f"the HTTP client end to end. Calls: {http_client.calls!r}"
    )
    assert summary["dead"] == 1 and summary["alive"] == 0, (
        f"AC-RL-6 (Gate 3): the bypass-shaped URL must still be counted "
        f"dead. Got: {summary!r}"
    )
    items = _written_payload_items(write_txn)
    assert items[0]["http_status"] == 0 and items[0]["fail_count"] == 1, (
        f"AC-RL-6 (Gate 3): must write http_status=0, fail_count=prev+1. "
        f"Got: {items!r}"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://lcsc.com/datasheet/foo.pdf",   # plain hostname
        "http://example.com/x.pdf",             # plain hostname
        "http://8.8.8.8/x",                     # literal PUBLIC IP (allowed)
    ],
)
def test_ac_rl_6_hostname_and_public_ip_proceed(url: str) -> None:
    """AC-RL-6: Given a URL whose host is a plain DNS hostname, or a literal
    PUBLIC IP address (none of loopback/link-local/private/unspecified).
    When is_checkable_url(url) is called.
    Then it returns True (the check proceeds to HTTP).
    """
    assert is_checkable_url(url) is True, (
        f"AC-RL-6: a plain hostname or public IP literal must be checkable: {url!r}"
    )


def test_ac_rl_6_end_to_end_unsafe_url_never_calls_http_client() -> None:
    """AC-RL-6: Given a Datasheet row whose url targets a loopback IP.
    When refresh_links_write processes it.
    Then the result is dead/http_status=0, the injected http_client receives
    ZERO calls, and the row is still counted (checked=1, dead=1) — the policy
    rejection is itself a definitive (if trivial) check outcome.
    """
    row = _make_row(url="http://127.0.0.1/internal.pdf", fail_count=0)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock
    )

    assert not http_client.calls, (
        f"AC-RL-6: an unsafe (loopback) URL must never reach the HTTP client. "
        f"Calls: {http_client.calls!r}"
    )
    assert summary["checked"] == 1 and summary["dead"] == 1 and summary["alive"] == 0, (
        f"AC-RL-6: the unsafe URL must still be counted as a checked/dead row. "
        f"Got: {summary!r}"
    )
    items = _written_payload_items(write_txn)
    assert items and items[0]["http_status"] == 0 and items[0]["fail_count"] == 1, (
        f"AC-RL-6: unsafe URL must write http_status=0, fail_count=prev+1. "
        f"Got: {items!r}"
    )


# ===========================================================================
# AC-RL-7: per-host rate limit
# ===========================================================================

def test_ac_rl_7_first_call_for_a_host_never_sleeps() -> None:
    """AC-RL-7: Given a HostRateLimiter with no prior record for a host.
    When .acquire(host) is called for the first time.
    Then the injected sleep is never called.
    """
    fake_sleep = MagicMock()
    limiter = HostRateLimiter(1.0, clock=lambda: 1000.0, sleep=fake_sleep)
    limiter.acquire("lcsc.com")
    fake_sleep.assert_not_called()


def test_ac_rl_7_second_same_host_check_within_interval_sleeps_remaining() -> None:
    """AC-RL-7: Given two .acquire() calls for the SAME host where the second
    arrives before min_interval seconds have elapsed (per the injected clock).
    When HostRateLimiter.acquire(host) is called twice.
    Then the injected sleep is called EXACTLY once, with the remaining wait
    (min_interval - elapsed), and real time.sleep is NEVER called.
    """
    clock_values = iter([1000.0, 1000.2])
    fake_sleep = MagicMock()
    limiter = HostRateLimiter(1.0, clock=lambda: next(clock_values), sleep=fake_sleep)

    with patch("time.sleep") as real_sleep:
        limiter.acquire("lcsc.com")
        limiter.acquire("lcsc.com")

    fake_sleep.assert_called_once()
    (waited,), _ = fake_sleep.call_args
    assert waited == pytest.approx(0.8), (
        f"AC-RL-7: expected sleep(~0.8) (1.0 - 0.2 elapsed). Got sleep({waited!r})."
    )
    assert not real_sleep.called, (
        "AC-RL-7: real time.sleep must NEVER be called; only the injected sleep."
    )


def test_ac_rl_7_elapsed_at_or_above_min_interval_does_not_sleep() -> None:
    """AC-RL-7: Given two .acquire() calls for the same host where the second
    arrives AT/AFTER min_interval seconds have elapsed.
    When HostRateLimiter.acquire(host) is called twice.
    Then the injected sleep is never called.
    """
    clock_values = iter([1000.0, 1001.0])
    fake_sleep = MagicMock()
    limiter = HostRateLimiter(1.0, clock=lambda: next(clock_values), sleep=fake_sleep)

    limiter.acquire("lcsc.com")
    limiter.acquire("lcsc.com")

    fake_sleep.assert_not_called()


def test_ac_rl_7_different_hosts_do_not_rate_limit_each_other() -> None:
    """AC-RL-7: Given two .acquire() calls for DIFFERENT hosts in immediate
    succession (per the injected clock).
    When HostRateLimiter.acquire(host) is called for each.
    Then the injected sleep is never called (rate limiting is per-host).
    """
    clock_values = iter([1000.0, 1000.01])
    fake_sleep = MagicMock()
    limiter = HostRateLimiter(1.0, clock=lambda: next(clock_values), sleep=fake_sleep)

    limiter.acquire("lcsc.com")
    limiter.acquire("digikey.com")

    fake_sleep.assert_not_called()


def test_ac_rl_7_refresh_links_write_calls_rate_limiter_acquire_before_each_check() -> None:
    """AC-RL-7 (Gate 3: rate limiter actually invoked, not just unit-tested
    in a vacuum): Given a rate_limiter is injected into refresh_links_write
    and two rows share the SAME host.
    When refresh_links_write processes them.
    Then rate_limiter.acquire(host) is called once per row, and — proven via
    a single shared call-order trace covering BOTH the limiter and the HTTP
    client — strictly BEFORE that row's HTTP check each time (not merely
    called somewhere during the whole run, and not called once up front for
    the whole batch).
    """
    order: list[tuple] = []

    class _TracingLimiter:
        def acquire(self, host: str) -> None:
            order.append(("acquire", host))

    class _TracingHttpClient(_FakeHttpClient):
        def head(self, url: str, **kwargs):
            order.append(("head", url))
            return super().head(url, **kwargs)

    row1 = _make_row(uid="0xH1", url="https://lcsc.com/a.pdf", fail_count=0)
    row2 = _make_row(uid="0xH2", url="https://lcsc.com/b.pdf", fail_count=0)
    http_client = _TracingHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_links_write(
        iter([row1, row2]), client, http_client=http_client, clock=_fixed_clock,
        rate_limiter=_TracingLimiter(),
    )

    assert order == [
        ("acquire", "lcsc.com"), ("head", "https://lcsc.com/a.pdf"),
        ("acquire", "lcsc.com"), ("head", "https://lcsc.com/b.pdf"),
    ], (
        f"AC-RL-7: rate_limiter.acquire(host) must precede each row's HTTP "
        f"check, interleaved per row (not batched up front). Got: {order!r}"
    )


# ===========================================================================
# AC-RL-8: injected fixed clock -> deterministic RFC-3339 UTC verified_at
# ===========================================================================

def test_ac_rl_8_format_verified_at_matches_fixed_moment_exactly() -> None:
    """AC-RL-8: Given a fixed, injected UTC datetime.
    When format_verified_at(moment) is called.
    Then the result is EXACTLY the expected RFC-3339 'Z'-suffixed string —
    proving no real wall-clock component leaks in (the fixed instant is
    deliberately not a plausible "now").
    """
    assert format_verified_at(_FIXED_MOMENT) == _FIXED_MOMENT_STR, (
        f"AC-RL-8: format_verified_at must produce the exact RFC-3339 string "
        f"for the injected moment. Got: {format_verified_at(_FIXED_MOMENT)!r}"
    )


def test_ac_rl_8_end_to_end_verified_at_uses_injected_clock_only() -> None:
    """AC-RL-8: Given refresh_links_write is called with an injected fixed
    clock callable (returning _FIXED_MOMENT, never real time).
    Then every written payload item's 'verified_at' equals the exact
    deterministic string derived from that fixed moment.
    """
    row = _make_row(url="https://lcsc.com/a.pdf", fail_count=0)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock
    )

    items = _written_payload_items(write_txn)
    assert items, "refresh_links_write must write at least one payload item."
    assert items[0]["verified_at"] == _FIXED_MOMENT_STR, (
        f"AC-RL-8: verified_at must equal the injected clock's deterministic "
        f"string, never a real wall-clock value. Got: {items[0]!r}"
    )


# ===========================================================================
# AC-RL-9: write-back shapes
# ===========================================================================

def test_ac_rl_9_alive_payload_shape_exact_keys_fail_count_zero() -> None:
    """AC-RL-9: Given an alive (HEAD 200) check for a Datasheet with a prior
    fail_count of 2.
    When refresh_links_write processes it.
    Then the written payload item has EXACTLY the keys
    {uid, verified_at, http_status, fail_count}, http_status == 200, and
    fail_count == 0 (reset on success).
    """
    row = _make_row(uid="0xA1", url="https://lcsc.com/a.pdf", fail_count=2)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_links_write(iter([row]), client, http_client=http_client, clock=_fixed_clock)

    items = _written_payload_items(write_txn)
    assert len(items) == 1
    item = items[0]
    assert set(item.keys()) == {"uid", "verified_at", "http_status", "fail_count"}, (
        f"AC-RL-9: alive payload must have EXACTLY "
        f"{{uid, verified_at, http_status, fail_count}}. Got: {item!r}"
    )
    assert item["http_status"] == 200
    assert item["fail_count"] == 0, (
        f"AC-RL-9: alive must reset fail_count to 0. Got: {item!r}"
    )
    assert item["uid"] == "0xA1"


def test_ac_rl_9_dead_payload_shape_exact_keys_fail_count_prev_plus_one() -> None:
    """AC-RL-9: Given a dead (HEAD 404) check for a Datasheet with a prior
    fail_count of 1.
    When refresh_links_write processes it.
    Then the written payload item has EXACTLY the keys
    {uid, verified_at, http_status, fail_count}, http_status == 404, and
    fail_count == 2 (prev + 1).
    """
    row = _make_row(uid="0xA2", url="https://lcsc.com/dead.pdf", fail_count=1)
    http_client = _FakeHttpClient(head_result=_FakeResponse(404))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock,
        max_failures=99,  # keep well above threshold so no purge fires here.
    )

    items = _written_payload_items(write_txn)
    assert len(items) == 1
    item = items[0]
    assert set(item.keys()) == {"uid", "verified_at", "http_status", "fail_count"}, (
        f"AC-RL-9: dead payload must have EXACTLY "
        f"{{uid, verified_at, http_status, fail_count}}. Got: {item!r}"
    )
    assert item["http_status"] == 404
    assert item["fail_count"] == 2, (
        f"AC-RL-9: dead must increment fail_count (prev=1 -> 2). Got: {item!r}"
    )


def test_ac_rl_9_uid_is_resolved_uid_never_a_blank_node() -> None:
    """AC-RL-9: Given a normal Datasheet row with a real uid.
    When refresh_links_write writes its payload.
    Then the 'uid' field is EXACTLY the row's own uid string — never a blank
    node reference (which would start with '_:' and mint a NEW node).
    """
    row = _make_row(uid="0xFEED", url="https://lcsc.com/x.pdf", fail_count=0)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_links_write(iter([row]), client, http_client=http_client, clock=_fixed_clock)

    items = _written_payload_items(write_txn)
    assert items[0]["uid"] == "0xFEED"
    assert not items[0]["uid"].startswith("_:"), (
        f"AC-RL-9: uid must never be a blank node. Got: {items[0]['uid']!r}"
    )


def test_ac_rl_9_fail_count_none_dead_increments_to_one_not_a_crash() -> None:
    """AC-RL-9 (Gate 3: guaranteed real-world initial state): Given a
    Datasheet row whose fail_count is explicitly None — the state EVERY
    pre-existing Datasheet row is guaranteed to have on the very first
    refresh-links run ever, since fail_count is a brand-new predicate.
    When refresh_links_write processes a dead (HEAD 404) check for it.
    Then it does NOT crash on `None + 1` (TypeError) — None is treated as 0,
    so the written fail_count is exactly 1.
    """
    row = _make_row(uid="0xN1", url="https://lcsc.com/never-checked.pdf")
    row.fail_count = None
    http_client = _FakeHttpClient(head_result=_FakeResponse(404))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock,
        max_failures=99,
    )

    items = _written_payload_items(write_txn)
    assert items[0]["fail_count"] == 1, (
        f"AC-RL-9: a None prior fail_count must be treated as 0 (dead -> 1), "
        f"never crash on None + 1. Got: {items!r}"
    )
    assert summary["checked"] == 1 and summary["dead"] == 1, (
        f"AC-RL-9: the None-fail_count row must still be checked/counted "
        f"normally. Got: {summary!r}"
    )


def test_ac_rl_9_fail_count_attribute_entirely_absent_dead_increments_to_one() -> None:
    """AC-RL-9 (Gate 3): Given a Datasheet row where the 'fail_count'
    attribute is not merely None but ENTIRELY ABSENT (getattr(row,
    'fail_count', None) returns None via the default, not because the field
    was explicitly set to null) — a slightly different real-world shape than
    an explicit None, e.g. if the row-building code only sets attributes for
    keys actually present in the raw JSON response.
    When refresh_links_write processes a dead (HEAD 500) check for it.
    Then it still does not crash, treating the absent attribute as 0, so the
    written fail_count is exactly 1.
    """
    row = SimpleNamespace(
        uid="0xN2", url="https://lcsc.com/no-fail-count-field.pdf", http_status=0
    )
    http_client = _FakeHttpClient(head_result=_FakeResponse(500))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock,
        max_failures=99,
    )

    items = _written_payload_items(write_txn)
    assert items[0]["fail_count"] == 1, (
        f"AC-RL-9: an entirely absent fail_count attribute must be treated "
        f"as 0 (dead -> 1), never crash. Got: {items!r}"
    )


def test_ac_rl_9_fail_count_none_alive_sets_to_zero() -> None:
    """AC-RL-9 (Gate 3): Given a Datasheet row with fail_count=None.
    When refresh_links_write processes an alive (HEAD 200) check for it.
    Then the written fail_count is exactly 0 (not a crash, not None).
    """
    row = _make_row(uid="0xN3", url="https://lcsc.com/first-check-ever.pdf")
    row.fail_count = None
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    refresh_links_write(iter([row]), client, http_client=http_client, clock=_fixed_clock)

    items = _written_payload_items(write_txn)
    assert items[0]["fail_count"] == 0, (
        f"AC-RL-9: alive with a None prior fail_count must write 0, not "
        f"None and not crash. Got: {items!r}"
    )


# ===========================================================================
# AC-RL-10: purge threshold (boundary: == triggers, ==-1 does not)
# ===========================================================================

def test_ac_rl_10_new_fail_count_equals_max_failures_triggers_purge() -> None:
    """AC-RL-10: Given max_failures=3 and a prior fail_count of 2 (so this
    run's dead result makes the NEW fail_count exactly 3 == max_failures).
    When refresh_links_write processes it.
    Then summary['purged'] == 1 (the purge decision fires at the boundary).
    """
    row = _make_row(uid="0xB1", url="https://lcsc.com/dying.pdf", fail_count=2)
    http_client = _FakeHttpClient(head_result=_FakeResponse(404))
    read_txn = _make_reverse_lookup_read_txn("0xB1", [])
    write_txn = _make_txn()
    purge_txn = _make_txn()
    client = _make_refresh_client(read_txn, [write_txn, purge_txn])

    summary = refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock,
        max_failures=3,
    )

    assert summary["purged"] == 1, (
        f"AC-RL-10: new fail_count == max_failures must trigger exactly one "
        f"purge. Got summary: {summary!r}"
    )


def test_ac_rl_10_new_fail_count_one_below_max_failures_no_purge() -> None:
    """AC-RL-10: Given max_failures=3 and a prior fail_count of 1 (so this
    run's dead result makes the NEW fail_count 2 == max_failures - 1).
    When refresh_links_write processes it.
    Then summary['purged'] == 0 (no purge below the threshold) and NO purge
    txn is opened at all (only the write-back txn).
    """
    row = _make_row(uid="0xB2", url="https://lcsc.com/struggling.pdf", fail_count=1)
    http_client = _FakeHttpClient(head_result=_FakeResponse(404))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock,
        max_failures=3,
    )

    assert summary["purged"] == 0, (
        f"AC-RL-10: new fail_count == max_failures - 1 must NOT purge. "
        f"Got summary: {summary!r}"
    )


def test_ac_rl_10_new_fail_count_strictly_greater_than_max_failures_still_purges() -> None:
    """AC-RL-10 (Gate 3: post-crash purge, forces >= not ==). Given
    max_failures=3 and a prior fail_count of 5 — a node STRANDED by a crash
    that committed the write-back but never reached the purge step in an
    earlier run, so its fail_count is already ABOVE the threshold. This
    run's dead result makes new_fail_count = 6, which is STRICTLY GREATER
    than max_failures (3), not equal to it.
    When refresh_links_write processes it.
    Then summary['purged'] == 1 — a naive `new_fail_count == max_failures`
    comparison would wrongly skip this (6 != 3) and leave the stranded,
    already-over-threshold Datasheet linked forever; only a
    `new_fail_count >= max_failures` comparison purges it. The written
    fail_count itself is still exactly 6 (prev=5 + 1), proving the
    increment is untouched by the threshold decision.
    """
    row = _make_row(uid="0xSTRANDED", url="https://lcsc.com/stranded.pdf", fail_count=5)
    http_client = _FakeHttpClient(head_result=_FakeResponse(500))
    read_txn = _make_reverse_lookup_read_txn("0xSTRANDED", [])
    write_txn = _make_txn()
    purge_txn = _make_txn()
    client = _make_refresh_client(read_txn, [write_txn, purge_txn])

    summary = refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock,
        max_failures=3,
    )

    assert summary["purged"] == 1, (
        f"AC-RL-10 (Gate 3): new_fail_count (6) STRICTLY GREATER than "
        f"max_failures (3) must still purge — this forces a `>=` "
        f"comparison, not `==`. Got summary: {summary!r}"
    )
    items = _written_payload_items(write_txn)
    assert items[0]["fail_count"] == 6, (
        f"AC-RL-10 (Gate 3): the written fail_count must still be exactly "
        f"prev(5) + 1 = 6, unaffected by the threshold decision. "
        f"Got: {items!r}"
    )


# ===========================================================================
# AC-RL-11: ordering (D2) — write-back committed in ONE txn, THEN purge
# in a SEPARATE txn
# ===========================================================================

def test_ac_rl_11_write_back_committed_before_purge_txn_mutates() -> None:
    """AC-RL-11: Given one alive row (no purge) and one dead row whose new
    fail_count crosses max_failures (purge required), processed in a single
    refresh_links_write call.
    When the call runs.
    Then: (a) the fail_count write-back for BOTH rows is committed in ONE
    txn, (b) that commit happens BEFORE the purge txn's mutate() is called,
    and (c) the purge mutation happens in a genuinely SEPARATE txn object.
    """
    alive_row = _make_row(uid="0xC1", url="https://lcsc.com/ok.pdf", fail_count=0)
    dying_row = _make_row(uid="0xC2", url="https://lcsc.com/dying.pdf", fail_count=1)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))

    order: list[str] = []

    write_txn = _make_txn()
    write_txn.commit.side_effect = lambda *a, **k: order.append("write_commit")

    purge_txn = _make_txn()
    purge_txn.mutate.side_effect = lambda *a, **k: order.append("purge_mutate")
    purge_txn.commit.side_effect = lambda *a, **k: order.append("purge_commit")

    read_txn = _make_reverse_lookup_read_txn("0xC2", ["0xP1"])
    client = _make_refresh_client(read_txn, [write_txn, purge_txn])

    # dying_row classified dead requires a SECOND http call (get/head per row);
    # script per-row responses via a client whose .head() alternates.
    responses = iter([_FakeResponse(200), _FakeResponse(404)])

    class _SequencedClient(_FakeHttpClient):
        def head(self, url, **kwargs):
            self.calls.append({"method": "HEAD", "url": url, "kwargs": kwargs})
            return next(responses)

    sequenced_client = _SequencedClient()

    refresh_links_write(
        iter([alive_row, dying_row]), client,
        http_client=sequenced_client, clock=_fixed_clock, max_failures=2,
    )

    assert order == ["write_commit", "purge_mutate", "purge_commit"], (
        f"AC-RL-11: the fail_count write-back must commit BEFORE the purge "
        f"txn mutates/commits, and they must be genuinely separate calls. "
        f"Observed order: {order!r}"
    )
    # Exactly one combined write-back mutate call covering BOTH rows.
    assert write_txn.mutate.call_count == 1, (
        f"AC-RL-11: both rows' fail_count updates must be written in ONE "
        f"mutate() call. Got {write_txn.mutate.call_count} calls."
    )
    written = _written_payload_items(write_txn)
    assert {item["uid"] for item in written} == {"0xC1", "0xC2"}, (
        f"AC-RL-11: the single write-back txn must cover both rows. "
        f"Got: {written!r}"
    )


# ===========================================================================
# Gate 3 (item 8): write-back/purge mutation failures must PROPAGATE, never
# be swallowed — this is what lets the CLI's try/except convert them into
# the path-free _REFRESH_DB_ERROR (asserted at the CLI level in
# test_cli_refresh_links.py). Mirrors embed_write's own _write_payload,
# which likewise lets a mutate()/commit() exception propagate untouched.
# ===========================================================================

def test_write_back_txn_mutate_exception_propagates_not_swallowed() -> None:
    """Gate 3 (item 8): Given the write-back txn's mutate() raises.
    When refresh_links_write is called.
    Then the exception PROPAGATES out of refresh_links_write (it is not
    caught/swallowed and no summary dict is silently returned instead).
    """
    row = _make_row(uid="0xW1", url="https://lcsc.com/x.pdf", fail_count=0)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    write_txn.mutate.side_effect = RuntimeError("connection refused")
    client = _make_simple_client(write_txn)

    with pytest.raises(RuntimeError, match="connection refused"):
        refresh_links_write(iter([row]), client, http_client=http_client, clock=_fixed_clock)


def test_purge_txn_mutate_exception_propagates_not_swallowed() -> None:
    """Gate 3 (item 8): Given the write-back txn succeeds but the SEPARATE
    purge txn's mutate() raises.
    When refresh_links_write is called for a row crossing the purge
    threshold.
    Then the exception PROPAGATES (not swallowed) — the write-back having
    already committed successfully does not mask a subsequent purge failure.
    """
    row = _make_row(uid="0xW2", url="https://lcsc.com/dying.pdf", fail_count=2)
    http_client = _FakeHttpClient(head_result=_FakeResponse(500))
    read_txn = _make_reverse_lookup_read_txn("0xW2", [])
    write_txn = _make_txn()
    purge_txn = _make_txn()
    purge_txn.mutate.side_effect = RuntimeError("connection refused")
    client = _make_refresh_client(read_txn, [write_txn, purge_txn])

    with pytest.raises(RuntimeError, match="connection refused"):
        refresh_links_write(
            iter([row]), client, http_client=http_client, clock=_fixed_clock,
            max_failures=3,
        )
    # The write-back itself must still have gone through before the purge
    # failure surfaced (ordering is unaffected by the later failure).
    assert write_txn.mutate.call_count == 1, (
        "Gate 3: the write-back mutate must have been attempted (and "
        "succeeded) before the purge failure propagates."
    )


# ===========================================================================
# AC-RL-12: multi-Part purge (D3)
# ===========================================================================

def test_ac_rl_12_purge_deletes_edge_from_every_part_and_the_datasheet_node() -> None:
    """AC-RL-12: Given a Datasheet (uid 0xD1) referenced by THREE Parts
    (0xP1, 0xP2, 0xP3) via the reverse '~datasheet' edge, and its new
    fail_count reaches max_failures.
    When refresh_links_write purges it.
    Then the purge txn's mutate() call carries a del_nquads payload
    containing EXACTLY one '<part_uid> <datasheet> <0xD1> .' triple per
    referencing Part (three total) PLUS one '<0xD1> * * .' node-delete
    triple — deleting the edge from every referencing Part AND the Datasheet
    node itself.
    """
    row = _make_row(uid="0xD1", url="https://lcsc.com/shared.pdf", fail_count=1)
    http_client = _FakeHttpClient(head_result=_FakeResponse(404))
    read_txn = _make_reverse_lookup_read_txn("0xD1", ["0xP1", "0xP2", "0xP3"])
    write_txn = _make_txn()
    purge_txn = _make_txn()
    client = _make_refresh_client(read_txn, [write_txn, purge_txn])

    refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock,
        max_failures=2,
    )

    assert purge_txn.mutate.call_count == 1, (
        f"AC-RL-12: exactly one purge mutate() call expected. "
        f"Got {purge_txn.mutate.call_count}."
    )
    _, kwargs = purge_txn.mutate.call_args
    del_nquads = kwargs.get("del_nquads")
    assert isinstance(del_nquads, str) and del_nquads, (
        f"AC-RL-12: purge mutate() must be called with a non-empty "
        f"'del_nquads' string payload. Got kwargs: {kwargs!r}"
    )

    for part_uid in ("0xP1", "0xP2", "0xP3"):
        expected_edge_triple = f"<{part_uid}> <datasheet> <0xD1> ."
        assert expected_edge_triple in del_nquads, (
            f"AC-RL-12: expected edge-delete triple {expected_edge_triple!r} "
            f"missing from del_nquads:\n{del_nquads}"
        )
    assert "<0xD1> * * ." in del_nquads, (
        f"AC-RL-12: expected the Datasheet node-delete triple "
        f"'<0xD1> * * .' in del_nquads:\n{del_nquads}"
    )
    # Exactly 3 edge-delete triples (no under/over-deletion of unrelated parts).
    edge_triple_count = del_nquads.count("<datasheet> <0xD1>")
    assert edge_triple_count == 3, (
        f"AC-RL-12: expected exactly 3 edge-delete triples (one per "
        f"referencing Part), got {edge_triple_count}:\n{del_nquads}"
    )


def test_ac_rl_12_on_purge_callback_receives_uid_fail_count_parts_unlinked() -> None:
    """AC-RL-12: Given the same shared-Datasheet purge scenario as above.
    When refresh_links_write is called with an on_purge callback.
    Then on_purge is invoked EXACTLY once with
    (datasheet_uid="0xD1", fail_count=2, parts_unlinked=3).
    """
    row = _make_row(uid="0xD1", url="https://lcsc.com/shared.pdf", fail_count=1)
    http_client = _FakeHttpClient(head_result=_FakeResponse(404))
    read_txn = _make_reverse_lookup_read_txn("0xD1", ["0xP1", "0xP2", "0xP3"])
    write_txn = _make_txn()
    purge_txn = _make_txn()
    client = _make_refresh_client(read_txn, [write_txn, purge_txn])

    on_purge = MagicMock()

    refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock,
        max_failures=2, on_purge=on_purge,
    )

    on_purge.assert_called_once_with("0xD1", 2, 3)


def test_ac_rl_12_on_purge_callback_args_are_path_free_and_exception_free() -> None:
    """AC-RL-12: Given the on_purge callback fires for a purge event.
    Then every argument passed to it is a plain primitive (str/int) — never
    an Exception instance, and no argument contains an operator filesystem
    path fragment or a raw traceback/exception string. This is the leaf's
    contribution toward the CLI's path-free destructive notice (the actual
    console text is rendered and asserted in test_cli_refresh_links.py).
    """
    import re

    row = _make_row(uid="0xD9", url="https://lcsc.com/shared2.pdf", fail_count=0)
    http_client = _FakeHttpClient(head_result=_FakeResponse(500))
    read_txn = _make_reverse_lookup_read_txn("0xD9", ["0xP9"])
    write_txn = _make_txn()
    purge_txn = _make_txn()
    client = _make_refresh_client(read_txn, [write_txn, purge_txn])

    captured: list[tuple] = []

    def _on_purge(*args):
        captured.append(args)

    refresh_links_write(
        iter([row]), client, http_client=http_client, clock=_fixed_clock,
        max_failures=1, on_purge=_on_purge,
    )

    assert len(captured) == 1
    for arg in captured[0]:
        assert isinstance(arg, (str, int)), (
            f"AC-RL-12: on_purge args must be plain str/int primitives, "
            f"never an Exception or complex object. Got: {arg!r} ({type(arg)})"
        )
        assert not isinstance(arg, BaseException), (
            f"AC-RL-12: on_purge must never receive a raw exception. Got: {arg!r}"
        )
        if isinstance(arg, str):
            assert not re.search(r"/(?:home|root|Users)/", arg), (
                f"AC-RL-12: on_purge argument must be path-free. Got: {arg!r}"
            )


# ===========================================================================
# AC-RL-13: idempotency (D6) — leaf-level de-duplication within one call
# ===========================================================================

def test_ac_rl_13_duplicate_uid_within_same_call_processed_once() -> None:
    """AC-RL-13: Given the SAME Datasheet uid appears TWICE within a single
    refresh_links_write call's input iterable (a defensive scenario — this
    should never happen given a correct uid keyset cursor at the CLI layer,
    see test_cli_refresh_links.py's AC-RL-4 cursor tests, but the leaf must
    not double-count even if it does).
    When refresh_links_write processes the duplicated input.
    Then summary['checked'] == 1 (not 2), and exactly ONE payload item for
    that uid is written (not two).
    """
    row_a = _make_row(uid="0xDUP", url="https://lcsc.com/dup.pdf", fail_count=0)
    row_b = _make_row(uid="0xDUP", url="https://lcsc.com/dup.pdf", fail_count=0)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    summary = refresh_links_write(
        iter([row_a, row_b]), client, http_client=http_client, clock=_fixed_clock
    )

    assert summary["checked"] == 1, (
        f"AC-RL-13: a duplicate uid within one call must be checked exactly "
        f"once, not double-counted. Got summary: {summary!r}"
    )
    items = _written_payload_items(write_txn)
    matching = [item for item in items if item["uid"] == "0xDUP"]
    assert len(matching) == 1, (
        f"AC-RL-13: exactly one payload item for the duplicated uid must be "
        f"written. Got: {items!r}"
    )


# ===========================================================================
# AC-RL-17: summary shape
# ===========================================================================

def test_ac_rl_17_summary_returns_exactly_checked_alive_dead_purged() -> None:
    """AC-RL-17: Given a mixed batch: 2 alive rows, 1 dead row (no purge),
    and 1 dead row whose new fail_count crosses max_failures (purge).
    When refresh_links_write processes the batch.
    Then it returns a dict with EXACTLY the keys
    {checked, alive, dead, purged} and correct counts:
    checked=4, alive=2, dead=2, purged=1.
    """
    rows = [
        _make_row(uid="0xE1", url="https://lcsc.com/1.pdf", fail_count=0),
        _make_row(uid="0xE2", url="https://lcsc.com/2.pdf", fail_count=0),
        _make_row(uid="0xE3", url="https://lcsc.com/3.pdf", fail_count=0),
        _make_row(uid="0xE4", url="https://lcsc.com/4.pdf", fail_count=2),
    ]
    responses = iter(
        [_FakeResponse(200), _FakeResponse(200), _FakeResponse(500), _FakeResponse(500)]
    )

    class _SequencedClient(_FakeHttpClient):
        def head(self, url, **kwargs):
            self.calls.append({"method": "HEAD", "url": url, "kwargs": kwargs})
            return next(responses)

    read_txn = _make_reverse_lookup_read_txn("0xE4", ["0xPX"])
    write_txn = _make_txn()
    purge_txn = _make_txn()
    client = _make_refresh_client(read_txn, [write_txn, purge_txn])

    summary = refresh_links_write(
        iter(rows), client, http_client=_SequencedClient(), clock=_fixed_clock,
        max_failures=3,
    )

    assert set(summary.keys()) == {"checked", "alive", "dead", "purged"}, (
        f"AC-RL-17: summary must have EXACTLY {{checked, alive, dead, purged}} "
        f"keys. Got: {summary!r}"
    )
    assert summary == {"checked": 4, "alive": 2, "dead": 2, "purged": 1}, (
        f"AC-RL-17: expected checked=4 alive=2 dead=2 purged=1. Got: {summary!r}"
    )


# ===========================================================================
# Module-level constant sanity (used directly by CLI wiring; see
# test_cli_refresh_links.py for CLI-side consumption).
# ===========================================================================

def test_default_constants_are_sane() -> None:
    """Given the module's default constants.
    Then DEFAULT_MAX_FAILURES == 3 and DEFAULT_TIMEOUT == 10.0, matching the
    design doc's stated defaults for `partgraph refresh-links`.
    """
    assert DEFAULT_MAX_FAILURES == 3
    assert DEFAULT_TIMEOUT == 10.0


def test_real_time_sleep_never_called_across_a_full_refresh_links_write_run() -> None:
    """Mandate check: Given a normal refresh_links_write run (no rate_limiter
    injected).
    Then real time.sleep is never invoked anywhere in the call.
    """
    row = _make_row(uid="0xZZ", url="https://lcsc.com/z.pdf", fail_count=0)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    with patch("time.sleep") as real_sleep:
        refresh_links_write(
            iter([row]), client, http_client=http_client, clock=_fixed_clock
        )
    assert not real_sleep.called, "real time.sleep must never be called."


def test_real_socket_never_opened_across_a_full_refresh_links_write_run() -> None:
    """Mandate check: Given a normal refresh_links_write run.
    Then socket.create_connection is never invoked (no real network I/O).
    """
    row = _make_row(uid="0xYY", url="https://lcsc.com/y.pdf", fail_count=0)
    http_client = _FakeHttpClient(head_result=_FakeResponse(200))
    write_txn = _make_txn()
    client = _make_simple_client(write_txn)

    with patch("socket.create_connection") as mock_conn:
        refresh_links_write(
            iter([row]), client, http_client=http_client, clock=_fixed_clock
        )
    assert not mock_conn.called, "real socket.create_connection must never be called."
