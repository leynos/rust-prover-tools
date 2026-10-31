"""HTTPS transport for bounded, verified prover downloads.

This module performs the network-facing half of
`rust_prover_tools.downloads`. It accepts only `https` URLs, follows a bounded
number of redirects, rejects a `Content-Length` larger than the asset's ceiling
before reading the body, counts streamed bytes so a response without a length
header cannot fill the disk, and applies one monotonic deadline to connection,
redirects, and body transfer.
"""

from __future__ import annotations

import dataclasses as dc
import http.client
import logging
import typing as typ
import urllib.parse
from time import monotonic

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
READ_CHUNK_SIZE: typ.Final[int] = 64 * 1024
REDIRECT_STATUSES: typ.Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})
USER_AGENT: typ.Final[str] = "rust-prover-tools"

_LOGGER = logging.getLogger(__name__)


class HTTPResponseLike(typ.Protocol):
    """The response surface used by the streaming helpers.

    The `getheader` overloads mirror `http.client.HTTPResponse` so a real
    response satisfies the protocol.
    """

    status: int

    @typ.overload
    def getheader(self, name: str) -> str | None: ...

    @typ.overload
    def getheader[T](self, name: str, default: T) -> str | T: ...

    def read(self, amt: int | None = None) -> bytes:
        """Read up to `amt` response bytes."""
        ...


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
                    _log_failure(name, "redirect_limit")
                    msg = f"too many redirects while downloading {request.url}"
                    raise ProverToolError(msg)
                try:
                    current = _validate_url(urllib.parse.urljoin(current, location))
                except ProverToolError:
                    _log_failure(name, "redirect_target")
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
    _log_failure(name, "redirect_limit")
    msg = f"too many redirects while downloading {request.url}"
    raise ProverToolError(msg)


def _log_failure(asset_name: str, reason: str, **fields: object) -> None:
    """Record one bounded download failure event."""
    log_event(
        _LOGGER,
        "download.failed",
        asset=asset_name,
        reason=reason,
        **fields,
    )


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
        _log_failure(asset_name, "deadline")
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
        response = connection.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        connection.close()
        _log_failure(asset_name, "transport_error")
        msg = f"failed to download {url}: {exc}"
        raise ProverToolError(msg) from exc
    return response, connection


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
        _log_failure(asset_name, "redirect_location", status=response.status)
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
        asset_name,
        "http_status",
        status=response.status,
        status_class=f"{response.status // 100}xx",
    )
    msg = f"download of {url} failed with HTTP status {response.status}"
    raise ProverToolError(msg)


def _content_length(response: HTTPResponseLike, asset_name: str) -> int | None:
    """Return a valid non-negative `Content-Length`, when present."""
    raw = response.getheader("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        _log_failure(asset_name, "invalid_content_length")
        msg = f"invalid Content-Length '{raw}' from {asset_name}"
        raise ProverToolError(msg) from exc
    if value < 0:
        _log_failure(asset_name, "invalid_content_length", declared=value)
        msg = f"invalid negative Content-Length '{raw}' from {asset_name}"
        raise ProverToolError(msg)
    return value


@dc.dataclass(frozen=True, slots=True)
class _StreamLimits:
    """Limits that apply while copying one response body to disk."""

    name: str
    max_bytes: int
    deadline: float


def _stream(
    response: HTTPResponseLike,
    destination: Path,
    limits: _StreamLimits,
) -> int:
    """Write a response body, enforcing the byte ceiling and deadline.

    The caller supplies the limits so the logical asset name, byte ceiling, and
    deadline travel together; transport events then join with the downloader's
    events. The byte count is returned so the caller can report the transferred
    size without re-reading the file.
    """
    name = limits.name
    expected = _content_length(response, name)
    _reject_declared_oversize(limits, expected)
    try:
        with destination.open("wb") as output:
            written = _copy_body(response, output, limits)
    except (OSError, http.client.HTTPException) as exc:
        _log_failure(name, "stream_error")
        msg = f"failed to download {name}: {exc}"
        raise ProverToolError(msg) from exc
    _require_complete_body(limits, written, expected)
    return written


def _reject_declared_oversize(
    limits: _StreamLimits,
    declared: int | None,
) -> None:
    """Reject a `Content-Length` above the ceiling before reading the body."""
    if declared is None or declared <= limits.max_bytes:
        return
    _log_failure(
        limits.name,
        "content_length",
        declared=declared,
        max_bytes=limits.max_bytes,
    )
    msg = (
        f"refusing to download {limits.name}: "
        f"Content-Length {declared} exceeds the {limits.max_bytes}-byte limit"
    )
    raise ProverToolError(msg)


def _copy_body(
    response: HTTPResponseLike,
    output: typ.BinaryIO,
    limits: _StreamLimits,
) -> int:
    """Copy the body to `output`, returning the number of bytes written."""
    written = 0
    while True:
        if monotonic() > limits.deadline:
            _log_failure(limits.name, "deadline", written=written)
            msg = f"download of {limits.name} exceeded the transfer deadline"
            raise ProverToolError(msg)
        chunk = response.read(READ_CHUNK_SIZE)
        if not chunk:
            return written
        written += len(chunk)
        if written > limits.max_bytes:
            _log_failure(
                limits.name,
                "size_limit",
                written=written,
                max_bytes=limits.max_bytes,
            )
            msg = (
                f"refusing to download {limits.name}: "
                f"streamed size exceeded the {limits.max_bytes}-byte limit"
            )
            raise ProverToolError(msg)
        output.write(chunk)


def _require_complete_body(
    limits: _StreamLimits,
    written: int,
    expected: int | None,
) -> None:
    """Reject a body shorter than its declared `Content-Length`."""
    if expected is None or written == expected:
        return
    _log_failure(
        limits.name,
        "incomplete_body",
        written=written,
        expected=expected,
    )
    msg = (
        f"incomplete download of {limits.name}: "
        f"received {written} bytes, expected {expected}"
    )
    raise ProverToolError(msg)
