"""HTTPS transport for bounded, verified prover downloads.

This module performs the connection-facing half of
`rust_prover_tools.downloads`. It accepts only `https` URLs, follows a bounded
number of redirects, and applies one monotonic deadline that starts before the
first connection. The body rules live in
`rust_prover_tools.download_stream`: it rejects an oversized `Content-Length`
before reading, counts streamed bytes so a response without a length header
cannot fill the disk, and stops a body read at the deadline.
"""

from __future__ import annotations

import dataclasses as dc
import http.client
import logging
import typing as typ
import urllib.parse
from time import monotonic

from rust_prover_tools.download_stream import (
    HTTPResponseLike,
    _DeadlineResponse,
    _log_failure,
    _stream,
    _StreamLimits,
    _TimeoutSocket,
)
from rust_prover_tools.download_url import _parse_https_url, _validate_url
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_DOWNLOAD_LIMITS",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_TRANSFER_TIMEOUT_SECONDS",
    "DownloadLimits",
)

DEFAULT_CONNECT_TIMEOUT_SECONDS: typ.Final[float] = 15.0
DEFAULT_TRANSFER_TIMEOUT_SECONDS: typ.Final[float] = 300.0
DEFAULT_MAX_REDIRECTS: typ.Final[int] = 5
HTTP_OK: typ.Final[int] = 200
REDIRECT_STATUSES: typ.Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})
USER_AGENT: typ.Final[str] = "rust-prover-tools"

_LOGGER = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True)
class DownloadLimits:
    """Network limits applied to one pinned asset download.

    Attributes
    ----------
    connect_timeout_seconds : float
        Socket timeout used while connecting and reading. Defaults to 15
        seconds.
    transfer_timeout_seconds : float
        Overall deadline for connection, redirects, and body transfer.
        Defaults to 300 seconds.
    max_redirects : int
        Maximum redirect hops followed before the download fails. Defaults to
        5, so at most 6 requests are made.
    """

    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    transfer_timeout_seconds: float = DEFAULT_TRANSFER_TIMEOUT_SECONDS
    max_redirects: int = DEFAULT_MAX_REDIRECTS


DEFAULT_DOWNLOAD_LIMITS: typ.Final[DownloadLimits] = DownloadLimits()


@dc.dataclass(frozen=True, slots=True)
class _DownloadRequest:
    """One bounded download and the asset identity its events report.

    `asset_name` defaults to the destination file name for direct callers;
    `download_pinned` supplies the pinned asset name so transport events join
    with the downloader's own events.
    """

    url: str
    destination: Path
    max_bytes: int
    asset_name: str | None = None

    @property
    def name(self) -> str:
        """Logical asset name reported by every event."""
        return self.asset_name or self.destination.name


def _download(request: _DownloadRequest, limits: DownloadLimits) -> None:
    """Fetch `request.url` into `request.destination`, following redirects."""
    name = request.name
    started = monotonic()
    deadline = started + limits.transfer_timeout_seconds
    current = _validate_url(request.url)
    log_event(_LOGGER, "download.start", asset=name, max_bytes=request.max_bytes)
    for hop in range(limits.max_redirects + 1):
        response, connection = _open(current, limits, deadline, asset_name=name)
        try:
            location = _redirect_location(response, current, asset_name=name)
            if location is not None:
                if hop >= limits.max_redirects:
                    _log_failure(_LOGGER, name, "redirect_limit")
                    msg = f"too many redirects while downloading {request.url}"
                    raise ProverToolError(msg)
                try:
                    current = _validate_url(urllib.parse.urljoin(current, location))
                except ProverToolError:
                    _log_failure(_LOGGER, name, "redirect_target")
                    raise
                continue
            _require_success(response, current, asset_name=name)
            written = _stream(
                response,
                request.destination,
                _StreamLimits(name, request.max_bytes, deadline),
            )
            log_event(
                _LOGGER,
                "download.complete",
                asset=name,
                written=written,
                redirects=hop,
                elapsed_seconds=monotonic() - started,
            )
            return
        finally:
            connection.close()
    _log_failure(_LOGGER, name, "redirect_limit")
    msg = f"too many redirects while downloading {request.url}"
    raise ProverToolError(msg)


def _open(
    url: str,
    limits: DownloadLimits,
    deadline: float,
    *,
    asset_name: str,
) -> tuple[HTTPResponseLike, http.client.HTTPSConnection]:
    """Request `url` and return the response with its connection."""
    parsed = _parse_https_url(url)
    remaining = deadline - monotonic()
    if remaining <= 0:
        _log_failure(_LOGGER, asset_name, "deadline")
        msg = f"download of {url} exceeded the transfer deadline"
        raise ProverToolError(msg)
    timeout = min(limits.connect_timeout_seconds, remaining)
    connection = http.client.HTTPSConnection(
        parsed.host,
        parsed.port,
        timeout=timeout,
    )
    target = urllib.parse.urlunsplit(
        ("", "", parsed.path or "/", parsed.query, ""),
    )
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
            },
        )
        # Capture the socket before `getresponse()`: a response that cannot be
        # reused closes the connection and clears `connection.sock`, although
        # the response still reads from the same socket.
        sock = typ.cast("_TimeoutSocket | None", getattr(connection, "sock", None))
        response = connection.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        connection.close()
        _log_failure(_LOGGER, asset_name, "transport_error")
        msg = f"failed to download {url}: {exc}"
        raise ProverToolError(msg) from exc
    return (
        _DeadlineResponse(
            response,
            deadline=deadline,
            read_timeout_seconds=limits.connect_timeout_seconds,
            sock=sock,
        ),
        connection,
    )


def _redirect_location(
    response: HTTPResponseLike,
    url: str,
    *,
    asset_name: str,
) -> str | None:
    """Return a redirect target, `None` for success, or raise for a bad hop."""
    if response.status not in REDIRECT_STATUSES:
        return None
    location = response.getheader("Location")
    if not location:
        _log_failure(
            _LOGGER,
            asset_name,
            "redirect_location",
            status=response.status,
        )
        msg = f"redirect response from {url} has no Location header"
        raise ProverToolError(msg)
    return location


def _require_success(
    response: HTTPResponseLike,
    url: str,
    *,
    asset_name: str,
) -> None:
    """Raise unless the response is a complete HTTP 200."""
    if response.status == HTTP_OK:
        return
    _log_failure(
        _LOGGER,
        asset_name,
        "http_status",
        status=response.status,
        status_class=f"{response.status // 100}xx",
    )
    msg = f"download of {url} failed with HTTP status {response.status}"
    raise ProverToolError(msg)
