"""Regression tests for the transfer deadline and the hop watchdog.

A hop must spend only the time the transfer has left, and the watchdog must
release whatever call is blocked at the deadline. These cases drive the budget
and the watchdog through the transport's own `_open`, because the ordering
between connecting, arming, watching and closing is the contract.
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rust_prover_tools import download_deadline, download_stream, download_transport
from rust_prover_tools.downloads import DownloadLimits
from rust_prover_tools.errors import ProverToolError, TransientDownloadError
from rust_prover_tools.unittests.download_support import (
    FakeClock,
    FakeHopConnection,
    FakeResponse,
    FakeSocket,
    GatedClock,
    HopFactory,
    HopWorker,
    download_request,
    events_from,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

# Hypothesis cannot reset `monkeypatch` between examples because it is a
# teardown fixture. Each example installs the same replacement, so reuse is
# safe.
REUSABLE_FIXTURES = [HealthCheck.function_scoped_fixture]

#: A finite, positive duration in seconds: a connect limit or a time left.
DURATION = st.floats(
    min_value=1e-3,
    max_value=1_200.0,
    allow_nan=False,
    allow_infinity=False,
)


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

    hop = FakeHopConnection(sock=sock)
    hop.on_request = block_until_shutdown
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

    hop = FakeHopConnection(sock=sock)
    hop.on_response = block_until_shutdown
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
    )
    hop.clear_sock_on_response = True
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
    )
    body = FakeHopConnection(response=FakeResponse(chunks=(b"payload",)))
    redirect.on_close = record_close
    body.on_close = record_close
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


def test_watchdog_shutdown_preserves_a_tls_socket_wrapper() -> None:
    """A TLS socket keeps its SSL object when the watchdog shuts it down.

    `SSLSocket.shutdown` clears `_sslobj` before shutting the descriptor down,
    so a read that already passed its own "is there a wrapper?" check can then
    dereference the cleared field and fail with `AttributeError`, which no
    caller classifies as the deadline. The watchdog shuts the descriptor down
    through the base class instead. The private attribute is the invariant
    here, so the test inspects it directly.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    client, server = socket.socketpair()
    tls = context.wrap_socket(
        client,
        server_hostname="127.0.0.1",
        do_handshake_on_connect=False,
    )
    watchdog = download_deadline._DeadlineWatchdog(
        typ.cast("download_stream._DeadlineSocket", tls),
        float("inf"),
    )
    try:
        wrapper = getattr(tls, "_sslobj", None)
        assert wrapper is not None, "the wrapper must exist before the test"
        watchdog._shut_down()
        assert getattr(tls, "_sslobj", None) is not None, (
            "shutting the socket down must not discard its TLS wrapper"
        )
    finally:
        tls.close()
        server.close()


def test_open_refuses_a_response_that_arrives_after_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete header block that arrives at the deadline is still the deadline.

    The watchdog can shut the socket down while the headers are parsed and
    leave a complete header block behind, so the hop must not hand that
    response over to be judged by its status instead of its timing.
    """
    clock = FakeClock()
    monkeypatch.setattr(download_deadline, "monotonic", clock.monotonic)
    hop = FakeHopConnection(sock=FakeSocket())

    def finish_late() -> None:
        """Spend the whole deadline and return the response normally."""
        clock.advance(2.0)

    hop.on_response = finish_late
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        HopFactory(hop),
    )

    with pytest.raises(ProverToolError, match="exceeded the transfer deadline"):
        download_transport._open(
            "https://example.test/asset.bin",
            DownloadLimits(),
            1.0,
        )

    assert hop.responses == 1, "the response must be acquired before the check"
    assert hop.closed, "the refused hop must close its connection"


@given(connect_timeout=DURATION, remaining=DURATION)
@settings(max_examples=100, deadline=None, suppress_health_check=REUSABLE_FIXTURES)
def test_hop_timeout_never_exceeds_the_time_left(
    monkeypatch: pytest.MonkeyPatch,
    connect_timeout: float,
    remaining: float,
) -> None:
    """The hop budget is the smaller of the limit and the time left, and positive.

    A hop with a whole limit to spare keeps the limit; a hop with less left
    gets only that. The budget is never zero, because a zero socket timeout
    makes the socket non-blocking rather than failing it.
    """
    monkeypatch.setattr(download_deadline, "monotonic", lambda: 0.0)

    budget = download_deadline._hop_timeout(
        "https://example.test/asset.bin",
        connect_timeout,
        remaining,
    )

    assert budget == pytest.approx(min(connect_timeout, remaining)), (
        "the hop must be bounded by the smaller of its limit and the time left"
    )
    assert budget > 0.0, "a hop budget must never be zero or negative"


@given(connect_timeout=DURATION, elapsed=DURATION)
@settings(max_examples=100, deadline=None, suppress_health_check=REUSABLE_FIXTURES)
def test_hop_timeout_refuses_every_spent_budget(
    monkeypatch: pytest.MonkeyPatch,
    connect_timeout: float,
    elapsed: float,
) -> None:
    """A hop with no time left is refused rather than given a zero budget."""
    monkeypatch.setattr(download_deadline, "monotonic", lambda: elapsed)

    with pytest.raises(ProverToolError, match="exceeded the transfer deadline"):
        download_deadline._hop_timeout(
            "https://example.test/asset.bin",
            connect_timeout,
            0.0,
        )


@given(connect_timeout=DURATION, remaining=DURATION)
@settings(max_examples=100, deadline=None, suppress_health_check=REUSABLE_FIXTURES)
def test_arm_socket_uses_the_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
    connect_timeout: float,
    remaining: float,
) -> None:
    """Re-arming before the send uses the same budget the hop was sized with."""
    monkeypatch.setattr(download_deadline, "monotonic", lambda: 0.0)
    sock = FakeSocket()
    hop = FakeHopConnection(sock=sock)

    armed = download_deadline._arm_socket(
        typ.cast("download_transport.http.client.HTTPSConnection", hop),
        "https://example.test/asset.bin",
        connect_timeout,
        remaining,
    )

    assert armed is sock, "the hop must arm the capture it will watch"
    assert sock.timeouts == [pytest.approx(min(connect_timeout, remaining))], (
        "the send must run under the time the connect phase left"
    )
