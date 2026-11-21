"""Regression tests for the bounded HTTPS transport request path."""

from __future__ import annotations

import logging
import typing as typ

import pytest

from rust_prover_tools import download_transport
from rust_prover_tools.downloads import DownloadLimits
from rust_prover_tools.errors import TransientDownloadError
from rust_prover_tools.unittests.download_support import (
    FakeResponse,
    FakeSocket,
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
