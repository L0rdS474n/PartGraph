"""
Tests: T-FETCH-*

Verifies partgraph.ingest.fetch.fetch_cdfer behaviour:
- T-FETCH-https:       non-HTTPS schemes are rejected before any network call.
- T-FETCH-idempotent:  existing complete file (size sentinel matches) + no force
                       skips download and reports "cached".
- T-FETCH-atomic:      a simulated partial body leaves the final path absent or
                       unchanged; the .part temp file is used then renamed.
- T-FETCH-progress:    fake streaming body invokes the progress callback with
                       monotonically non-decreasing byte counts.
- T-FETCH-no-network:  the injectable http_client parameter exists on the
                       function signature, proving no real socket is needed in
                       tests.
- T-FETCH-size-limit:  Content-Length exceeding max_bytes raises ValueError
                       before streaming; lying Content-Length still capped during
                       streaming.
- T-FETCH-magic:       downloaded bytes not starting with the SQLite magic header
                       raise a descriptive error with no dest or .part file left.

NOTE: Collection will ERROR if partgraph.ingest.fetch does not yet exist.
That is the expected red state before implementation.
"""

from __future__ import annotations

import inspect
import pathlib
import typing
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from partgraph.ingest.fetch import fetch_cdfer  # noqa: F401 — expected ImportError if not yet implemented


# ---------------------------------------------------------------------------
# SQLite magic header — built at runtime so source scanners cannot mistake it
# for a real DB file comment or binary blob.
# b"SQLite format 3" + 0x00
# ---------------------------------------------------------------------------

_SQLITE_MAGIC: bytes = b"SQLite format 3" + bytes([0])  # 16 bytes


def _make_sqlite_body(size: int = 512) -> bytes:
    """Return a fake body that begins with the SQLite magic header."""
    assert size >= len(_SQLITE_MAGIC), "Body must be at least 16 bytes"
    return _SQLITE_MAGIC + b"\x00" * (size - len(_SQLITE_MAGIC))


# ---------------------------------------------------------------------------
# Helpers / fake HTTP client
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal fake HTTP response that supports streaming iteration."""

    def __init__(
        self,
        content_length: int | None,
        chunks: list[bytes],
        status_code: int = 200,
    ) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self._chunks = chunks
        self._iter_bytes_called = False

    def iter_bytes(self, chunk_size: int = 8192):  # noqa: ARG002
        self._iter_bytes_called = True
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _FakeHttpClient:
    """Injectable HTTP client that never opens a real socket."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs) -> _FakeResponse:  # noqa: ARG002
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return self._response


# ---------------------------------------------------------------------------
# T-FETCH-https
# ---------------------------------------------------------------------------

def test_fetch_https_scheme_non_https_rejected() -> None:
    """Given a URL with a non-HTTPS scheme (http://).
    When fetch_cdfer is called.
    Then it raises ValueError (or similar) before any network call is made
    and the injectable client receives zero calls.

    Security requirement: HTTPS-only transport; plaintext HTTP is forbidden
    because it would expose the download to MITM interception on any network.
    """
    bad_urls = [
        "http://example.com/file.zip",
        "ftp://example.com/file.zip",
        "file:///etc/passwd",
    ]
    for url in bad_urls:
        spy_client = MagicMock()
        with pytest.raises((ValueError, TypeError)):
            fetch_cdfer(url, pathlib.Path("/tmp/out.zip"), http_client=spy_client)
        assert not spy_client.stream.called, (
            f"HTTP client was called despite non-HTTPS URL {url!r}; "
            "scheme validation must occur before any I/O."
        )


# ---------------------------------------------------------------------------
# T-FETCH-idempotent
# ---------------------------------------------------------------------------

def test_fetch_idempotent_existing_complete_file_skipped(tmp_path: pathlib.Path) -> None:
    """Given an existing destination file whose size matches the server's
    Content-Length header, and force=False.
    When fetch_cdfer is called.
    Then no download is performed (the http_client receives zero stream calls)
    and the return value / console message indicates the file was cached.
    """
    dest = tmp_path / "component.sqlite3"
    # Write a synthetic 'complete' file beginning with the SQLite magic header.
    fake_content = _make_sqlite_body(512)
    dest.write_bytes(fake_content)

    # Fake server reports the same length as the local file.
    fake_resp = _FakeResponse(
        content_length=len(fake_content),
        chunks=[fake_content],
    )
    client = _FakeHttpClient(fake_resp)

    result = fetch_cdfer(
        "https://example.com/component.sqlite3",
        dest,
        http_client=client,
        force=False,
    )

    # The client must NOT have been called (no HEAD/GET for actual body).
    # (A single HEAD request to discover Content-Length is acceptable;
    # a streaming GET body fetch is not.)
    body_fetch_calls = [c for c in client.calls if c["method"].upper() == "GET"]
    assert not body_fetch_calls, (
        f"Expected zero GET calls for a cached file, got: {client.calls}"
    )
    # Return value (if any string) should indicate cache hit.
    if isinstance(result, str):
        assert "cach" in result.lower() or "skip" in result.lower(), (
            f"Return value should mention cache/skip for idempotent case, got: {result!r}"
        )


def test_fetch_force_flag_bypasses_idempotency(tmp_path: pathlib.Path) -> None:
    """Given an existing complete file and force=True.
    When fetch_cdfer is called.
    Then a download IS performed (stream GET call is made).
    """
    dest = tmp_path / "component.sqlite3"
    fake_content = _make_sqlite_body(256)
    dest.write_bytes(fake_content)

    fake_resp = _FakeResponse(
        content_length=len(fake_content),
        chunks=[fake_content],
    )
    client = _FakeHttpClient(fake_resp)

    fetch_cdfer(
        "https://example.com/component.sqlite3",
        dest,
        http_client=client,
        force=True,
    )

    get_calls = [c for c in client.calls if c["method"].upper() == "GET"]
    assert get_calls, (
        "Expected at least one GET call when force=True, even if the file exists."
    )


# ---------------------------------------------------------------------------
# T-FETCH-atomic
# ---------------------------------------------------------------------------

def test_fetch_atomic_partial_body_leaves_dest_unchanged(tmp_path: pathlib.Path) -> None:
    """Given a simulated partial/interrupted body (streaming raises midway).
    When fetch_cdfer is called.
    Then the original destination path is absent or unchanged from before the call.

    The implementation must write to a .part temp file and only rename on
    successful completion; an interrupted download must not corrupt the
    destination.
    """

    class _PartialResponse:
        """Raises mid-stream to simulate a connection drop."""
        headers: typing.ClassVar[dict] = {"content-length": "1024"}
        status_code = 200

        def iter_bytes(self, chunk_size: int = 8192):  # noqa: ARG002
            yield b"partial_data_"
            raise ConnectionError("simulated drop")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    class _PartialClient:
        calls: typing.ClassVar[list] = []

        def stream(self, method: str, url: str, **kwargs):  # noqa: ARG002
            self.calls.append(method)
            return _PartialResponse()

    dest = tmp_path / "db.sqlite3"
    assert not dest.exists()

    client = _PartialClient()

    with pytest.raises(Exception):  # noqa: B017 — any exception from partial download
        fetch_cdfer(
            "https://example.com/db.sqlite3",
            dest,
            http_client=client,
        )

    assert not dest.exists(), (
        "Destination file must NOT exist after an interrupted download. "
        "The implementation must write to a .part temp file and only rename on success."
    )


def test_fetch_atomic_temp_part_file_not_left_on_success(tmp_path: pathlib.Path) -> None:
    """Given a successful download with a valid SQLite magic header.
    When fetch_cdfer completes.
    Then no .part temporary file is left alongside the destination.
    """
    content = _make_sqlite_body(128)
    fake_resp = _FakeResponse(content_length=len(content), chunks=[content])
    client = _FakeHttpClient(fake_resp)
    dest = tmp_path / "complete.sqlite3"

    fetch_cdfer("https://example.com/complete.sqlite3", dest, http_client=client)

    part_files = list(tmp_path.glob("*.part"))
    assert not part_files, (
        f".part temp files left after successful download: {part_files}"
    )
    assert dest.exists(), "Destination must exist after a successful download."


# ---------------------------------------------------------------------------
# T-FETCH-progress
# ---------------------------------------------------------------------------

def test_fetch_progress_callback_called_monotonically() -> None:
    """Given a fake streaming response with multiple chunks beginning with the
    SQLite magic header.
    When fetch_cdfer is called with a progress callback.
    Then the callback is invoked with monotonically non-decreasing byte-received
    counts on every call.
    """
    import tempfile

    # First chunk must start with the magic header so magic validation passes.
    first_chunk = _SQLITE_MAGIC + b"X" * (100 - len(_SQLITE_MAGIC))
    chunks = [first_chunk, b"Y" * 200, b"Z" * 300]
    total = sum(len(c) for c in chunks)
    fake_resp = _FakeResponse(content_length=total, chunks=chunks)
    client = _FakeHttpClient(fake_resp)

    observed: list[int] = []

    def _progress(received: int, total_size: int | None) -> None:  # noqa: ARG001
        observed.append(received)

    with tempfile.TemporaryDirectory() as td:
        dest = pathlib.Path(td) / "out.sqlite3"
        fetch_cdfer(
            "https://example.com/out.sqlite3",
            dest,
            http_client=client,
            progress=_progress,
        )

    assert observed, "Progress callback was never invoked."
    for i in range(1, len(observed)):
        assert observed[i] >= observed[i - 1], (
            f"Progress callback decreased at index {i}: "
            f"{observed[i-1]} -> {observed[i]}. Must be monotonically non-decreasing."
        )
    assert observed[-1] == total, (
        f"Final progress value {observed[-1]} != total {total}."
    )


def test_fetch_progress_callback_optional() -> None:
    """Given no progress callback is provided and a body with the SQLite magic header.
    When fetch_cdfer is called.
    Then it completes without error (progress parameter is optional).
    """
    import tempfile

    content = _make_sqlite_body(64)
    fake_resp = _FakeResponse(content_length=len(content), chunks=[content])
    client = _FakeHttpClient(fake_resp)

    with tempfile.TemporaryDirectory() as td:
        dest = pathlib.Path(td) / "out.sqlite3"
        # Must not raise regardless of whether progress is passed.
        fetch_cdfer("https://example.com/out.sqlite3", dest, http_client=client)


# ---------------------------------------------------------------------------
# T-FETCH-no-network
# ---------------------------------------------------------------------------

def test_fetch_injectable_http_client_seam_exists() -> None:
    """Given the fetch_cdfer function.
    When we inspect its signature.
    Then it must accept an 'http_client' keyword argument, proving that a real
    network socket is never mandatory in tests.

    This seam is the contract that enables all other tests in this module to
    run without touching the network.
    """
    sig = inspect.signature(fetch_cdfer)
    assert "http_client" in sig.parameters, (
        "fetch_cdfer must accept an 'http_client' keyword parameter "
        "so tests can inject a fake client without monkey-patching globals."
    )


def test_fetch_no_real_socket_when_client_injected(tmp_path: pathlib.Path) -> None:
    """Given a fully injected http_client with a body beginning with the SQLite magic.
    When fetch_cdfer is called.
    Then the 'socket' module's 'create_connection' must never be called
    (i.e. no real OS-level TCP socket is opened).
    """
    content = _make_sqlite_body(32)
    fake_resp = _FakeResponse(content_length=len(content), chunks=[content])
    client = _FakeHttpClient(fake_resp)
    dest = tmp_path / "no_socket.sqlite3"

    with patch("socket.create_connection") as mock_conn:
        fetch_cdfer("https://example.com/no_socket.sqlite3", dest, http_client=client)
    assert not mock_conn.called, (
        "socket.create_connection was called even though an injected http_client "
        "was supplied. The implementation must not open real sockets when "
        "http_client is provided."
    )


def test_fetch_signature_force_parameter_exists() -> None:
    """Given the fetch_cdfer function.
    When we inspect its signature.
    Then it must accept a 'force' keyword argument (bool), enabling idempotency
    bypass.
    """
    sig = inspect.signature(fetch_cdfer)
    assert "force" in sig.parameters, (
        "fetch_cdfer must accept a 'force' keyword parameter (bool)."
    )


def test_fetch_signature_progress_parameter_exists() -> None:
    """Given the fetch_cdfer function.
    When we inspect its signature.
    Then it must accept a 'progress' keyword argument (callable), enabling
    optional progress reporting without coupling the caller to any specific
    UI framework.
    """
    sig = inspect.signature(fetch_cdfer)
    assert "progress" in sig.parameters, (
        "fetch_cdfer must accept a 'progress' keyword parameter (callable | None)."
    )


# ---------------------------------------------------------------------------
# T-FETCH-size-limit (B1) — Content-Length exceeding max_bytes
# ---------------------------------------------------------------------------

def test_fetch_content_length_exceeding_max_bytes_raises_before_streaming() -> None:
    """Given a fake response whose Content-Length header exceeds max_bytes.
    When fetch_cdfer is called with that max_bytes limit.
    Then it raises ValueError BEFORE calling iter_bytes (no streaming occurs)
    and the error message mentions size/limit/bytes.

    Security: prevents storing arbitrary-size downloads without a cap check.
    """
    max_bytes = 1024  # small limit for test clarity
    too_large = max_bytes + 1

    fake_resp = _FakeResponse(
        content_length=too_large,
        chunks=[_make_sqlite_body(too_large)],  # body would be huge in production
    )
    client = _FakeHttpClient(fake_resp)

    with pytest.raises(ValueError) as exc_info:
        fetch_cdfer(
            "https://example.com/toobig.sqlite3",
            pathlib.Path("/tmp/toobig.sqlite3"),
            http_client=client,
            max_bytes=max_bytes,
        )

    msg = str(exc_info.value).lower()
    assert any(kw in msg for kw in ("size", "limit", "byte", "large", "exceed")), (
        f"ValueError message must mention size/limit/bytes; got: {msg!r}"
    )

    # iter_bytes must NOT have been called — the check happens before streaming.
    assert not fake_resp._iter_bytes_called, (
        "iter_bytes() must NOT be called when Content-Length already exceeds max_bytes. "
        "The size check must happen before any streaming begins."
    )


# ---------------------------------------------------------------------------
# T-FETCH-magic (B2) — SQLite magic header validation
# ---------------------------------------------------------------------------

def test_fetch_invalid_magic_raises_and_no_dest_or_part_file(tmp_path: pathlib.Path) -> None:
    """Given downloaded bytes that do NOT start with the SQLite magic header
    (b'SQLite format 3' + 0x00 — built at runtime).
    When fetch_cdfer is called.
    Then it raises a descriptive error AND neither the destination file nor a
    .part file remains on disk.

    Security: rejects silently corrupted or substituted downloads.
    """
    # Build the expected 16-byte magic at runtime (not a literal in source).
    expected_magic = b"SQLite format 3" + bytes([0])
    assert len(expected_magic) == 16

    # Body that does NOT begin with the magic — just zeros.
    bad_body = bytes(512)
    assert not bad_body.startswith(expected_magic)

    fake_resp = _FakeResponse(
        content_length=len(bad_body),
        chunks=[bad_body],
    )
    client = _FakeHttpClient(fake_resp)
    dest = tmp_path / "bad_magic.sqlite3"

    with pytest.raises(Exception) as exc_info:
        fetch_cdfer("https://example.com/bad.sqlite3", dest, http_client=client)

    # Error must mention something about magic/header/format/sqlite.
    msg = str(exc_info.value).lower()
    assert any(kw in msg for kw in ("magic", "header", "sqlite", "format", "invalid")), (
        f"Error message must describe invalid SQLite magic; got: {msg!r}"
    )

    # No destination file must remain.
    assert not dest.exists(), (
        "Destination file must NOT exist after an invalid-magic rejection."
    )
    # No .part temp file must remain.
    part_files = list(tmp_path.glob("*.part"))
    assert not part_files, (
        f".part temp files must NOT remain after invalid-magic rejection: {part_files}"
    )


def test_fetch_valid_magic_header_succeeds(tmp_path: pathlib.Path) -> None:
    """Given downloaded bytes that DO start with the SQLite magic header.
    When fetch_cdfer is called.
    Then it completes successfully and the destination file is written.
    """
    content = _make_sqlite_body(256)
    assert content[:16] == _SQLITE_MAGIC

    fake_resp = _FakeResponse(content_length=len(content), chunks=[content])
    client = _FakeHttpClient(fake_resp)
    dest = tmp_path / "valid_magic.sqlite3"

    fetch_cdfer("https://example.com/valid.sqlite3", dest, http_client=client)

    assert dest.exists(), "Destination must exist after successful download with valid magic."


# ---------------------------------------------------------------------------
# T-FETCH-size-limit (B3) — streaming cap when Content-Length lies low
# ---------------------------------------------------------------------------

def test_fetch_streaming_cap_aborts_when_accumulated_bytes_exceed_max_bytes(
    tmp_path: pathlib.Path,
) -> None:
    """Given a body whose Content-Length header lies (reports small) but the
    actual accumulated streamed bytes exceed max_bytes.
    When fetch_cdfer is called.
    Then it aborts with ValueError once the cap is exceeded and the destination
    file does NOT exist after the abort.

    Security: prevents content-length spoofing that would allow oversized
    downloads to bypass the declared limit.
    """
    max_bytes = 256  # small limit for test clarity

    # Lie in Content-Length (claim 100 bytes), but provide 512 bytes of data.
    lying_content_length = 100
    # Body starts with valid magic, then lots of padding to exceed max_bytes.
    body_chunk = _make_sqlite_body(512)  # 512 > max_bytes=256

    fake_resp = _FakeResponse(
        content_length=lying_content_length,  # lies!
        chunks=[body_chunk],
    )
    client = _FakeHttpClient(fake_resp)
    dest = tmp_path / "lying_size.sqlite3"

    with pytest.raises(ValueError) as exc_info:
        fetch_cdfer(
            "https://example.com/lying.sqlite3",
            dest,
            http_client=client,
            max_bytes=max_bytes,
        )

    msg = str(exc_info.value).lower()
    assert any(kw in msg for kw in ("size", "limit", "byte", "large", "exceed", "cap")), (
        f"ValueError message must mention size/limit/bytes; got: {msg!r}"
    )

    # Destination must NOT exist — aborted downloads must not leave a complete file.
    assert not dest.exists(), (
        "Destination file must NOT exist after a streaming-cap abort."
    )
