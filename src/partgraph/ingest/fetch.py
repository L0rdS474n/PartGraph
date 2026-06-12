"""Safe, resumable download of the JLCPCB/LCSC SQLite database (CDFER source).

:func:`fetch_cdfer` streams a (potentially ~1 GB) SQLite file to disk with a
set of security and robustness guarantees that are exercised by the unit suite:

- HTTPS-only: any non-``https`` scheme is rejected *before* any network call,
  preventing plaintext MITM exposure.
- Size cap: the ``Content-Length`` header is checked against ``max_bytes``
  before streaming, and the accumulated byte count is capped during streaming
  so a lying/short ``Content-Length`` cannot smuggle an oversized body.
- Magic-byte validation: the first 16 bytes must be the SQLite header
  (``b"SQLite format 3\\x00"``); otherwise the partial file is deleted and a
  descriptive error is raised.
- Atomicity: bytes are streamed to a ``.part`` temp file and only
  :func:`os.replace`-d into place on full success; an interrupted download
  never leaves a corrupt destination, and the ``.part`` file is always removed
  on failure.
- Idempotency: if the destination already exists and its size matches the
  server's ``Content-Length``, the download is skipped (unless ``force``).
- Testability: the HTTP client is injectable (``http_client``) so no real
  socket is opened in tests; a default :class:`httpx.Client` is used otherwise.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

__all__ = ["fetch_cdfer"]

# SQLite database files begin with the 16-byte string "SQLite format 3\x00".
# Built from parts so the constant is unambiguous and not mistaken for data.
_SQLITE_MAGIC: bytes = b"SQLite format 3" + bytes([0])
_MAGIC_LEN = len(_SQLITE_MAGIC)

# Default ceiling: 3 GiB. The real archive is ~1 GB; the cap guards against an
# accidental or hostile oversized response.
_DEFAULT_MAX_BYTES = 3 * 1024**3

# Streaming chunk size (bytes).
_CHUNK_SIZE = 1024 * 1024

# Network timeout for the default client (seconds). Generous read timeout to
# tolerate a slow ~1 GB transfer, but bounded so a hung connection cannot block
# forever. Applies only to the default httpx client; injected clients set their
# own policy.
_DEFAULT_TIMEOUT = 60.0

ProgressCallback = Callable[[int, "int | None"], None]


def _content_length(headers: Any) -> int | None:
    """Return the integer ``content-length`` from *headers*, or ``None``.

    Header lookup is case-insensitive where the mapping supports it (httpx and
    the test fakes both use lowercase keys). Malformed values yield ``None``.
    """
    if headers is None:
        return None
    value = None
    # Mapping-like access; tolerate dicts that only have lowercase keys.
    try:
        value = headers.get("content-length")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _probe_content_length(http_client: Any, url: str) -> int | None:
    """Issue a HEAD request and return the server's ``Content-Length``.

    Used only for the idempotency check. Any failure (no header, transport
    error) returns ``None`` so the caller falls back to a normal download
    rather than crashing.
    """
    try:
        with http_client.stream("HEAD", url) as resp:
            return _content_length(getattr(resp, "headers", None))
    except Exception:  # noqa: BLE001 — best-effort probe; never fatal here
        return None


def fetch_cdfer(  # noqa: PLR0913 — keyword-only options pinned by the test contract
    url: str,
    dest: Path,
    *,
    http_client: Any = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> str:
    """Download the SQLite database at *url* to *dest* safely and atomically.

    Args:
        url: HTTPS URL of the SQLite database. Non-HTTPS schemes are rejected
            before any I/O.
        dest: Destination file path. Its parent directory is created if needed.
        http_client: Optional injected HTTP client exposing a
            ``stream(method, url, **kwargs)`` context manager whose response has
            ``headers`` and ``iter_bytes(chunk_size)``. Defaults to a real
            :class:`httpx.Client`.
        force: If ``True``, always download even when a matching cached file
            exists.
        progress: Optional callback invoked as ``progress(received, total)``
            with monotonically non-decreasing ``received`` byte counts.
        max_bytes: Maximum allowed download size. Enforced both via the
            ``Content-Length`` header (before streaming) and the accumulated
            byte count (during streaming).

    Returns:
        A short human-readable status string. For a cache hit it contains the
        word "cached"; for a completed download it describes the result.

    Raises:
        ValueError: For a non-HTTPS scheme, or when the declared/streamed size
            exceeds ``max_bytes``.
        OSError / RuntimeError: For an invalid SQLite magic header or transport
            failures. On any failure the ``.part`` temp file is removed and the
            destination is left untouched.
    """
    dest = Path(dest)

    # --- 1. Scheme validation (BEFORE any network access) -------------------
    scheme = urlsplit(url).scheme.lower()
    if scheme != "https":
        raise ValueError(
            f"Refusing to download over non-HTTPS scheme {scheme!r}: {url!r}. "
            "Only https:// URLs are permitted for the component database "
            "download (plaintext transports expose the file to interception)."
        )

    owns_client = http_client is None
    if owns_client:
        import httpx  # noqa: PLC0415 — lazy import keeps the gRPC/HTTP stack optional

        http_client = httpx.Client(follow_redirects=True, timeout=_DEFAULT_TIMEOUT)

    try:
        # --- 2. Idempotency check -------------------------------------------
        if dest.exists() and not force:
            remote_len = _probe_content_length(http_client, url)
            if remote_len is not None and dest.stat().st_size == remote_len:
                return f"using cached file at {dest} ({remote_len} bytes)"

        # --- 3. Stream the body to a .part temp file ------------------------
        return _download_to_dest(
            http_client=http_client,
            url=url,
            dest=dest,
            progress=progress,
            max_bytes=max_bytes,
        )
    finally:
        if owns_client:
            http_client.close()


def _download_to_dest(
    *,
    http_client: Any,
    url: str,
    dest: Path,
    progress: ProgressCallback | None,
    max_bytes: int,
) -> str:
    """Stream the GET body into ``dest`` atomically with all safety checks."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # ".part" sidecar next to dest; with_name avoids any multi-suffix edge case.
    part_path = dest.with_name(dest.name + ".part")

    try:
        with http_client.stream("GET", url) as resp:
            declared = _content_length(getattr(resp, "headers", None))

            # 3a. Reject oversized declared length BEFORE streaming any bytes.
            if declared is not None and declared > max_bytes:
                raise ValueError(
                    f"Refusing to download {declared} bytes: exceeds the maximum "
                    f"allowed size of {max_bytes} bytes (Content-Length limit)."
                )

            received = 0
            head = bytearray()
            magic_checked = False

            with part_path.open("wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK_SIZE):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    received += len(chunk)

                    # 3b. Streaming cap: a short/lying Content-Length must not
                    # let an oversized body slip through.
                    if received > max_bytes:
                        raise ValueError(
                            f"Download exceeded the maximum allowed size of "
                            f"{max_bytes} bytes (streamed {received} bytes); "
                            "aborting (possible Content-Length spoofing)."
                        )

                    # 3c. Validate the SQLite magic header as soon as the first
                    # 16 bytes are available.
                    if not magic_checked and len(head) < _MAGIC_LEN:
                        head.extend(chunk[: _MAGIC_LEN - len(head)])
                        if len(head) >= _MAGIC_LEN:
                            magic_checked = True
                            if bytes(head) != _SQLITE_MAGIC:
                                raise ValueError(
                                    "Downloaded data is not a valid SQLite "
                                    "database: the file does not start with the "
                                    "expected SQLite format magic header. "
                                    "Refusing to use a corrupt or substituted "
                                    "download."
                                )

                    if progress is not None:
                        progress(received, declared)

            # Edge case: an empty or sub-16-byte body never satisfied the magic
            # check, so it cannot be a valid SQLite file.
            if not magic_checked:
                raise ValueError(
                    "Downloaded data is too short to be a valid SQLite database "
                    f"(received {received} bytes; need at least {_MAGIC_LEN} for "
                    "the SQLite format magic header)."
                )

        # --- 4. Atomic publish ---------------------------------------------
        os.replace(part_path, dest)
        return f"downloaded {received} bytes to {dest}"
    finally:
        # The .part file must never linger after a failure (no-op if absent).
        part_path.unlink(missing_ok=True)
