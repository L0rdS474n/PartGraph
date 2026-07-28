"""Datasheet link-rot checker (leaf module for ``partgraph refresh-links``).

This is a **leaf** module (issue #11, PR 1). It HTTP-checks datasheet URLs and
writes the freshness result back to Dgraph by ``uid``, auto-purging a link after
N consecutive failures. Like :mod:`partgraph.embed` it depends only on injected
seams — the ``http_client`` (mirroring :mod:`partgraph.ingest.fetch`'s
injectable-client discipline), the pydgraph ``client``, a wall-clock ``clock``
callable and, for per-host politeness, a monotonic ``clock``/``sleep`` pair on
:class:`HostRateLimiter`. It never opens a real socket, never contacts a real
Dgraph, and never reads the real wall clock, so the unit suite stays hermetic.

Security posture (Gate 3)
-------------------------
- **Validate-before-I/O URL policy** (:func:`is_checkable_url`): an
  allow-list of exactly ``{http, https}`` schemes, and a *fail-closed* host
  check that rejects any literal loopback/link-local/private/unspecified/
  reserved/multicast IP (via stdlib :mod:`ipaddress`) AND any numeric/hex/octal
  alt-encoding of such an address that ``ipaddress`` cannot even parse
  (``2130706433`` / ``0x7f000001`` / ``017700000001`` / ``127.1`` / ``0`` /
  ``0177.0.0.1``). A host that is not a valid public-IP literal is allowed only
  when it is a genuine dotted DNS hostname ending in an alphabetic TLD — the
  obfuscated-IP forms have no such TLD, so they fail closed rather than fall
  through as "just a hostname".
- **Outbound headers carry only the dedicated** :data:`USER_AGENT` — never an
  Authorization/Cookie/Proxy-Authorization (or any other) header. A link
  checker has no business forwarding a credential to a third-party host.
- **Purge is destructive and deliberate**: it runs in a SEPARATE transaction
  *after* the fail_count write-back has committed, deletes the ``datasheet``
  edge from every referencing Part (via the reverse ``~datasheet`` edge) plus
  the Datasheet node itself, and reports the event through the ``on_purge``
  callback with plain, path-free primitives only. Every uid interpolated into
  raw n-quad/query text is shape-validated first (no injection vector).
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_MAX_FAILURES",
    "DEFAULT_TIMEOUT",
    "USER_AGENT",
    "HostRateLimiter",
    "classify_url",
    "format_verified_at",
    "is_checkable_url",
    "refresh_links_write",
]

#: Dedicated User-Agent sent on every datasheet HTTP check. Deliberately a
#: plain, descriptive product token — NOT a library default (httpx/requests) and
#: carrying no credential, PII or internal hostname — so a datasheet host sees an
#: honest, attributable identity and nothing sensitive leaks outbound.
USER_AGENT = "PartGraph-LinkChecker/1.0"

#: Default consecutive-failure threshold at/above which a link is auto-purged.
DEFAULT_MAX_FAILURES = 3

#: Default per-request HTTP timeout (seconds) for a single datasheet check.
DEFAULT_TIMEOUT = 10.0

#: HTTP status classification bounds. 2xx and 3xx (``200 <= s < 400``) are
#: "alive" (the URL is served, redirects included); ``>= 400`` is "dead".
#: Named constants keep the comparison magic-value-free.
_HTTP_ALIVE_MIN = 200
_HTTP_ALIVE_MAX = 400

#: Statuses for which a HEAD probe is retried once as a GET (some hosts reject
#: HEAD with 405 Method Not Allowed / 501 Not Implemented yet serve GET fine).
_GET_FALLBACK_STATUSES = frozenset({405, 501})

#: Allow-list of permitted URL schemes (exactly http/https; everything else —
#: file/ftp/gopher/data/sftp/ws/jar/dict/... — is rejected before any I/O).
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: A genuine top-level domain is alphabetic and at least two characters. Used to
#: distinguish a real dotted hostname (``lcsc.com``) from an obfuscated numeric
#: IP form that :mod:`ipaddress` cannot parse (``127.1`` -> TLD ``"1"``).
_TLD_RE = re.compile(r"[a-zA-Z]{2,}")

#: A genuine DNS hostname has at least two dot-separated labels (name + TLD); a
#: single-label host (``2130706433`` / ``0x7f000001`` / ``0``) fails closed.
_MIN_HOSTNAME_LABELS = 2

#: Shape a uid must match before it is interpolated into raw n-quad / query
#: text (validate-before-interpolate; mirrors the repo's ADR-INJECT convention).
#: Broad enough for real Dgraph uids (``0x`` + hex) while excluding whitespace,
#: angle brackets, quotes and every other n-quad/DQL metacharacter, so no
#: untrusted value can break out of ``<...>`` or a ``uid(...)`` clause.
#: Anchored with ``\Z``, never ``$``: Python's ``$`` also matches just before a
#: trailing newline, which would admit ``"0x1a\n"`` into n-quad/query text.
_UID_SAFE_RE = re.compile(r"^0x[0-9A-Za-z]+\Z")


class HostRateLimiter:
    """Per-host politeness limiter (injected monotonic ``clock``/``sleep``).

    Two ``acquire(host)`` calls for the SAME host within ``min_interval`` seconds
    pause (via the injected ``sleep``) for the remaining time; different hosts
    never rate-limit each other, and the first call for any host never sleeps.
    The ``clock``/``sleep`` pair is injected (monotonic seconds) so the unit
    suite drives it deterministically and real :func:`time.sleep` is never
    touched here.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._min_interval = float(min_interval)
        self._clock = clock
        self._sleep = sleep
        self._last_seen: dict[str, float] = {}

    def acquire(self, host: str) -> None:
        """Block (via the injected ``sleep``) until *host* may be contacted."""
        # Exactly one clock read per acquire (tests inject a finite clock iter).
        now = self._clock()
        last = self._last_seen.get(host)
        if last is not None:
            wait = self._min_interval - (now - last)
            if wait > 0:
                self._sleep(wait)
                now = now + wait
        self._last_seen[host] = now


def is_checkable_url(url: str) -> bool:
    """Return ``True`` iff *url* is safe to HTTP-check (validate-before-I/O).

    Fail-closed policy: the scheme must be exactly ``http``/``https``, and the
    host must be either a genuine public-IP literal (not loopback/link-local/
    private/unspecified/reserved/multicast) or a genuine dotted DNS hostname
    ending in an alphabetic TLD. A host that merely *looks* numeric/hex/octal
    but that :mod:`ipaddress` refuses to parse (a classic SSRF-bypass encoding
    of a loopback address) is rejected, never treated as a plain hostname.
    """
    if not isinstance(url, str) or not url:
        return False
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    host = parts.hostname
    if not host:
        return False
    host = host.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Not a parseable IP literal: allow ONLY a genuine dotted hostname.
        # Fail closed on numeric/hex/octal obfuscated-IP forms (no alpha TLD).
        return _is_plausible_hostname(host)
    return not _is_forbidden_ip(address)


def _is_forbidden_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return ``True`` for any non-public literal IP (SSRF target class)."""
    return bool(
        address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_unspecified
        or address.is_reserved
        or address.is_multicast
    )


def _is_plausible_hostname(host: str) -> bool:
    """Return ``True`` iff *host* is a genuine dotted DNS name (alpha TLD).

    Requires at least two non-empty dot-separated labels whose rightmost label
    (the TLD) is purely alphabetic — which every obfuscated numeric-IP form
    fails (they have no dot, or a numeric last label), so they fail closed.
    """
    labels = host.split(".")
    if len(labels) < _MIN_HOSTNAME_LABELS or any(not label for label in labels):
        return False
    return bool(_TLD_RE.fullmatch(labels[-1]))


def classify_url(
    url: str,
    http_client: Any,
    *,
    timeout: float,
) -> tuple[bool, int]:
    """Return ``(alive, http_status)`` for *url* via the injected *http_client*.

    Issues a HEAD first; only on 405/501 does it retry once as a GET. A 2xx/3xx
    status is alive; a 4xx/5xx is dead. Any transport failure (timeout/TLS/
    connect) is caught and reported as ``(False, 0)`` — never propagated — so a
    single dead link never aborts the run. The dedicated :data:`USER_AGENT` is
    the ONLY header sent (no credential-like header is ever forwarded).
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        response = http_client.head(url, headers=headers, timeout=timeout)
    except Exception:  # noqa: BLE001 — any transport failure classifies dead.
        return (False, 0)
    status = _status_of(response)
    if status in _GET_FALLBACK_STATUSES:
        try:
            response = http_client.get(url, headers=headers, timeout=timeout)
        except Exception:  # noqa: BLE001 — a failed GET fallback is dead too.
            return (False, 0)
        status = _status_of(response)
    alive = _HTTP_ALIVE_MIN <= status < _HTTP_ALIVE_MAX
    return (alive, status)


def _status_of(response: Any) -> int:
    """Return the integer ``status_code`` of *response*, defaulting to 0."""
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def format_verified_at(moment: datetime) -> str:
    """Return the deterministic RFC-3339 UTC (``...Z``) string for *moment*.

    A naive datetime is treated as UTC; a tz-aware one is converted to UTC. The
    result is byte-stable for a given instant (no wall-clock or locale leak), so
    the stamp is reproducible across runs.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh_links_write(  # noqa: PLR0913 — the keyword-only seams are the AC contract.
    datasheets_iter: Any,
    client: Any,
    *,
    http_client: Any,
    clock: Callable[[], datetime],
    max_failures: int = DEFAULT_MAX_FAILURES,
    timeout: float = DEFAULT_TIMEOUT,
    rate_limiter: HostRateLimiter | None = None,
    on_purge: Callable[[str, int, int], None] | None = None,
) -> dict:
    """Check each datasheet link and write the freshness result back by uid.

    For every unique Datasheet row: apply the URL policy, (optionally) wait on
    the per-host *rate_limiter*, HTTP-classify the link, and stage a narrow
    ``{uid, verified_at, http_status, fail_count}`` write-back — ``fail_count``
    reset to 0 when alive, else ``prev + 1`` (a missing/``None`` prior counts as
    0). All rows are written back in ONE committed transaction; then, in a
    SEPARATE transaction per crossed threshold, a link whose new ``fail_count >=
    max_failures`` is purged (edge removed from every referencing Part + the
    Datasheet node deleted) and reported via *on_purge*.

    A uid appearing twice within one call is processed exactly once (defensive
    de-duplication). Write-back and purge mutation failures propagate unchanged
    so the CLI can convert them into a single path-free error.

    Returns exactly ``{"checked": int, "alive": int, "dead": int, "purged":
    int}``. Per-purge detail (uid, fail_count, #parts unlinked) is delivered via
    *on_purge*, never carried in this summary dict.
    """
    verified_at = format_verified_at(clock())
    payload: list[dict[str, Any]] = []
    alive_count = 0
    purges: list[tuple[str, int]] = []
    seen: set[str] = set()

    for row in datasheets_iter:
        uid = getattr(row, "uid", None)
        if not isinstance(uid, str) or not uid or uid in seen:
            continue
        seen.add(uid)

        alive, status = _check_row(
            row, http_client=http_client, timeout=timeout, rate_limiter=rate_limiter
        )
        prev_failures = _coerce_fail_count(row)
        new_failures = 0 if alive else prev_failures + 1

        payload.append(
            {
                "uid": uid,
                "verified_at": verified_at,
                "http_status": status,
                "fail_count": new_failures,
            }
        )
        if alive:
            alive_count += 1
        elif new_failures >= max_failures:
            purges.append((uid, new_failures))

    checked = len(payload)

    # (D2) Write-back for ALL rows commits in ONE transaction BEFORE any purge.
    if payload:
        _write_back(client, payload)

    # (D3) Each purge runs in its own SEPARATE transaction, after the commit.
    purged = 0
    for datasheet_uid, fail_count in purges:
        if not _UID_SAFE_RE.match(datasheet_uid):
            # Never interpolate an unsafe uid into query/n-quad text.
            continue
        parts_unlinked = _purge_datasheet(client, datasheet_uid)
        purged += 1
        if on_purge is not None:
            on_purge(datasheet_uid, fail_count, parts_unlinked)

    return {
        "checked": checked,
        "alive": alive_count,
        "dead": checked - alive_count,
        "purged": purged,
    }


def _check_row(
    row: Any,
    *,
    http_client: Any,
    timeout: float,
    rate_limiter: HostRateLimiter | None,
) -> tuple[bool, int]:
    """Return ``(alive, http_status)`` for one row, applying the URL policy.

    An unsafe URL (policy rejection) is a definitive dead result with status 0
    and issues ZERO HTTP calls. For a checkable URL the per-host *rate_limiter*
    (when supplied) is acquired STRICTLY BEFORE the HTTP check.
    """
    url = getattr(row, "url", None)
    if not isinstance(url, str) or not is_checkable_url(url):
        return (False, 0)
    if rate_limiter is not None:
        rate_limiter.acquire(_host_of(url))
    return classify_url(url, http_client, timeout=timeout)


def _host_of(url: str) -> str:
    """Return the lowercase host of *url* (``""`` when absent)."""
    return (urlsplit(url).hostname or "").lower()


def _coerce_fail_count(row: Any) -> int:
    """Return the row's prior ``fail_count`` as an int, treating None/absent as 0.

    ``fail_count`` is a NEW predicate, so a pre-existing Datasheet row carries it
    unset on the first run ever — coercing missing/``None`` to 0 keeps ``dead ->
    prev + 1`` from crashing on ``None + 1``.
    """
    value = getattr(row, "fail_count", None)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _write_back(client: Any, payload: list[dict[str, Any]]) -> None:
    """Write the narrow uid write-back for all rows in one committed txn.

    A ``mutate``/``commit`` failure propagates untouched (never swallowed) so the
    CLI's try/except can convert it into a single path-free error, mirroring
    :func:`partgraph.embed._write_payload`.
    """
    txn = client.txn()
    try:
        txn.mutate(set_obj=payload)
        txn.commit()
    finally:
        txn.discard()


def _purge_datasheet(client: Any, datasheet_uid: str) -> int:
    """Delete *datasheet_uid* and its edge from every referencing Part.

    Returns the number of Parts unlinked. The reverse ``~datasheet`` lookup finds
    every referencing Part; the delete mutation carries one
    ``<part_uid> <datasheet> <datasheet_uid> .`` triple per Part plus one
    ``<datasheet_uid> * * .`` node-delete triple (the RDF ``<uid> * * .`` node
    delete is the only in-repo node-deletion precedent — see
    ``tests/conftest.py``). A ``mutate``/``commit`` failure propagates untouched.
    """
    part_uids = _reverse_lookup_parts(client, datasheet_uid)
    lines = [
        f"<{part_uid}> <datasheet> <{datasheet_uid}> ." for part_uid in part_uids
    ]
    lines.append(f"<{datasheet_uid}> * * .")
    del_nquads = "\n".join(lines)

    txn = client.txn()
    try:
        txn.mutate(del_nquads=del_nquads)
        txn.commit()
    finally:
        txn.discard()
    return len(part_uids)


def _reverse_lookup_parts(client: Any, datasheet_uid: str) -> list[str]:
    """Return the uids of Parts referencing *datasheet_uid* via ``~datasheet``.

    A read-only lookup (always discarded). Only shape-validated part uids are
    returned, so no untrusted value can later reach raw n-quad text.
    """
    query = f"{{ q(func: uid({datasheet_uid})) {{ ~datasheet {{ uid }} }} }}"
    txn = client.txn(read_only=True)
    try:
        response = txn.query(query)
        raw = getattr(response, "json", None)
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else {}
    finally:
        txn.discard()

    part_uids: list[str] = []
    if not isinstance(data, dict):
        return part_uids
    for node in data.get("q", []) or []:
        if not isinstance(node, dict):
            continue
        for referrer in node.get("~datasheet", []) or []:
            uid = referrer.get("uid") if isinstance(referrer, dict) else None
            if isinstance(uid, str) and _UID_SAFE_RE.match(uid):
                part_uids.append(uid)
    return part_uids
