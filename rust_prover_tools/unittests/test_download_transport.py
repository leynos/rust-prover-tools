"""Regression tests for the bounded HTTPS transport request path."""

from __future__ import annotations

import logging

import pytest

from rust_prover_tools import download_transport
from rust_prover_tools.downloads import DownloadLimits
from rust_prover_tools.errors import TransientDownloadError
from rust_prover_tools.unittests.download_support import (
    ConnectionRecorder,
    FakeResponse,
    FakeSocket,
    events_from,
)


def test_open_requests_the_validated_path_with_identity_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport requests the resource path and asks for an identity body."""
    recorder = ConnectionRecorder(FakeSocket(), FakeResponse(chunks=(b"payload",)))
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        recorder,
    )

    response, hop = download_transport._open(
        "https://example.test:8443/asset.bin?token=abc",
        DownloadLimits(connect_timeout_seconds=5.0),
        download_transport.monotonic() + 60.0,
    )
    try:
        assert response.status == 200, "the response status must be returned"
        assert response.read(7) == b"payload", "the response body must be readable"
    finally:
        hop.close()

    (connection,) = recorder.connections
    assert connection.host == "example.test", "the host must come from the URL"
    assert connection.port == 8443, "the port must come from the URL"
    assert connection.method == "GET", "the request must be a GET"
    assert connection.target == "/asset.bin?token=abc", (
        "the path and query must be requested"
    )
    assert connection.headers == {
        "Accept-Encoding": "identity",
        "User-Agent": download_transport.USER_AGENT,
    }, "the request must ask for an identity body"


def test_open_arms_the_send_and_the_read_and_closes_the_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The send and the body read are each armed, and the handle closes."""
    sock = FakeSocket()
    recorder = ConnectionRecorder(sock, FakeResponse(chunks=(b"payload",)))
    monkeypatch.setattr(
        download_transport.http.client,
        "HTTPSConnection",
        recorder,
    )

    response, hop = download_transport._open(
        "https://example.test/asset.bin",
        DownloadLimits(connect_timeout_seconds=5.0),
        download_transport.monotonic() + 60.0,
    )
    try:
        assert response.read(7) == b"payload", "the response body must be readable"
    finally:
        hop.close()

    assert sock.timeouts == [5.0, 5.0], (
        "the send and the body read must each be armed with the configured timeout"
    )
    assert isinstance(hop, download_transport._HopHandle), (
        "a hop handle must be returned"
    )
    assert recorder.connections[0].closed is True, (
        "the hop handle must close the connection"
    )


def test_open_reports_a_failed_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connection error is reported and the connection is closed."""
    caplog.set_level(logging.DEBUG, logger="rust_prover_tools")
    connections: list[FailingConnection] = []

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
    assert {
        "event": "download.failed",
        "reason": "transport_error",
    } in events_from(caplog), "a transport failure must not report an asset-name label"
