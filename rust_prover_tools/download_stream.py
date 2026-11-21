"""Streaming body transfer for bounded prover downloads.

This is the body-handling half of `rust_prover_tools.download_transport`. It
wraps each response read so the socket cannot outlive the transfer deadline,
rejects an oversized `Content-Length` before the destination opens, counts
streamed bytes so a response without a length header cannot fill the disk, and
rejects a body shorter than its declared length.

One monotonic deadline covers the request that opens a hop, the header parse
and every body read. Each read re-arms the socket with the time that is left,
and a read that fails at or after the deadline, including the end of stream a
shut-down socket produces, is reported as the deadline rather than as a
transport fault. A body written to disk is not itself supervised: local file
I/O can still block past the deadline.

The connection and redirect policy stays in
`rust_prover_tools.download_transport`; this module owns nothing about how the
response was obtained.
"""

from __future__ import annotations

import dataclasses as dc
import http.client
import logging
import typing as typ
from time import monotonic

from rust_prover_tools.errors import ProverToolError, TransientDownloadError
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    from pathlib import Path

READ_CHUNK_SIZE: typ.Final[int] = 64 * 1024

_LOGGER = logging.getLogger(__name__)


class HTTPResponseLike(typ.Protocol):
    """The response surface used by the streaming helpers.

    The `getheader` overloads mirror `http.client.HTTPResponse` so a real
    response satisfies the protocol.
    """

    @property
    def status(self) -> int:
        """The HTTP status code."""
        ...

    @typ.overload
    def getheader(self, name: str) -> str | None: ...

    @typ.overload
    def getheader[T](self, name: str, default: T) -> str | T: ...

    def read(self, amt: int | None = None) -> bytes:
        """Read up to `amt` response bytes."""
        ...


class _TimeoutSocket(typ.Protocol):
    """The socket operation needed to bound a response read."""

    def settimeout(self, value: float) -> None:
        """Apply `value` seconds as the socket's timeout."""
        ...


class _DeadlineSocket(_TimeoutSocket, typ.Protocol):
    """A timeout socket that can also be released at the deadline.

    `settimeout` arms one blocking call, while `shutdown` releases a call that
    is already blocked. A shut-down socket reads as end of stream, which is why
    a late EOF is classified as the deadline too.
    """

    def shutdown(self, how: int) -> None:
        """Shut the socket down, waking any call blocked on it."""
        ...


@dc.dataclass(slots=True)
class _DeadlineResponse:
    """A response that bounds each body read by the remaining time."""

    response: HTTPResponseLike
    deadline: float
    read_timeout_seconds: float
    sock: _TimeoutSocket | None = None

    @property
    def status(self) -> int:
        """The wrapped response's HTTP status."""
        return self.response.status

    @typ.overload
    def getheader(self, name: str) -> str | None: ...

    @typ.overload
    def getheader[T](self, name: str, default: T) -> str | T: ...

    def getheader(self, name: str, default: object = None) -> object:
        """Forward a header lookup to the wrapped response."""
        return self.response.getheader(name, default)

    def read(self, amt: int | None = None) -> bytes:
        """Read body bytes, bounding the wait by the remaining deadline."""
        if self.sock is not None:
            remaining = self.deadline - monotonic()
            if remaining > 0:
                # The connection's original timeout may outlast the deadline.
                self.sock.settimeout(min(self.read_timeout_seconds, remaining))
        return self.response.read(amt)


def _log_failure(
    logger: logging.Logger,
    reason: str,
    **fields: object,
) -> None:
    """Record one bounded download failure event on `logger`.

    The asset name is deliberately not logged. Callers of the generic
    downloader supply their own names, so the value is not a closed category
    and must not become a metric label; the reason and the numeric fields
    are bounded.
    """
    log_event(
        logger,
        "download.failed",
        reason=reason,
        **fields,
    )


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
    """Write a response body, enforcing the byte ceiling and deadline."""
    # The limits travel together so failure messages name the logical asset,
    # and the returned count avoids re-reading the file.
    name = limits.name
    expected = _content_length(response, name)
    _reject_declared_oversize(limits, expected)
    try:
        with destination.open("wb") as output:
            try:
                written = _copy_body(response, output, limits)
            except (OSError, http.client.HTTPException) as exc:
                # Only response reads reach here: `_copy_body` converts local
                # write failures first, so retrying this one may help.
                _log_failure(_LOGGER, "stream_error")
                msg = f"failed to download {name}: {exc}"
                raise TransientDownloadError(msg) from exc
    except OSError as exc:
        # Opening, flushing or closing the destination is a local storage
        # failure; a retry of the same request cannot repair it.
        _log_failure(_LOGGER, "store_error")
        msg = f"failed to store {name} at {destination}: {exc}"
        raise ProverToolError(msg) from exc
    _require_complete_body(limits, written, expected)
    return written


def _content_length(response: HTTPResponseLike, asset_name: str) -> int | None:
    """Return a valid non-negative `Content-Length`, when present."""
    raw = response.getheader("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        _log_failure(_LOGGER, "invalid_content_length")
        msg = f"invalid Content-Length '{raw}' from {asset_name}"
        raise ProverToolError(msg) from exc
    if value < 0:
        _log_failure(_LOGGER, "invalid_content_length", declared=value)
        msg = f"invalid negative Content-Length '{raw}' from {asset_name}"
        raise ProverToolError(msg)
    return value


def _reject_declared_oversize(
    limits: _StreamLimits,
    declared: int | None,
) -> None:
    """Reject a `Content-Length` above the ceiling before reading the body."""
    if declared is None or declared <= limits.max_bytes:
        return
    _log_failure(
        _LOGGER,
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
            _raise_deadline(limits, written)
        try:
            chunk = response.read(READ_CHUNK_SIZE)
        except (OSError, http.client.HTTPException) as exc:
            # The watchdog shuts the socket down at the deadline, so the
            # resulting `TimeoutError` or reset is the deadline expiring, not a
            # transport fault. Before the deadline it is the server's failure.
            if monotonic() < limits.deadline:
                raise
            _raise_deadline(limits, written, cause=exc)
        if not chunk:
            # A socket shut down at the deadline reads as EOF, so the deadline
            # is checked again before a short body is accepted as complete.
            _require_time_left(limits, written)
            return written
        written += len(chunk)
        if written > limits.max_bytes:
            _log_failure(
                _LOGGER,
                "size_limit",
                written=written,
                max_bytes=limits.max_bytes,
            )
            msg = (
                f"refusing to download {limits.name}: "
                f"streamed size exceeded the {limits.max_bytes}-byte limit"
            )
            raise ProverToolError(msg)
        try:
            output.write(chunk)
        except OSError as exc:
            # A local disk error must not be retried as a flaky connection.
            _log_failure(_LOGGER, "store_error", written=written)
            msg = f"failed to store {limits.name}: {exc}"
            raise ProverToolError(msg) from exc


def _require_time_left(limits: _StreamLimits, written: int) -> None:
    """Raise the deadline failure once the transfer has no time left."""
    if monotonic() >= limits.deadline:
        _raise_deadline(limits, written)


def _raise_deadline(
    limits: _StreamLimits,
    written: int,
    *,
    cause: BaseException | None = None,
) -> typ.NoReturn:
    """Report the expired transfer deadline and raise the bounded failure."""
    _log_failure(_LOGGER, "deadline", written=written)
    msg = f"download of {limits.name} exceeded the transfer deadline"
    raise ProverToolError(msg) from cause


def _require_complete_body(
    limits: _StreamLimits,
    written: int,
    expected: int | None,
) -> None:
    """Reject a body shorter than its declared `Content-Length`.

    `HTTPResponse.read` can report the end of a truncated stream without
    raising `IncompleteRead`, so this check is often the only place a cut
    connection surfaces. The failure is transient for the same reason a read
    error is: a retry may receive the whole body, and the digest and storage
    checks that follow still decide whether what arrived is acceptable.
    """
    if expected is None or written == expected:
        return
    _log_failure(
        _LOGGER,
        "incomplete_body",
        written=written,
        expected=expected,
    )
    msg = (
        f"incomplete download of {limits.name}: "
        f"received {written} bytes, expected {expected}"
    )
    raise TransientDownloadError(msg)
