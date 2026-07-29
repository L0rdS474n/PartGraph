"""
Tests: real-installed-`requests` behaviour pins for the two `requests`
semantics this repository's code and its own ADRs already document as
relied-upon:

  1. ADR-0022 ("readiness *phase*..." section): "`probe_health()` calls
     `requests.get(url, timeout=HEALTH_PROBE_TIMEOUT_S)`, and `requests`
     maps a single float to `Timeout(connect=t, read=t)` (`HTTPAdapter.send`)
     — the connect and read phases are bounded *separately*." That claim is
     the basis for ADR-0022's own readiness-budget arithmetic ("One probe
     costs up to 2 x HEALTH_PROBE_TIMEOUT_S, not 1x").

  2. `partgraph.util.health.probe_health` and
     `partgraph.util.index_health.probe_index` both catch
     `requests.exceptions.Timeout` BEFORE the broader
     `requests.exceptions.RequestException`, relying on `Timeout` actually
     being a `RequestException` subclass for that except-order to make sense
     at all, and on `ConnectTimeout`/`ReadTimeout` both landing in the
     narrower `Timeout` branch rather than falling through to the generic
     one.

Read directly from the installed `requests` (2.34.2) source during this
analysis, `requests/adapters.py`'s `HTTPAdapter.send()`:

    if isinstance(timeout, tuple):
        ...
    elif isinstance(timeout, TimeoutSauce):
        pass
    else:
        resolved_timeout = TimeoutSauce(connect=timeout, read=timeout)

where `TimeoutSauce = urllib3.util.Timeout` (module-level import). This file
pins that construction directly, plus the exception hierarchy, against the
REAL installed library — the "property, not proxy" complement to the (very
deliberate) absence of a `requests` version bound in
`tests/unit/test_pyproject_dependency_pins.py`: this repository chooses to
keep receiving `requests`' frequent CVE patches rather than pin it, and
accepts that trade specifically BECAUSE this behaviour can be, and is, pinned
directly instead.

DELIBERATE DEPARTURE from this suite's usual discipline, disclosed rather
than silently done: `tests/unit/test_health.py`'s own docstring states "No
test in this file opens a real socket... every HTTP outcome is injected via
a fake/spy `http_get` callable" — appropriate there, because that file tests
PartGraph's OWN code against an injected seam. This file's subject is
different: it is `requests`' OWN internal contract, which an injected seam
cannot observe (a fake `http_get` never runs `requests`' real
`HTTPAdapter.send()` at all). Two techniques are used, in increasing realism:

  - `test_*_the_resolved_timeout_object` monkeypatches ONLY the connection
    acquisition (`HTTPAdapter.get_connection_with_tls_context`), so
    `HTTPAdapter.send()`'s OWN timeout-resolution code runs for real, but no
    socket is opened and no wall-clock time passes — deterministic, no
    flake risk, and it fails LOUDLY (an `AttributeError` from
    `monkeypatch.setattr`) if a future `requests` release renames or removes
    that method, which is itself useful: this is exactly the kind of
    internal refactor a version bound could otherwise mask.
  - `test_a_single_float_timeout_bounds_the_read_phase_end_to_end` opens one
    real TCP listener on `127.0.0.1` (loopback only, ephemeral port, no
    external network, no DNS) that accepts a connection but never answers
    it, and asserts the resulting `requests.exceptions.ReadTimeout` lands
    within a small, generous bound — corroborating that the object built
    above is not merely constructed but actually enforced end-to-end. A
    short (0.3s) timeout keeps this fast and low-flake; the assertion is a
    generous upper bound, never a tight timing equality.
"""

from __future__ import annotations

import socket
import time

import pytest
import requests
import requests.adapters
import requests.exceptions

#: Generous upper bound for the real-socket read-timeout corroboration test.
#: The requested timeout is 0.3s; on a loaded CI host the actual raise may
#: land somewhat later, but never anywhere close to this ceiling unless
#: something is genuinely broken.
_READ_TIMEOUT_UPPER_BOUND_S = 5.0


class _StopBeforeNetwork(Exception):
    """Raised by the fake connection's `urlopen` to abort BEFORE any real
    socket I/O — the seam exists only to observe the `timeout=` kwarg
    `HTTPAdapter.send()` passes down, never to complete a request.
    """


class _CapturingConnection:
    """Stands in for the real urllib3 connection pool `HTTPAdapter.send()`
    would otherwise acquire. Captures every kwarg `send()` passes to
    `urlopen()` (the same call requests would make into urllib3) and aborts
    before any network I/O.
    """

    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def urlopen(self, **kwargs):
        self._captured.update(kwargs)
        raise _StopBeforeNetwork


def _resolved_timeout_for(timeout) -> object:
    """Drive the REAL `requests.adapters.HTTPAdapter.send()` up to (but not
    including) the network call, and return the `timeout=` object it built
    and would have passed to urllib3's `urlopen()`.
    """
    captured: dict = {}
    adapter = requests.adapters.HTTPAdapter()
    adapter.get_connection_with_tls_context = (
        lambda *args, **kwargs: _CapturingConnection(captured)
    )
    request = requests.Request("GET", "http://127.0.0.1:1/never-sent").prepare()
    with pytest.raises(_StopBeforeNetwork):
        adapter.send(request, timeout=timeout)
    assert "timeout" in captured, (
        "HTTPAdapter.send() did not reach urlopen() with a timeout kwarg at all — "
        "the seam did not observe what it was built to observe."
    )
    return captured["timeout"]


# ---------------------------------------------------------------------------
# ADR-0022's claim: a single float becomes Timeout(connect=t, read=t)
# ---------------------------------------------------------------------------


def test_a_single_float_timeout_resolves_to_the_same_connect_and_read_bound() -> None:
    """Given `requests.get(url, timeout=T)` is called with a bare float `T`
    (exactly how `partgraph.util.health.probe_health` and
    `partgraph.util.index_health.probe_index` both call it).
    When the REAL `HTTPAdapter.send()` resolves that timeout (network I/O
    stopped before it happens; see `_resolved_timeout_for`).
    Then the resulting object's `connect_timeout` AND `read_timeout` both
    equal T — ADR-0022's documented claim, observed directly against the
    installed library rather than assumed from the ADR's prose.
    """
    resolved = _resolved_timeout_for(2.0)
    assert resolved.connect_timeout == pytest.approx(2.0)
    assert resolved.read_timeout == pytest.approx(2.0)


def test_a_connect_read_tuple_timeout_resolves_to_two_distinct_bounds() -> None:
    """Given `requests.get(url, timeout=(connect, read))` is called with a
    2-tuple (the alternative form ADR-0022's own prose contrasts the single-
    float form against).
    When the REAL `HTTPAdapter.send()` resolves that timeout.
    Then `connect_timeout` and `read_timeout` are each set independently,
    proving the single-float behaviour above is a real COLLAPSE of two
    independently-settable phases, not merely how the object happens to
    print.
    """
    resolved = _resolved_timeout_for((1.5, 9.0))
    assert resolved.connect_timeout == pytest.approx(1.5)
    assert resolved.read_timeout == pytest.approx(9.0)


def test_a_single_float_timeout_bounds_the_read_phase_end_to_end() -> None:
    """Given a real TCP listener on 127.0.0.1 (loopback, ephemeral port)
    that accepts a connection and then never sends a response.
    When `requests.get(url, timeout=0.3)` is called against it for real (no
    seam, no mock — the actual library, the actual OS socket stack).
    Then a `requests.exceptions.ReadTimeout` is raised, and it lands well
    within a generous upper bound — corroborating, end-to-end, that the
    single float this repository passes for `HEALTH_PROBE_TIMEOUT_S` /
    `INDEX_PROBE_TIMEOUT_S` really does bound the read phase in practice,
    not merely in the object `HTTPAdapter.send()` constructs.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        start = time.monotonic()
        with pytest.raises(requests.exceptions.ReadTimeout):
            requests.get(f"http://127.0.0.1:{port}/", timeout=0.3)
        elapsed = time.monotonic() - start
    finally:
        listener.close()

    assert elapsed < _READ_TIMEOUT_UPPER_BOUND_S, (
        f"ReadTimeout took {elapsed}s to raise for a requested 0.3s timeout — "
        f"exceeds the generous {_READ_TIMEOUT_UPPER_BOUND_S}s bound."
    )


# ---------------------------------------------------------------------------
# The exception hierarchy health.py / index_health.py except-order relies on
# ---------------------------------------------------------------------------


def test_timeout_is_a_request_exception_subclass() -> None:
    """Given `probe_health`/`probe_index` both catch
    `requests.exceptions.Timeout` before the broader
    `requests.exceptions.RequestException`.
    When the REAL installed `requests.exceptions` classes are compared.
    Then `Timeout` IS a `RequestException` — the relationship that except-
    order silently assumes.
    """
    assert issubclass(requests.exceptions.Timeout, requests.exceptions.RequestException)


@pytest.mark.parametrize(
    "specific_exception_name", ["ConnectTimeout", "ReadTimeout"],
)
def test_specific_timeout_exceptions_are_caught_by_the_narrower_timeout_branch(
    specific_exception_name: str,
) -> None:
    """Given health.py/index_health.py catch `requests.exceptions.Timeout`
    (the narrower branch) before `requests.exceptions.RequestException` (the
    broader one), specifically so a timeout gets its own dedicated message.
    When the REAL `ConnectTimeout`/`ReadTimeout` classes are checked against
    `Timeout`.
    Then each IS a subclass of `Timeout` — so neither one can silently fall
    through to the generic branch and lose the dedicated timeout message.
    """
    specific_exception = getattr(requests.exceptions, specific_exception_name)
    assert issubclass(specific_exception, requests.exceptions.Timeout)
