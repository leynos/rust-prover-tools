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
import typing as typ
import urllib.parse
from time import monotonic

from rust_prover_tools.errors import ProverToolError

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


_HeaderDefault = typ.TypeVar("_HeaderDefault")


class HTTPResponseLike(typ.Protocol):
    """The response surface used by the streaming helpers.

    The `getheader` overloads mirror `http.client.HTTPResponse` so a real
    response satisfies the protocol.
    """

    status: int

    @typ.overload
    def getheader(self, name: str) -> str | None: ...

    @typ.overload
    def getheader(self, name: str, default: _HeaderDefault) -> str | _HeaderDefault: ...

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
class _HTTPSUrl:
    """Validated HTTPS URL components."""

    host: str
    port: int | None
    path: str
    query: str


def _download(
    url: str,
    destination: Path,
    max_bytes: int,
    limits: DownloadLimits,
) -> None:
    """Fetch `url` into `destination`, following bounded redirects."""
    deadline = monotonic() + limits.transfer_timeout_seconds
    current = _validate_url(url)
    for hop in range(limits.max_redirects + 1):
        response, connection = _open(current, limits, deadline)
        try:
            location = _redirect_location(response, current)
            if location is not None:
                if hop >= limits.max_redirects:
                    msg = f"too many redirects while downloading {url}"
                    raise ProverToolError(msg)
                current = _validate_url(urllib.parse.urljoin(current, location))
                continue
            _require_success(response, current)
            _stream(response, destination, max_bytes, deadline)
            return
        finally:
            connection.close()
    msg = f"too many redirects while downloading {url}"
    raise ProverToolError(msg)


def _open(
    url: str,
    limits: DownloadLimits,
    deadline: float,
) -> tuple[HTTPResponseLike, http.client.HTTPSConnection]:
    """Request `url` and return the response with its connection."""
    parsed = _parse_https_url(url)
    remaining = deadline - monotonic()
    if remaining <= 0:
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
        msg = f"failed to download {url}: {exc}"
        raise ProverToolError(msg) from exc
    return response, connection


def _parse_https_url(url: str) -> _HTTPSUrl:
    """Return parsed URL components after enforcing the HTTPS policy."""
    try:
        parsed = urllib.parse.urlsplit(url)
        # Accessing ``port`` may itself raise for a malformed port.
        port = parsed.port
    except ValueError as exc:
        msg = f"invalid download URL '{url}': {exc}"
        raise ProverToolError(msg) from exc
    if parsed.scheme.lower() != "https":
        msg = f"download URL must use https, got '{url}'"
        raise ProverToolError(msg)
    host = parsed.hostname
    if host is None:
        msg = f"download URL has no host: '{url}'"
        raise ProverToolError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = f"download URL must not contain credentials: '{url}'"
        raise ProverToolError(msg)
    return _HTTPSUrl(host=host, port=port, path=parsed.path, query=parsed.query)


def _validate_url(url: str) -> str:
    """Validate a URL and return it unchanged for redirect bookkeeping."""
    _parse_https_url(url)
    return url


def _redirect_location(
    response: HTTPResponseLike,
    url: str,
) -> str | None:
    """Return a redirect target, `None` for success, or raise for a bad hop."""
    if response.status not in REDIRECT_STATUSES:
        return None
    location = response.getheader("Location")
    if not location:
        msg = f"redirect response from {url} has no Location header"
        raise ProverToolError(msg)
    return location


def _require_success(response: HTTPResponseLike, url: str) -> None:
    """Raise unless the response is a complete HTTP 200."""
    if response.status == HTTP_OK:
        return
    msg = f"download of {url} failed with HTTP status {response.status}"
    raise ProverToolError(msg)


def _content_length(
    response: HTTPResponseLike,
    destination: Path,
) -> int | None:
    """Return a valid non-negative `Content-Length`, when present."""
    raw = response.getheader("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"invalid Content-Length '{raw}' from {destination.name}"
        raise ProverToolError(msg) from exc
    if value < 0:
        msg = f"invalid negative Content-Length '{raw}' from {destination.name}"
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
    max_bytes: int,
    deadline: float,
) -> None:
    """Write a response body, enforcing the byte ceiling and deadline."""
    expected = _content_length(response, destination)
    limits = _StreamLimits(destination.name, max_bytes, deadline)
    _reject_declared_oversize(limits, expected)
    try:
        with destination.open("wb") as output:
            written = _copy_body(response, output, limits)
    except (OSError, http.client.HTTPException) as exc:
        msg = f"failed to download {destination.name}: {exc}"
        raise ProverToolError(msg) from exc
    _require_complete_body(limits, written, expected)


def _reject_declared_oversize(
    limits: _StreamLimits,
    declared: int | None,
) -> None:
    """Reject a `Content-Length` above the ceiling before reading the body."""
    if declared is None or declared <= limits.max_bytes:
        return
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
            msg = f"download of {limits.name} exceeded the transfer deadline"
            raise ProverToolError(msg)
        chunk = response.read(READ_CHUNK_SIZE)
        if not chunk:
            return written
        written += len(chunk)
        if written > limits.max_bytes:
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
    msg = (
        f"incomplete download of {limits.name}: "
        f"received {written} bytes, expected {expected}"
    )
    raise ProverToolError(msg)
