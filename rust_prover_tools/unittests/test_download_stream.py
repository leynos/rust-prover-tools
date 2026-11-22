"""Regression tests for deadline-bounded response handling.

The transport opens and sends under the transfer deadline; these cases cover
what the body side does with it. Each read is bounded by the time left, a read
that fails or ends at the deadline is reported as the deadline rather than as a
retryable stream fault, and a finished response is never re-armed, because
`http.client` may already have closed its socket.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import http.client
import http.server
import logging
import threading
import time
import typing as typ

import pytest

from rust_prover_tools import download_deadline, download_stream, download_transport
from rust_prover_tools.downloads import DownloadLimits
from rust_prover_tools.errors import ProverToolError, TransientDownloadError
from rust_prover_tools.unittests.download_support import (
    NO_DEADLINE,
    FakeClock,
    FakeHopConnection,
    FakeResponse,
    FakeSocket,
    GatedClock,
    HopFactory,
    HopWorker,
    events_from,
    stream_limits,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


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


@dc.dataclass(frozen=True, slots=True)
class _TimeoutCase:
    """One read timeout and where it falls relative to the deadline."""

    elapsed: float
    deadline: float
    error_match: str
    event: dict[str, object]


@pytest.mark.parametrize(
    "case",
    [
        _TimeoutCase(
            2.0,
            1.0,
            "transfer deadline",
            {"event": "download.failed", "reason": "deadline", "written": 0},
        ),
        _TimeoutCase(
            0.1,
            10.0,
            "failed to download",
            {"event": "download.failed", "reason": "stream_error"},
        ),
    ],
    ids=["at-the-deadline", "before-the-deadline"],
)
def test_stream_classifies_a_read_timeout_by_the_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: _TimeoutCase,
) -> None:
    """A read timeout is the deadline at or past it, and a stream error before.

    The watchdog shuts the socket down at the deadline, so the timeout it
    produces must not be retried as a flaky connection. Before the deadline the
    same exception is the server's failure and stays transient.
    """
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")
    clock = FakeClock()
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)

    class TimingOutResponse(FakeResponse):
        """A response whose read spends `elapsed` and then times out."""

        def read(self, amt: int | None = None) -> bytes:
            """Spend the scripted time and time out."""
            del amt
            clock.advance(case.elapsed)
            msg = "timed out"
            raise TimeoutError(msg)

    with pytest.raises(ProverToolError, match=case.error_match):
        download_stream._stream(
            TimingOutResponse(),
            tmp_path / "asset.bin",
            stream_limits(10, case.deadline),
        )

    assert events_from(caplog)[-1] == case.event, (
        "the failure must be classified by where the read ended relative to "
        "the deadline"
    )


def test_stream_reports_a_blocked_read_at_the_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A body read the watchdog cuts short is the transfer's deadline."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")
    clock = GatedClock()
    # The pre-read check and the watchdog must both start after the gate opens
    # only once the blocked read is in flight; a real clock could trip the
    # check before `BlockingResponse.read()` runs, which would pass the test
    # without ever exercising the watchdog.
    monkeypatch.setattr(download_deadline, "monotonic", clock.monotonic)
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)
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


@dc.dataclass(frozen=True, slots=True)
class _EofCase:
    """One short body that ends at the deadline, with its declared length."""

    headers: dict[str, str]


@pytest.mark.parametrize(
    "case",
    [
        _EofCase({"Content-Length": "10"}),
        _EofCase({}),
    ],
    ids=["declared-length", "no-length"],
)
def test_stream_reports_a_late_eof_as_the_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: _EofCase,
) -> None:
    """A body that ends at the deadline is the deadline, not a short body.

    The watchdog releases a blocked read by shutting the socket down, which
    reads as EOF. The deadline is therefore checked again before an empty
    chunk is accepted, so a stalled body cannot pass as complete or be
    retried as a truncated transfer.
    """
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools.download_stream")
    clock = FakeClock()
    monkeypatch.setattr(download_stream, "monotonic", clock.monotonic)

    class LateEofResponse(FakeResponse):
        """A response whose body ends only after the deadline."""

        def read(self, amt: int | None = None) -> bytes:
            """Spend past the deadline and report the end of the body."""
            del amt
            clock.advance(2.0)
            return b""

    with pytest.raises(ProverToolError, match="transfer deadline"):
        download_stream._stream(
            LateEofResponse(headers=case.headers),
            tmp_path / "asset.bin",
            stream_limits(100, 1.0),
        )

    assert events_from(caplog)[-1] == {
        "event": "download.failed",
        "reason": "deadline",
        "written": 0,
    }, "a body that ends at the deadline must report the deadline"
