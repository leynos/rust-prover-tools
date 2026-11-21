"""Regression tests for bounded, verified HTTPS downloads."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import http.server
import logging
import pathlib
import socket
import threading
import time
import typing as typ

import pytest

from rust_prover_tools import (
    download_deadline,
    download_stream,
    download_transport,
    downloads,
)
from rust_prover_tools.downloads import (
    DownloadLimits,
    PinnedAsset,
    download_pinned,
    sha256_file,
)
from rust_prover_tools.errors import ProverToolError, TransientDownloadError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import os
    from pathlib import Path

# Tests that exercise byte limits rather than time keep the deadline open.
NO_DEADLINE = float("inf")


def stream_limits(
    max_bytes: int,
    deadline: float,
    *,
    name: str = "asset.bin",
) -> download_stream._StreamLimits:
    """Return stream limits for a test asset."""
    return download_stream._StreamLimits(name, max_bytes, deadline)


def download_request(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    asset_name: str | None = None,
) -> download_transport._DownloadRequest:
    """Return one bounded download request for a test URL."""
    return download_transport._DownloadRequest(
        url,
        destination,
        max_bytes,
        asset_name,
    )


def events_from(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the structured events captured during a test."""
    return [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]


def deny_pending_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make removing a pending download fail as an unwritable directory would."""
    original_unlink = pathlib.Path.unlink

    def deny_unlink(self: pathlib.Path, *, missing_ok: bool = False) -> None:
        """Raise a permission error when a pending download is unlinked."""
        if self.name.endswith(".download"):
            raise PermissionError(13, "permission denied", str(self))
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", deny_unlink)


class FakeClock:
    """A deterministic monotonic clock."""

    def __init__(self, now: float = 0.0) -> None:
        """Start the fake clock at `now` seconds."""
        self.now = now

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the fake clock forward."""
        self.now += seconds


class GatedClock:
    """A real clock that stands still until a blocked phase starts.

    A hop reads its budget before the fake call blocks, so a real clock could
    expire during a scheduling stall and fail the test before the watchdog is
    armed. Until `started` is set this clock reports the instant the deadline
    was measured against; after it, real time, so the watchdog still waits out
    the budget in real seconds and the bounded joins stay guards rather than
    the deadline mechanism.
    """

    def __init__(self, budget: float = 0.2) -> None:
        """Freeze the clock and measure `budget` against the frozen instant."""
        self.started = threading.Event()
        self._origin = time.monotonic()
        self.deadline = self._origin + budget

    def monotonic(self) -> float:
        """Report the frozen origin until the gate opens, then real time."""
        if self.started.is_set():
            return time.monotonic()
        return self._origin


class FakeResponse:
    """A response object that satisfies the downloader protocol."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
        clock: FakeClock | None = None,
    ) -> None:
        """Store the canned status, headers, body chunks, and optional clock."""
        self.status = status
        self.headers = headers or {}
        self.chunks = chunks
        self.read_count = 0
        self._clock = clock

    def getheader(
        self,
        name: str,
        default: str | None = None,
    ) -> str | None:
        """Return a canned header."""
        return self.headers.get(name, default)

    def isclosed(self) -> bool:
        """Report whether the canned body has reached its end."""
        return self.read_count >= len(self.chunks)

    def read(self, amt: int | None = None) -> bytes:
        """Return the next canned chunk, or an empty chunk at EOF."""
        del amt
        if self._clock is not None:
            self._clock.advance(1.0)
        if self.read_count >= len(self.chunks):
            return b""
        chunk = self.chunks[self.read_count]
        self.read_count += 1
        return chunk


class FakeConnection:
    """A connection object that records whether it was closed."""

    def __init__(self) -> None:
        """Start the connection in the open state."""
        self.closed = False

    def close(self) -> None:
        """Record closure."""
        self.closed = True


class FakeSocket:
    """A socket double that records the timeouts and shutdowns it receives."""

    def __init__(self, release: threading.Event | None = None) -> None:
        """Start with no recorded traffic and an optional release signal."""
        self.timeouts: list[float] = []
        self.shutdowns: list[int] = []
        self.timeout: float | None = None
        self._release = release

    def settimeout(self, value: float) -> None:
        """Record a requested timeout."""
        self.timeouts.append(value)
        self.timeout = value

    def shutdown(self, how: int) -> None:
        """Record a shutdown, releasing any fake call blocked on the socket."""
        self.shutdowns.append(how)
        if self._release is not None:
            self._release.set()


class FakeHopConnection:
    """A scripted connection double for one transport hop.

    The transport constructs its connection, so a test builds this double with
    the phases it scripts and hands it out through `HopFactory`. `connect()`
    creates the socket the transport captures, which keeps the constructor
    timeout and the pre-send re-arm distinguishable in `sock.timeouts`.
    """

    def __init__(
        self,
        *,
        sock: FakeSocket | None = None,
        clock: FakeClock | None = None,
        connect_seconds: float = 0.0,
        response: FakeResponse | None = None,
        clear_sock_on_response: bool = False,
        on_request: cabc.Callable[[], None] | None = None,
        on_response: cabc.Callable[[], None] | None = None,
        on_close: cabc.Callable[[], None] | None = None,
    ) -> None:
        """Configure the socket and the work each hop phase performs."""
        self.sock = sock
        self.host: str | None = None
        self.port: int | None = None
        self.timeout: float | None = None
        self.requests: list[dict[str, object]] = []
        self.responses = 0
        self.closed = False
        self._clock = clock
        self._connect_seconds = connect_seconds
        self._response = response or FakeResponse(chunks=(b"payload",))
        self._clear_sock_on_response = clear_sock_on_response
        self._on_request = on_request
        self._on_response = on_response
        self._on_close = on_close

    def connect(self) -> None:
        """Create the socket the transport captures, spending connect time."""
        if self._clock is not None:
            self._clock.advance(self._connect_seconds)
        if self.sock is None:
            self.sock = FakeSocket()

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str],
    ) -> None:
        """Record the request and the timeout in force, then run its script."""
        self.requests.append(
            {
                "method": method,
                "target": target,
                "headers": headers,
                "timeout": None if self.sock is None else self.sock.timeout,
            },
        )
        if self._on_request is not None:
            self._on_request()

    def getresponse(self) -> FakeResponse:
        """Return the canned response, clearing the socket when scripted."""
        self.responses += 1
        if self._on_response is not None:
            self._on_response()
        if self._clear_sock_on_response:
            # `http.client` clears `sock` when it cannot reuse the connection,
            # so a response double must be able to model that.
            self.sock = None
        return self._response

    def close(self) -> None:
        """Record closure, then run any scripted close work."""
        self.closed = True
        if self._on_close is not None:
            self._on_close()


class HopFactory:
    """Serve scripted hops in order from the patched connection constructor."""

    def __init__(self, *hops: FakeHopConnection) -> None:
        """Store the hops to hand out, in order."""
        self._hops = list(hops)
        self.constructed: list[FakeHopConnection] = []

    def __call__(
        self,
        host: str,
        port: int | None,
        *,
        timeout: float,
    ) -> FakeHopConnection:
        """Record the constructor budget on the next scripted hop."""
        hop = self._hops.pop(0)
        hop.host = host
        hop.port = port
        hop.timeout = timeout
        self.constructed.append(hop)
        return hop


class HopWorker:
    """Run one transport call off the test thread and join it with a bound."""

    def __init__(self, release: threading.Event) -> None:
        """Bind the signal a blocked fake call waits on."""
        self._release = release
        self._action: cabc.Callable[[], object] | None = None
        self.value: object = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def run(
        self,
        action: cabc.Callable[[], object],
        *,
        timeout: float = 5.0,
    ) -> HopWorker:
        """Run `action`, releasing and joining the worker even on failure."""
        self._action = action
        self._thread.start()
        try:
            self._thread.join(timeout)
            assert not self._thread.is_alive(), (
                "the transport call must finish within its bounded deadline"
            )
        finally:
            # A watchdog that never fired would otherwise leave this worker
            # blocked in its fake call for the whole test session.
            self._release.set()
            self._thread.join(timeout)
        return self

    def _run(self) -> None:
        """Call the action and keep whatever it returned or raised."""
        assert self._action is not None, "the worker must be given an action"
        try:
            self.value = self._action()
        except BaseException as exc:  # ruff: ignore[blind-except] - reported to the test
            self.error = exc


def test_stream_rejects_oversized_content_length_before_reading(
    tmp_path: Path,
) -> None:
    """A declared oversized body is refused before the destination opens."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(
        headers={"Content-Length": "100"},
        chunks=(b"x" * 100,),
    )

    with pytest.raises(ProverToolError, match="Content-Length 100 exceeds"):
        download_stream._stream(
            response,
            destination,
            stream_limits(10, NO_DEADLINE),
        )

    assert response.read_count == 0, "the body must not be read"
    assert not destination.exists(), "no partial file should be created"


def test_stream_stops_at_byte_ceiling_without_length_header(
    tmp_path: Path,
) -> None:
    """A streamed response without Content-Length is counted and stopped."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(chunks=(b"123456", b"7890"))

    with pytest.raises(ProverToolError, match="streamed size exceeded"):
        download_stream._stream(
            response,
            destination,
            stream_limits(8, NO_DEADLINE),
        )

    assert response.read_count == 2, "the offending chunk should stop the read"
    assert destination.read_bytes() == b"123456", (
        "the destination must not receive the offending chunk"
    )


def test_stream_emits_a_size_limit_event_at_debug_level(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The size-limit boundary is observable when the logger opts in."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")
    destination = tmp_path / "asset.bin"
    response = FakeResponse(chunks=(b"123456", b"7890"))

    with pytest.raises(ProverToolError, match="streamed size exceeded"):
        download_stream._stream(
            response,
            destination,
            stream_limits(8, NO_DEADLINE),
        )

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    assert any(
        event["event"] == "download.failed" and event["reason"] == "size_limit"
        for event in events
    ), "a rejected stream should report its limit category"


def test_stream_is_silent_without_debug_logging(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default configuration produces no download events."""
    caplog.set_level(logging.WARNING, logger="rust_prover_tools.download_stream")
    destination = tmp_path / "asset.bin"
    response = FakeResponse(chunks=(b"payload",))

    download_stream._stream(
        response,
        destination,
        stream_limits(100, NO_DEADLINE),
    )

    assert not caplog.records, "download events must be opt-in"


def test_download_pinned_emits_a_cache_hit_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cache hit is observable when the logger opts in."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.downloads")
    payload = b"cached"
    destination = tmp_path / "asset.bin"
    destination.write_bytes(payload)
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=sha256_file(destination),
        max_bytes=100,
    )

    download_pinned(asset, destination)

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    assert {"event": "download.cache_hit"} in events, (
        "a cache hit must not carry an asset-name label"
    )


def test_stream_stops_when_total_deadline_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow stream is stopped by the overall transfer deadline."""
    clock = FakeClock()
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)
    destination = tmp_path / "asset.bin"
    response = FakeResponse(chunks=(b"a", b"b"), clock=clock)

    with pytest.raises(ProverToolError, match="transfer deadline"):
        download_stream._stream(
            response,
            destination,
            stream_limits(100, 0.0),
        )

    assert response.read_count == 1, "the deadline should stop further reads"
    assert destination.read_bytes() == b"a", (
        "the bytes read before the deadline must be kept"
    )


def test_deadline_response_bounds_each_read_by_the_remaining_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every body read resets the socket timeout to the remaining deadline."""
    clock = FakeClock()
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)
    sock = FakeSocket()
    response = download_stream._DeadlineResponse(
        FakeResponse(chunks=(b"a", b"b")),
        deadline=10.0,
        read_timeout_seconds=15.0,
        sock=sock,
    )

    assert response.read(1) == b"a", "the wrapper must forward the first chunk"
    clock.advance(4.0)
    assert response.read(1) == b"b", "the wrapper must forward later chunks"
    assert sock.timeouts == [10.0, 6.0], (
        "each read must wait no longer than the remaining deadline"
    )


def test_deadline_response_uses_the_configured_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short socket timeout still caps a read inside a longer deadline."""
    clock = FakeClock()
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)
    sock = FakeSocket()
    response = download_stream._DeadlineResponse(
        FakeResponse(chunks=(b"a",)),
        deadline=300.0,
        read_timeout_seconds=15.0,
        sock=sock,
    )

    response.read(1)

    assert sock.timeouts == [15.0], "the configured read timeout must bound the wait"


def test_deadline_response_skips_the_timeout_without_a_socket() -> None:
    """A response without a socket still forwards its body."""
    response = download_stream._DeadlineResponse(
        FakeResponse(chunks=(b"body",)),
        deadline=10.0,
        read_timeout_seconds=15.0,
    )

    assert response.read(4) == b"body", "the wrapper must forward the body"


def test_stream_reports_a_read_timeout_at_the_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A read timeout at the deadline is reported as a deadline failure."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")
    clock = FakeClock()
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)

    class TimingOutResponse(FakeResponse):
        """A response whose read runs past the deadline and then times out."""

        def read(self, amt: int | None = None) -> bytes:
            """Advance past the deadline and time out."""
            del amt
            clock.advance(2.0)
            msg = "timed out"
            raise TimeoutError(msg)

    with pytest.raises(ProverToolError, match="transfer deadline"):
        download_stream._stream(
            TimingOutResponse(),
            tmp_path / "asset.bin",
            stream_limits(10, 1.0),
        )

    assert events_from(caplog)[-1] == {
        "event": "download.failed",
        "reason": "deadline",
        "written": 0,
    }, "a read timeout at the deadline must report the deadline"


def test_stream_reports_an_early_read_timeout_as_a_stream_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A read timeout before the deadline remains a transport failure."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")
    clock = FakeClock()
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)

    class TimingOutResponse(FakeResponse):
        """A response whose read times out before the deadline is reached."""

        def read(self, amt: int | None = None) -> bytes:
            """Time out without reaching the deadline."""
            del amt
            clock.advance(0.1)
            msg = "timed out"
            raise TimeoutError(msg)

    with pytest.raises(ProverToolError, match="failed to download"):
        download_stream._stream(
            TimingOutResponse(),
            tmp_path / "asset.bin",
            stream_limits(10, 10.0),
        )

    assert events_from(caplog)[-1] == {
        "event": "download.failed",
        "reason": "stream_error",
    }, "an early read timeout must not be reported as the deadline"


def test_stream_accepts_a_complete_body_matching_content_length(
    tmp_path: Path,
) -> None:
    """A body within both limits and matching its length is written."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(
        headers={"Content-Length": "6"},
        chunks=(b"abc", b"def"),
    )

    download_stream._stream(response, destination, stream_limits(10, NO_DEADLINE))

    assert destination.read_bytes() == b"abcdef", "the complete body must be written"


def test_stream_rejects_an_incomplete_body(tmp_path: Path) -> None:
    """A body shorter than Content-Length is treated as a failed transfer."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(
        headers={"Content-Length": "10"},
        chunks=(b"abc",),
    )

    with pytest.raises(ProverToolError, match="incomplete download"):
        download_stream._stream(
            response,
            destination,
            stream_limits(20, NO_DEADLINE),
        )


@pytest.mark.parametrize("value", ["not-a-number", "-1"])
def test_stream_rejects_invalid_content_length(
    tmp_path: Path,
    value: str,
) -> None:
    """Malformed length headers cannot bypass the ceiling."""
    destination = tmp_path / "asset.bin"
    response = FakeResponse(headers={"Content-Length": value})

    with pytest.raises(ProverToolError, match="Content-Length"):
        download_stream._stream(
            response,
            destination,
            stream_limits(10, NO_DEADLINE),
        )


def test_download_follows_a_bounded_https_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect to another HTTPS host is followed and closed."""
    first_connection = FakeConnection()
    second_connection = FakeConnection()
    responses = iter(
        (
            (
                FakeResponse(
                    status=302,
                    headers={"Location": "https://cdn.example.test/asset"},
                ),
                first_connection,
            ),
            (
                FakeResponse(chunks=(b"payload",)),
                second_connection,
            ),
        ),
    )

    def fake_open(
        url: str,
        limits: DownloadLimits,
        deadline: float,
    ) -> tuple[FakeResponse, FakeConnection]:
        """Return the next scripted response and its connection."""
        del url, limits, deadline
        return next(responses)

    monkeypatch.setattr(download_transport, "_open", fake_open)
    destination = tmp_path / "asset.bin"

    download_transport._download(
        download_request("https://example.test/asset", destination, max_bytes=100),
        DownloadLimits(max_redirects=1),
    )

    assert destination.read_bytes() == b"payload", (
        "the final body must be written to the destination"
    )
    assert first_connection.closed, "the redirect connection must close"
    assert second_connection.closed, "the final connection must close"


def test_download_rejects_excess_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redirect ceiling stops a redirect loop."""
    connection = FakeConnection()
    response = FakeResponse(
        status=302,
        headers={"Location": "https://example.test/again"},
    )

    def fake_open(
        url: str,
        limits: DownloadLimits,
        deadline: float,
    ) -> tuple[FakeResponse, FakeConnection]:
        """Return the redirecting response and its connection."""
        del url, limits, deadline
        return response, connection

    monkeypatch.setattr(download_transport, "_open", fake_open)

    with pytest.raises(ProverToolError, match="too many redirects"):
        download_transport._download(
            download_request(
                "https://example.test/asset",
                tmp_path / "asset.bin",
                max_bytes=100,
            ),
            DownloadLimits(max_redirects=0),
        )

    assert connection.closed, "the rejected redirect connection must close"


@pytest.mark.parametrize(
    ("status", "headers", "match"),
    [
        (302, {"Location": "http://example.test/asset"}, "must use https"),
        (302, None, "no Location"),
        (404, None, "HTTP status 404"),
    ],
    ids=["downgrade", "missing-location", "non-success"],
)
def test_download_rejects_invalid_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str] | None,
    match: str,
) -> None:
    """A failed response is reported and its connection is closed."""
    connection = FakeConnection()
    response = FakeResponse(status=status, headers=headers)
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )

    with pytest.raises(ProverToolError, match=match):
        download_transport._download(
            download_request(
                "https://example.test/asset",
                tmp_path / "asset.bin",
                max_bytes=100,
            ),
            DownloadLimits(),
        )

    assert connection.closed, "the failed connection must close"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/asset",
        "https://user:secret@example.test/asset",
        "https:///asset",
        "https://example.test:not-a-port/asset",
    ],
)
def test_download_rejects_urls_outside_the_https_policy(url: str) -> None:
    """Only credential-free HTTPS URLs with a valid host are accepted."""
    with pytest.raises(ProverToolError):
        download_transport._validate_url(url)


def test_download_pinned_reuses_a_matching_cache_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached asset with the pinned digest avoids the network."""
    payload = b"cached"
    destination = tmp_path / "asset.bin"
    destination.write_bytes(payload)
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=sha256_file(destination),
        max_bytes=100,
    )

    def fail_download(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Fail the test if a cache hit falls back to the network."""
        del request, limits
        msg = "network must not be used for a cache hit"
        raise AssertionError(msg)

    monkeypatch.setattr(downloads, "_download", fail_download)

    assert download_pinned(asset, destination) == destination, (
        "a cache hit must return the destination path"
    )
    assert destination.read_bytes() == payload, (
        "a cache hit must keep the verified bytes"
    )


def test_download_pinned_removes_pending_file_on_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest mismatch never leaves a pending or final asset behind."""
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )

    def write_wrong_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write bytes whose digest cannot match the pinned asset."""
        del limits
        request.destination.write_bytes(b"wrong")

    monkeypatch.setattr(downloads, "_download", write_wrong_payload)

    with pytest.raises(ProverToolError, match="SHA-256 mismatch"):
        download_pinned(asset, destination)

    assert not destination.exists(), (
        "a digest mismatch must not publish the destination"
    )
    assert not destination.with_name("asset.bin.download").exists(), (
        "a digest mismatch must remove the pending file"
    )


def test_download_pinned_reports_a_digest_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure to hash the pending file is one bounded download failure."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.downloads")
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write the pending payload that the digest check then fails to read."""
        del limits
        request.destination.write_bytes(b"payload")

    def fail_to_hash(path: Path) -> str:
        """Raise a bounded read error instead of hashing the pending file."""
        msg = f"failed to read '{path}': permission denied"
        raise ProverToolError(msg)

    monkeypatch.setattr(downloads, "_download", write_payload)
    monkeypatch.setattr(downloads, "sha256_file", fail_to_hash)

    with pytest.raises(ProverToolError, match="failed to read"):
        download_pinned(asset, destination)

    assert [event for event in events_from(caplog) if "failed" in str(event)] == [
        {"event": "download.failed", "reason": "digest_read"}
    ], "a digest read failure must report exactly one bounded reason"
    assert not destination.exists(), "an unverified response must not be published"
    assert not destination.with_name("asset.bin.download").exists(), (
        "a digest read failure must still remove the pending file"
    )


def test_download_pinned_reports_a_pending_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cleanup failure after a verified store is a bounded failure."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.downloads")
    payload = b"verified"
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=100,
    )

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write the verified payload to the pending download path."""
        del limits
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", write_payload)
    deny_pending_cleanup(monkeypatch)

    with pytest.raises(ProverToolError, match="failed to remove pending download"):
        download_pinned(asset, destination)

    assert [event for event in events_from(caplog) if "failed" in str(event)] == [
        {"event": "download.failed", "reason": "cleanup_error"}
    ], "a cleanup failure must report exactly one bounded reason"
    assert not any(
        event.get("event") == "download.verified" for event in events_from(caplog)
    ), "a failed cleanup must not report a completed verification"
    assert destination.read_bytes() == payload, (
        "the verified asset stays published even when cleanup fails"
    )


def test_download_pinned_keeps_the_primary_failure_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cleanup failure must not replace the digest mismatch that caused it."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.downloads")
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )

    def write_wrong_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write bytes whose digest cannot match the pinned asset."""
        del limits
        request.destination.write_bytes(b"wrong")

    monkeypatch.setattr(downloads, "_download", write_wrong_payload)
    deny_pending_cleanup(monkeypatch)

    with pytest.raises(ProverToolError, match="SHA-256 mismatch"):
        download_pinned(asset, destination)

    events = events_from(caplog)
    assert [event for event in events if event.get("event") == "download.failed"] == [
        {"event": "download.failed", "reason": "digest_mismatch"}
    ], "the primary failure must remain the only download failure event"
    assert {
        "event": "download.cleanup_failed",
        "reason": "cleanup_error",
    } in events, "a masked cleanup failure should still be observable"


def test_download_pinned_stores_a_verified_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified download is moved from the pending path to its destination."""
    payload = b"verified"
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=100,
    )

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write the verified payload to the pending download path."""
        del limits
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", write_payload)

    assert download_pinned(asset, destination) == destination, (
        "a verified download must return the destination path"
    )
    assert destination.read_bytes() == payload, (
        "the published file must contain the verified payload"
    )
    assert not destination.with_name("asset.bin.download").exists(), (
        "the pending file must be renamed, not left behind"
    )


def test_download_pinned_removes_pending_file_when_stream_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The content-length ceiling removes the pending file from disk."""
    clock = FakeClock()
    monkeypatch.setattr(download_deadline, "monotonic", clock.monotonic)
    connection = FakeConnection()
    response = FakeResponse(
        headers={"Content-Length": "100"},
        chunks=(b"x" * 100,),
    )
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )
    destination = tmp_path / "asset.bin"

    with pytest.raises(ProverToolError, match="Content-Length"):
        download_pinned(
            PinnedAsset(
                name="asset.bin",
                url="https://example.test/asset.bin",
                sha256="0" * 64,
                max_bytes=10,
            ),
            destination,
            DownloadLimits(transfer_timeout_seconds=60.0),
        )

    assert not destination.exists(), (
        "a stopped download must not publish the destination"
    )
    assert not destination.with_name("asset.bin.download").exists(), (
        "a stopped download must remove the pending file"
    )


def test_download_pinned_reports_a_directory_creation_failure(
    tmp_path: Path,
) -> None:
    """A destination whose parent cannot be created is reported."""
    blocker = tmp_path / "cache"
    blocker.write_text("not a directory", encoding="utf-8")
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )

    with pytest.raises(ProverToolError, match="failed to create download directory"):
        download_pinned(asset, blocker / "asset.bin")


def test_download_pinned_reports_a_store_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified download that cannot be published is reported."""
    payload = b"verified"
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=100,
    )

    def write_payload(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Write the verified payload to the pending download path."""
        del limits
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", write_payload)
    original_replace = pathlib.Path.replace

    def deny_publish(
        path: pathlib.Path,
        target: pathlib.Path,
    ) -> pathlib.Path:
        """Raise a permission error when the pending file is published."""
        if path.name == "asset.bin.download":
            raise PermissionError(13, "permission denied", str(path))
        return original_replace(path, target)

    monkeypatch.setattr(pathlib.Path, "replace", deny_publish)

    with pytest.raises(ProverToolError, match="failed to store"):
        download_pinned(asset, destination)


def test_sha256_file_reports_an_unreadable_path(tmp_path: Path) -> None:
    """A path that cannot be read as a file is reported."""
    with pytest.raises(ProverToolError, match="failed to read"):
        sha256_file(tmp_path)


def test_stream_reports_a_write_failure(tmp_path: Path) -> None:
    """A filesystem error while writing the body is a storage failure.

    Opening or writing the destination cannot be repaired by asking the same
    server again, so the error must not be classified as a transient download.
    """
    destination = tmp_path / "asset.bin"
    destination.mkdir()
    response = FakeResponse(chunks=(b"payload",))

    with pytest.raises(ProverToolError, match="failed to store"):
        download_stream._stream(
            response,
            destination,
            stream_limits(100, NO_DEADLINE),
        )


def test_open_requests_the_validated_path_with_identity_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport requests the resource path and asks for an identity body."""
    response = FakeResponse(chunks=(b"payload",))
    sock = FakeSocket()
    captured: dict[str, object] = {}

    class FakeConnection:
        """A connection double that records the request and serves a response."""

        def __init__(
            self,
            host: str,
            port: int | None,
            *,
            timeout: float,
        ) -> None:
            """Record the connection target."""
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout
            self.sock: FakeSocket | None = None

        def connect(self) -> None:
            """Create the socket the transport captures and re-arms."""
            self.sock = sock

        def request(
            self,
            method: str,
            target: str,
            *,
            headers: dict[str, str],
        ) -> None:
            """Record the method, target, and headers of the request."""
            captured["method"] = method
            captured["target"] = target
            captured["headers"] = headers

        def getresponse(self) -> FakeResponse:
            """Return the canned response."""
            return response

        def close(self) -> None:
            """Record that the connection was closed."""
            captured["closed"] = True

    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        FakeConnection,
    )

    returned_response, hop = download_transport._open(
        "https://example.test:8443/asset.bin?token=abc",
        DownloadLimits(connect_timeout_seconds=5.0),
        download_transport.monotonic() + 60.0,
    )
    try:
        assert returned_response.status == 200, "the response status must be returned"
        assert returned_response.read(7) == b"payload", (
            "the response body must be readable"
        )
    finally:
        hop.close()

    assert sock.timeouts == [5.0, 5.0], (
        "the send and the body read must each be armed with the configured timeout"
    )
    assert isinstance(hop, download_transport._HopHandle), (
        "a hop handle must be returned"
    )
    assert captured["closed"] is True, "the hop handle must close the connection"
    assert captured["host"] == "example.test", "the host must come from the URL"
    assert captured["port"] == 8443, "the port must come from the URL"
    assert captured["target"] == "/asset.bin?token=abc", (
        "the path and query must be requested"
    )
    assert captured["method"] == "GET", "the request must be a GET"
    assert captured["headers"] == {
        "Accept-Encoding": "identity",
        "User-Agent": download_transport.USER_AGENT,
    }, "the request must ask for an identity body"


def test_open_reports_a_failed_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connection error is reported and the connection is closed."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")

    class FailingConnection:
        """A connection whose every request fails with a connection error."""

        def __init__(
            self,
            host: str,
            port: int | None,
            *,
            timeout: float,
        ) -> None:
            """Track this connection so the test can check it was closed."""
            del host, port, timeout
            self.sock: FakeSocket | None = None
            self.closed = False
            connections.append(self)

        def connect(self) -> None:
            """Create the socket the transport captures before the request."""
            self.sock = FakeSocket()

        def request(
            self,
            method: str,
            target: str,
            *,
            headers: dict[str, str],
        ) -> None:
            """Raise a connection error for every request."""
            del method, target, headers
            msg = "connection refused"
            raise OSError(msg)

        def close(self) -> None:
            """Record that the connection was closed."""
            self.closed = True

    connections: list[FailingConnection] = []

    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        FailingConnection,
    )

    with pytest.raises(TransientDownloadError, match="failed to download"):
        download_transport._open(
            "https://example.test/asset.bin",
            DownloadLimits(),
            download_transport.monotonic() + 60.0,
        )

    assert len(connections) == 1, "the transport should open one connection"
    assert connections[0].closed, "the failed connection must close"
    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    assert {
        "event": "download.failed",
        "reason": "transport_error",
    } in events, "a transport failure must not report an asset-name label"


def test_open_rejects_an_expired_deadline() -> None:
    """A deadline already in the past stops before opening a connection."""
    with pytest.raises(ProverToolError, match="exceeded the transfer deadline"):
        download_transport._open(
            "https://example.test/asset.bin",
            DownloadLimits(),
            download_transport.monotonic() - 1.0,
        )


def test_open_sends_the_request_under_the_time_left_after_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A five-second connect leaves fifty-five seconds for the request."""
    clock = FakeClock()
    # The hop budget and the watchdog read the deadline module's clock, while
    # the response wrapper reads the stream's.
    monkeypatch.setattr(download_deadline, "monotonic", clock.monotonic)
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)
    sock = FakeSocket()
    hop = FakeHopConnection(sock=sock, clock=clock, connect_seconds=5.0)
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        HopFactory(hop),
    )

    _response, handle = download_transport._open(
        "https://example.test/asset.bin",
        DownloadLimits(connect_timeout_seconds=60.0),
        60.0,
    )
    try:
        assert hop.timeout == pytest.approx(60.0), "the hop opens with its full budget"
        assert hop.requests[0]["timeout"] == pytest.approx(55.0), (
            "a socket timeout bounds one call, so the request must be sent "
            "under the time the connect phase left"
        )
    finally:
        handle.close()

    assert hop.closed, "the hop handle must close the connection"


def test_open_refuses_a_hop_whose_connect_spends_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connect that reaches the deadline sends no request."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    clock = FakeClock()
    monkeypatch.setattr(download_deadline, "monotonic", clock.monotonic)
    sock = FakeSocket()
    hop = FakeHopConnection(sock=sock, clock=clock, connect_seconds=60.0)
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        HopFactory(hop),
    )

    with pytest.raises(
        ProverToolError,
        match="exceeded the transfer deadline",
    ) as raised:
        download_transport._open(
            "https://example.test/asset.bin",
            DownloadLimits(connect_timeout_seconds=60.0),
            60.0,
        )

    assert not isinstance(raised.value, TransientDownloadError), (
        "a spent deadline is not a retryable transport fault"
    )
    assert hop.requests == [], "a spent hop must not send a request"
    assert sock.timeouts == [], "a spent hop must not re-arm its socket"
    assert hop.closed, "the refused connection must close"
    assert {
        "event": "download.failed",
        "reason": "deadline",
    } in events_from(caplog), "a refused hop must report the deadline"


def test_open_reports_a_blocked_request_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A send the watchdog shuts down surfaces as the transfer's deadline."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    clock = GatedClock()
    monkeypatch.setattr(download_deadline, "monotonic", clock.monotonic)
    released = threading.Event()
    sock = FakeSocket(release=released)

    def block_until_shutdown() -> None:
        """Block until the watchdog shuts the socket down, then fail."""
        clock.started.set()
        if not released.wait(5.0):
            msg = "the watchdog never released the blocked send"
            raise AssertionError(msg)
        msg = "connection reset by peer"
        raise OSError(msg)

    hop = FakeHopConnection(sock=sock, on_request=block_until_shutdown)
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        HopFactory(hop),
    )
    deadline = clock.deadline

    worker = HopWorker(released).run(
        lambda: download_transport._open(
            "https://example.test/asset.bin",
            DownloadLimits(),
            deadline,
        ),
    )

    error = worker.error
    assert isinstance(error, ProverToolError), "a blocked send must fail the download"
    assert not isinstance(error, TransientDownloadError), (
        "a send the deadline cut short must not be retried as a transport fault"
    )
    assert "exceeded the transfer deadline" in str(error), (
        "a blocked send must report the transfer deadline"
    )
    assert isinstance(error.__cause__, OSError), (
        "the socket failure the watchdog caused must be chained"
    )
    assert sock.shutdowns == [socket.SHUT_RDWR], (
        "the watchdog must shut the socket down at the deadline"
    )
    assert hop.responses == 0, "a send cut short must not read a response"
    assert hop.closed, "the failed hop must close its connection"
    assert {
        "event": "download.failed",
        "reason": "deadline",
    } in events_from(caplog), "the blocked send must report the deadline"


def test_open_reports_a_blocked_response_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response the watchdog cuts short surfaces as the deadline."""
    clock = GatedClock()
    monkeypatch.setattr(download_deadline, "monotonic", clock.monotonic)
    released = threading.Event()
    sock = FakeSocket(release=released)

    def block_until_shutdown() -> None:
        """Block until the watchdog shuts the socket down, then fail."""
        clock.started.set()
        if not released.wait(5.0):
            msg = "the watchdog never released the blocked response"
            raise AssertionError(msg)
        msg = "connection reset by peer"
        raise OSError(msg)

    hop = FakeHopConnection(sock=sock, on_response=block_until_shutdown)
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        HopFactory(hop),
    )
    deadline = clock.deadline

    worker = HopWorker(released).run(
        lambda: download_transport._open(
            "https://example.test/asset.bin",
            DownloadLimits(),
            deadline,
        ),
    )

    error = worker.error
    assert isinstance(error, ProverToolError), (
        "a blocked response must fail the download"
    )
    assert not isinstance(error, TransientDownloadError), (
        "a response the deadline cut short must not be retried"
    )
    assert "exceeded the transfer deadline" in str(error), (
        "a blocked response must report the transfer deadline"
    )
    assert hop.responses == 1, "the request is sent before the deadline"
    assert hop.closed, "the failed hop must close its connection"
    assert sock.shutdowns == [socket.SHUT_RDWR], (
        "the watchdog must shut the socket down at the deadline"
    )


def test_stream_reports_a_blocked_read_at_the_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A body read the watchdog cuts short is the transfer's deadline."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")
    clock = GatedClock()
    # `download_stream` keeps the real clock, which shares `GatedClock`'s time
    # base, so the body read and the watchdog agree on the deadline.
    monkeypatch.setattr(download_deadline, "monotonic", clock.monotonic)
    released = threading.Event()
    sock = FakeSocket(release=released)

    class BlockingResponse(FakeResponse):
        """A response whose body read waits for the socket shutdown."""

        def read(self, amt: int | None = None) -> bytes:
            """Block until the watchdog shuts the socket down, then fail."""
            del amt
            clock.started.set()
            if not released.wait(5.0):
                msg = "the watchdog never released the blocked read"
                raise AssertionError(msg)
            msg = "connection reset by peer"
            raise OSError(msg)

    hop = FakeHopConnection(sock=sock, response=BlockingResponse())
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        HopFactory(hop),
    )
    deadline = clock.deadline
    response, handle = download_transport._open(
        "https://example.test/asset.bin",
        DownloadLimits(),
        deadline,
    )
    try:
        worker = HopWorker(released).run(
            lambda: download_stream._stream(
                response,
                tmp_path / "asset.bin",
                stream_limits(100, deadline),
            ),
        )
    finally:
        handle.close()

    error = worker.error
    assert isinstance(error, ProverToolError), "a blocked read must fail the download"
    assert not isinstance(error, TransientDownloadError), (
        "a read the deadline cut short must not be retried as a stream fault"
    )
    assert "transfer deadline" in str(error), (
        "a blocked read must report the transfer deadline"
    )
    assert {
        "event": "download.failed",
        "reason": "deadline",
        "written": 0,
    } in events_from(caplog), "a late EOF or read must report the bytes written"


def test_open_reads_through_the_socket_it_captured_before_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response that clears `connection.sock` still bounds later reads."""
    clock = FakeClock()
    # The hop is opened on the transport's clock and the body read on the
    # stream's, so both are driven by the same fake.
    monkeypatch.setattr(download_deadline, "monotonic", clock.monotonic)
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)
    sock = FakeSocket()
    hop = FakeHopConnection(
        sock=sock,
        clock=clock,
        response=FakeResponse(chunks=(b"payload",)),
        clear_sock_on_response=True,
    )
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        HopFactory(hop),
    )

    response, handle = download_transport._open(
        "https://example.test/asset.bin",
        DownloadLimits(connect_timeout_seconds=60.0),
        60.0,
    )
    try:
        assert hop.sock is None, "the response must have cleared the raw socket"
        clock.advance(10.0)
        assert response.read(7) == b"payload", (
            "the captured socket must still serve the body"
        )
    finally:
        handle.close()

    assert sock.timeouts == [60.0, 50.0], (
        "the captured socket must be re-armed from the time the read has left"
    )


def test_download_stops_each_hops_watchdog_before_closing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every hop stops its watchdog before its connection closes."""
    actions: list[str] = []
    watchdogs: list[download_deadline._DeadlineWatchdog] = []
    stopped: list[int] = []

    class RecordingWatchdog(download_deadline._DeadlineWatchdog):
        """Record the lifecycle of every hop's watchdog."""

        def start(self) -> None:
            """Record the start and keep the instance for the assertions."""
            watchdogs.append(self)
            actions.append("watchdog.start")
            super().start()

        def stop(self) -> None:
            """Record the stop before stopping for real."""
            stopped.append(id(self))
            actions.append("watchdog.stop")
            super().stop()

    def record_close() -> None:
        """Record that a hop closed its connection."""
        actions.append("connection.close")

    # `_open` resolves the watchdog from the transport's own globals, so the
    # recording subclass must replace it there.
    monkeypatch.setattr(download_transport, "_DeadlineWatchdog", RecordingWatchdog)
    redirect = FakeHopConnection(
        response=FakeResponse(
            status=302,
            headers={"Location": "https://example.test/final"},
        ),
        on_close=record_close,
    )
    body = FakeHopConnection(
        response=FakeResponse(chunks=(b"payload",)),
        on_close=record_close,
    )
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        HopFactory(redirect, body),
    )

    download_transport._download(
        download_request(
            "https://example.test/start",
            tmp_path / "asset.bin",
            max_bytes=100,
        ),
        DownloadLimits(max_redirects=1),
    )

    assert actions == [
        "watchdog.start",
        "watchdog.stop",
        "connection.close",
        "watchdog.start",
        "watchdog.stop",
        "connection.close",
    ], "a hop must stop its watchdog before it closes and before the next hop opens"
    assert stopped == [id(watchdog) for watchdog in watchdogs], (
        "a finished hop must not leave its watchdog running"
    )
    assert not any(watchdog.expired for watchdog in watchdogs), (
        "a hop that met its deadline must not have expired"
    )
    assert redirect.closed, "the redirect hop must close"
    assert body.closed, "the final hop must close"


def test_watchdog_is_inert_for_an_unbounded_deadline() -> None:
    """A non-finite deadline can never arrive, so no thread is started."""
    watchdog = download_transport._DeadlineWatchdog(FakeSocket(), float("inf"))
    watchdog.start()
    try:
        assert watchdog._thread is None, (
            "an unbounded deadline must not start a watchdog thread"
        )
    finally:
        watchdog.stop()
    assert not watchdog.expired, "an unbounded watchdog can never expire"


def test_download_complete_event_reports_bytes_and_elapsed_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Completion reports the transferred size and duration, not the asset."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    payload = b"verified"
    response = FakeResponse(chunks=(payload,))
    connection = FakeConnection()
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )

    download_transport._download(
        download_request(
            "https://example.test/asset.bin",
            tmp_path / "asset.bin.download",
            max_bytes=100,
            asset_name="asset.bin",
        ),
        DownloadLimits(),
    )

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    complete = [event for event in events if event["event"] == "download.complete"]
    assert len(complete) == 1, "a successful download emits one completion event"
    event = complete[0]
    assert "asset" not in event, "completion must not carry an asset-name label"
    assert event["written"] == len(payload), "completion reports the body bytes"
    assert event["redirects"] == 0, "a direct download followed no redirects"
    elapsed = event["elapsed_seconds"]
    assert isinstance(elapsed, float), "completion reports a float elapsed time"
    assert elapsed >= 0.0, "completion reports a non-negative elapsed time"
    labelled = [
        event
        for event in events
        if str(event["event"]).startswith("download.") and "asset" in event
    ]
    assert labelled == [], "no download event may carry an asset-name label"


def test_download_reports_a_missing_redirect_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A redirect without a Location header reports that bounded reason."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    connection = FakeConnection()
    response = FakeResponse(status=302)
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )

    with pytest.raises(ProverToolError, match="no Location"):
        download_transport._download(
            download_request(
                "https://example.test/asset.bin",
                tmp_path / "asset.bin.download",
                max_bytes=100,
                asset_name="asset.bin",
            ),
            DownloadLimits(),
        )

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    assert {
        "event": "download.failed",
        "reason": "redirect_location",
        "status": 302,
    } in events, "a bad redirect should report its status and bounded reason"


@pytest.mark.parametrize(
    ("status", "status_class", "is_transient"),
    [(503, "5xx", True), (404, "4xx", False)],
)
def test_download_reports_an_http_status_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
    status_class: str,
    *,
    is_transient: bool,
) -> None:
    """A non-success response reports its status and whether it is transient."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    connection = FakeConnection()
    response = FakeResponse(status=status)
    monkeypatch.setattr(
        download_transport,
        "_open",
        lambda url, limits, deadline: (response, connection),
    )

    with pytest.raises(ProverToolError, match=f"HTTP status {status}") as raised:
        download_transport._download(
            download_request(
                "https://example.test/asset.bin",
                tmp_path / "asset.bin.download",
                max_bytes=100,
                asset_name="asset.bin",
            ),
            DownloadLimits(),
        )

    assert isinstance(raised.value, TransientDownloadError) is is_transient, (
        "only a retryable status may be classified as transient"
    )
    assert {
        "event": "download.failed",
        "reason": "http_status",
        "status": status,
        "status_class": status_class,
    } in events_from(caplog), "a failed response should report a bounded status class"


def test_download_reports_a_stream_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport error while reading the body is reported once."""

    class FailingResponse(FakeResponse):
        """A response whose body read fails with a connection reset."""

        def read(self, amt: int | None = None) -> bytes:
            """Fail every body read with a connection reset."""
            del amt
            msg = "connection reset"
            raise OSError(msg)

    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")

    with pytest.raises(TransientDownloadError, match=r"failed to download asset\.bin"):
        download_stream._stream(
            FailingResponse(),
            tmp_path / "asset.bin",
            stream_limits(100, NO_DEADLINE),
        )

    events = [
        typ.cast("dict[str, object]", record.event_data)
        for record in caplog.records
        if hasattr(record, "event_data")
    ]
    assert {
        "event": "download.failed",
        "reason": "stream_error",
    } in events, "a body transport error should report one bounded event"


def test_download_pinned_passes_the_logical_asset_to_the_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The downloader reports the pinned asset name, not the pending path."""
    payload = b"verified"
    destination = tmp_path / "asset.bin"
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        max_bytes=100,
    )
    captured: dict[str, str | None] = {}

    def record_asset(
        request: download_transport._DownloadRequest,
        limits: DownloadLimits,
    ) -> None:
        """Record the logical asset name and write the verified payload."""
        del limits
        captured["asset_name"] = request.asset_name
        request.destination.write_bytes(payload)

    monkeypatch.setattr(downloads, "_download", record_asset)

    download_pinned(asset, destination)

    assert captured["asset_name"] == "asset.bin", (
        "transport events must use the same asset value as downloader events"
    )


def test_download_pinned_reports_an_unreadable_cache_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache inspection failure is reported before any network work."""

    class UnreadablePath:
        """A destination whose `stat` reports an environmental failure."""

        def __init__(self, path: Path) -> None:
            """Wrap the path whose metadata lookups will fail."""
            self._path = path

        @property
        def parent(self) -> Path:
            """The parent directory of the wrapped path."""
            return self._path.parent

        def stat(self) -> os.stat_result:
            """Raise a permission error for every metadata lookup."""
            raise PermissionError(13, "permission denied", str(self._path))

        def __getattr__(self, name: str) -> object:
            """Forward every other attribute lookup to the wrapped path."""
            return getattr(self._path, name)

    calls: list[tuple[object, object]] = []

    def record_download(*args: object, **kwargs: object) -> None:
        """Record the download call without performing any transfer."""
        calls.append((args, kwargs))

    monkeypatch.setattr(downloads, "_download", record_download)
    asset = PinnedAsset(
        name="asset.bin",
        url="https://example.test/asset.bin",
        sha256="0" * 64,
        max_bytes=100,
    )
    destination = typ.cast("Path", UnreadablePath(tmp_path / "asset.bin"))

    with pytest.raises(ProverToolError, match="failed to inspect download cache"):
        download_pinned(asset, destination)

    assert not calls, "an unreadable cache entry must not trigger a download"


@contextlib.contextmanager
def closing_http_server(payload: bytes) -> cabc.Generator[int]:
    """Serve one `Connection: close` response with `payload` over loopback.

    Yields
    ------
    int
        The loopback port the server listens on.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        """Answer every GET with the scripted body and a close."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            """Send the payload and announce that the connection closes."""
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(
            self,
            format: str,  # ruff: ignore[builtin-argument-shadowing]
            *args: object,
        ) -> None:
            """Silence the server's stderr access log."""

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_deadline_response_reads_to_eof_after_the_connection_closes() -> None:
    """A close-delimited body reaches EOF without re-arming a closed socket.

    `http.client` releases the socket once the declared length is exhausted,
    and a `Connection: close` response also closed it when the headers were
    read. The next body read only observes EOF, so arming the captured socket
    then would raise `OSError: Bad file descriptor` and the complete body would
    be reported as a transient failure instead of reaching digest verification.
    """
    payload = b"verified"
    with closing_http_server(payload) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        connection.connect()
        sock = typ.cast("download_stream._TimeoutSocket", connection.sock)
        connection.request("GET", "/", headers={})
        raw = connection.getresponse()
        response = download_stream._DeadlineResponse(
            raw,
            deadline=time.monotonic() + 60.0,
            read_timeout_seconds=15.0,
            sock=sock,
        )
        try:
            chunks = []
            while chunk := response.read(64 * 1024):
                chunks.append(chunk)
        finally:
            connection.close()

    assert b"".join(chunks) == payload, "the whole body must survive EOF"
    assert raw.isclosed(), "the read that consumed the length closed the response"
