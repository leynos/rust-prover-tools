"""Shared doubles for the bounded download transport tests.

Each helper builds a fake clock, socket, response or connection, so a
transport test runs without a real network call, a real seconds-long wait, or
a patched module global beyond the connection factory. The doubles are used by
both the general download tests and the deadline-specific ones.
"""

from __future__ import annotations

import threading
import time
import typing as typ

from rust_prover_tools import download_stream, download_transport

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    import pytest

#: Tests that exercise byte limits rather than time keep the deadline open.
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

    The transport constructs its connection, so a test builds this double and
    hands it out through `HopFactory`. `connect()` creates the socket the
    transport captures, which keeps the constructor timeout and the pre-send
    re-arm distinguishable in `sock.timeouts`.

    The phase hooks are ordinary attributes rather than constructor arguments:
    a test sets only the phases it scripts, and a phase it leaves unset does
    its recording and nothing else.
    """

    def __init__(
        self,
        *,
        sock: FakeSocket | None = None,
        clock: FakeClock | None = None,
        connect_seconds: float = 0.0,
        response: FakeResponse | None = None,
    ) -> None:
        """Configure the socket, the connect cost and the response to serve."""
        self.sock = sock
        self.host: str | None = None
        self.port: int | None = None
        self.timeout: float | None = None
        self.requests: list[dict[str, object]] = []
        self.responses = 0
        self.closed = False
        #: Clear `sock` in `getresponse()`, as `http.client` does when the
        #: response cannot reuse the connection.
        self.clear_sock_on_response = False
        #: Work each phase performs, in addition to its recording.
        self.on_request: cabc.Callable[[], None] | None = None
        self.on_response: cabc.Callable[[], None] | None = None
        self.on_close: cabc.Callable[[], None] | None = None
        self._clock = clock
        self._connect_seconds = connect_seconds
        self._response = response or FakeResponse(chunks=(b"payload",))

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
        if self.on_request is not None:
            self.on_request()

    def getresponse(self) -> FakeResponse:
        """Return the canned response, clearing the socket when scripted."""
        self.responses += 1
        if self.on_response is not None:
            self.on_response()
        if self.clear_sock_on_response:
            self.sock = None
        return self._response

    def close(self) -> None:
        """Record closure, then run any scripted close work."""
        self.closed = True
        if self.on_close is not None:
            self.on_close()


class RecordedConnection:
    """One connection exactly as the transport constructed and used it."""

    def __init__(self, sock: FakeSocket, response: FakeResponse) -> None:
        """Bind the socket the connect phase creates and the response it serves."""
        self.sock: FakeSocket | None = None
        self.host: str | None = None
        self.port: int | None = None
        self.timeout: float | None = None
        self.method: str | None = None
        self.target: str | None = None
        self.headers: dict[str, str] | None = None
        self.closed = False
        self._socket = sock
        self._response = response

    def connect(self) -> None:
        """Create the socket the transport captures and re-arms."""
        self.sock = self._socket

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str],
    ) -> None:
        """Record the method, target and headers of one request."""
        self.method = method
        self.target = target
        self.headers = headers

    def getresponse(self) -> FakeResponse:
        """Return the canned response."""
        return self._response

    def close(self) -> None:
        """Record closure."""
        self.closed = True


class ConnectionRecorder:
    """Record the constructor and request of every transport connection.

    A test that needs the transport's own constructor arguments patches
    `HTTPSConnection` with this recorder. It builds one `RecordedConnection`
    per call, so the constructor's host, port and timeout sit beside the
    request the same connection then carried.
    """

    def __init__(self, sock: FakeSocket, response: FakeResponse) -> None:
        """Bind the socket connect() creates and the response it serves."""
        self._sock = sock
        self._response = response
        self.connections: list[RecordedConnection] = []

    def __call__(
        self,
        host: str,
        port: int | None,
        *,
        timeout: float,
    ) -> RecordedConnection:
        """Record the constructor arguments and return the new connection."""
        connection = RecordedConnection(self._sock, self._response)
        connection.host = host
        connection.port = port
        connection.timeout = timeout
        self.connections.append(connection)
        return connection


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
