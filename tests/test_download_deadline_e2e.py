"""Behavioural tests for the transfer deadline at the public boundary.

The unit suites drive the transport's private seams with doubles. These cases
instead call `download_unverified` against a real loopback TLS server, so the
connect, handshake, send and body phases all run for real and only the URL and
timing are controlled by the test. They prove that a server which stalls a
network phase makes the public call fail as the deadline, within a bounded
time, rather than waiting out a socket timeout or being retried as a flaky
connection.
"""

from __future__ import annotations

import contextlib
import http.server
import socket
import ssl
import threading
import time
import typing as typ

import pytest

from rust_prover_tools.downloads import (
    BoundedAsset,
    DownloadLimits,
    download_unverified,
)
from rust_prover_tools.errors import ProverToolError, TransientDownloadError
from tests.kani_snapshot_tls_fixtures import (
    PRIVATE_KEY_PEM,
    trust_loopback_certificate,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

#: The budget the stalled-body case allows itself to be stopped by.
_TRANSFER_BUDGET = 1.0
#: The connect and read limit. It is deliberately far above the transfer
#: budget, so a phase that fell back to it would overrun the elapsed bound.
_PHASE_LIMIT = 10.0
#: A bound well above the transfer budget that still fails a phase limit.
_ELAPSED_LIMIT = 3.0


@pytest.fixture
def tls_material(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    """Write the loopback certificate and key and trust the certificate."""
    private_key = tmp_path / "loopback-key.pem"
    private_key.write_text(PRIVATE_KEY_PEM, encoding="utf-8")
    trust_loopback_certificate(monkeypatch, tmp_path)
    return tmp_path / "loopback-cert.pem", private_key


def _asset(port: int) -> BoundedAsset:
    """Return one unverified asset served from a loopback port."""
    return BoundedAsset(
        name="asset.bin",
        url=f"https://127.0.0.1:{port}/asset",
        max_bytes=1024,
    )


@contextlib.contextmanager
def _tls_server(
    site: type[http.server.BaseHTTPRequestHandler],
    material: tuple[Path, Path],
) -> cabc.Generator[int]:
    """Serve `site` over TLS on a loopback port for the block's duration."""
    certificate, private_key = material
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), site)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@contextlib.contextmanager
def _silent_listener() -> cabc.Generator[int]:
    """Accept one connection and never read or write it.

    A client cannot finish its TLS handshake against such a peer, which stalls
    the connect phase exactly as a wedged server does.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted: list[socket.socket] = []

    def accept() -> None:
        """Hold the connection open without answering."""
        connection, _ = listener.accept()
        accepted.append(connection)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        for connection in accepted:
            connection.close()
        listener.close()
        thread.join(timeout=10)


def _stalling_handler(
    release: threading.Event,
) -> type[http.server.BaseHTTPRequestHandler]:
    """Return a handler that sends a partial body and then stalls."""

    class StallingHandler(http.server.BaseHTTPRequestHandler):
        """Send headers and part of a body, then hold the rest back."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            """Send the response head and a partial body, then wait."""
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 10)
            self.wfile.flush()
            release.wait(10)

        def log_message(self, format: str, *args: object) -> None:  # ruff: ignore[builtin-argument-shadowing]
            """Silence the server's stderr access log."""

    return StallingHandler


def _assert_nothing_published(destination: Path) -> None:
    """Assert a failed transfer left neither the destination nor its pending file."""
    assert not destination.exists(), "a failed transfer publishes nothing"
    assert not destination.with_name(f"{destination.name}.download").exists(), (
        "a failed transfer removes its pending file"
    )


def test_a_stalled_connect_fails_as_the_deadline(
    tmp_path: Path,
) -> None:
    """A peer that never completes the handshake fails within the budget."""
    destination = tmp_path / "asset.bin"
    with _silent_listener() as port:
        started = time.monotonic()
        with pytest.raises(
            ProverToolError,
            match="exceeded the transfer deadline",
        ) as raised:
            download_unverified(
                _asset(port),
                destination,
                DownloadLimits(
                    connect_timeout_seconds=_PHASE_LIMIT,
                    transfer_timeout_seconds=0.5,
                ),
            )
        elapsed = time.monotonic() - started

    assert not isinstance(raised.value, TransientDownloadError), (
        "a stalled connect must not be retried as a flaky connection"
    )
    assert elapsed < _ELAPSED_LIMIT, (
        "the connect phase must be bounded by the transfer deadline, "
        f"took {elapsed:.1f}s"
    )
    _assert_nothing_published(destination)


def test_a_stalled_body_fails_as_the_deadline(
    tmp_path: Path,
    tls_material: tuple[Path, Path],
) -> None:
    """A body that stops arriving fails at the deadline, not at a timeout."""
    destination = tmp_path / "asset.bin"
    release = threading.Event()
    with _tls_server(_stalling_handler(release), tls_material) as port:
        started = time.monotonic()
        with pytest.raises(ProverToolError, match="transfer deadline") as raised:
            download_unverified(
                _asset(port),
                destination,
                DownloadLimits(
                    connect_timeout_seconds=_PHASE_LIMIT,
                    transfer_timeout_seconds=_TRANSFER_BUDGET,
                ),
            )
        elapsed = time.monotonic() - started
        release.set()

    assert not isinstance(raised.value, TransientDownloadError), (
        "a body the deadline cut short must not be retried as a stream fault"
    )
    assert elapsed < _ELAPSED_LIMIT, (
        f"the body read must be bounded by the transfer deadline, took {elapsed:.1f}s"
    )
    _assert_nothing_published(destination)
