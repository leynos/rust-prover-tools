"""The one transfer deadline and the watchdog that enforces it.

A deadline covers every redirect hop, the request that opens it, the header
parse and every body read. A deadline checked only between reads cannot stop a
server that trickles header bytes or never finishes answering a send, because
each blocking call still finishes inside the socket timeout. The watchdog
therefore shuts the hop's socket down once the transfer's own clock says no
time remains, which makes a blocked send, a blocked header parse and a blocked
body read fail alike.

A socket timeout bounds each blocking call, not a sequence of them, so the
budget is recomputed between phases rather than fixed when a hop opens. A call
already in flight cannot be re-armed, though, and the standard library applies
a hop's timeout to every resolved address in turn before TLS negotiation reuses
it, so a multi-address host can overrun the deadline by one hop timeout per
address. DNS resolution is outside the socket timeout altogether, and local
file I/O is not supervised at all.

`rust_prover_tools.download_transport` owns how a hop is requested;
`rust_prover_tools.download_stream` owns the body rules. This module owns only
the deadline both of them share.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import logging
import math
import socket
import threading
import typing as typ
from time import monotonic

from rust_prover_tools.download_stream import _DeadlineSocket, _log_failure
from rust_prover_tools.errors import ProverToolError, TransientDownloadError

if typ.TYPE_CHECKING:
    import http.client

_LOGGER = logging.getLogger(__name__)


class _DeadlineWatchdog:
    """Shut one hop's socket down when the transfer deadline passes.

    A deadline checked only between reads cannot stop a server that trickles
    header bytes or never finishes answering a send, because each blocking call
    still finishes inside the socket timeout. Shutting the socket down releases
    whichever call is blocked, so the hop fails at the deadline. The socket is
    captured when the watchdog is armed, because `getresponse()` clears
    `connection.sock` for a response the connection cannot reuse.
    """

    def __init__(self, sock: _DeadlineSocket, deadline: float) -> None:
        """Arm a watchdog for `sock`, without starting it."""
        self._sock = sock
        self._deadline = deadline
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set once the watchdog has shut the socket down.
        self.expired = False

    def start(self) -> None:
        """Start watching, unless the deadline can never arrive."""
        if not math.isfinite(self._deadline):
            return
        self._thread = threading.Thread(
            target=self._watch,
            name="download-deadline-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop watching and wait for the watchdog thread to finish."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _watch(self) -> None:
        """Wait until the transfer has no time left, then shut the socket down.

        The clock is re-read after each wait, so an early wakeup only starts
        another wait and `expired` is set only once the transport's own clock
        says no time remains.
        """
        while not self._stop.is_set():
            remaining = self._deadline - monotonic()
            if remaining <= 0:
                self.expired = True
                self._shut_down()
                return
            if self._stop.wait(remaining):
                return

    def _shut_down(self) -> None:
        """Shut the captured socket down, treating a closed one as done."""
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)


@dc.dataclass(slots=True)
class _HopHandle:
    """One hop's watchdog and connection, closed together.

    The handle outlives `_open` because the caller still streams the body, so
    the watchdog must stay armed for `response.read()` as well as the send.
    """

    watchdog: _DeadlineWatchdog
    connection: http.client.HTTPSConnection

    def close(self) -> None:
        """Stop this hop's watchdog before closing its connection."""
        _close_hop(self.watchdog, self.connection)


def _close_hop(
    watchdog: _DeadlineWatchdog | None,
    connection: http.client.HTTPSConnection,
) -> None:
    """Stop a hop's watchdog, then close its connection."""
    try:
        if watchdog is not None:
            watchdog.stop()
    finally:
        connection.close()


def _raise_deadline(
    url: str,
    *,
    cause: BaseException | None = None,
) -> typ.NoReturn:
    """Report the expired transfer deadline as the base failure."""
    _log_failure(_LOGGER, "deadline")
    msg = f"download of {url} exceeded the transfer deadline"
    raise ProverToolError(msg) from cause


def _hop_timeout(url: str, connect_timeout_seconds: float, deadline: float) -> float:
    """Return the seconds a hop may spend, refusing one with no time left.

    One clock reading both refuses a spent hop and sizes its budget, so a
    deadline passing between two readings cannot yield a zero timeout, which
    would arm a non-blocking socket instead of failing the hop.

    Raises
    ------
    ProverToolError
        If the hop has no time left.
    """
    remaining = deadline - monotonic()
    if remaining <= 0:
        _raise_deadline(url)
    return min(connect_timeout_seconds, remaining)


def _deadline_reached(watchdog: _DeadlineWatchdog | None, deadline: float) -> bool:
    """Report whether a failed hop ran out of time."""
    return (watchdog is not None and watchdog.expired) or monotonic() >= deadline


def _arm_socket(
    connection: http.client.HTTPSConnection,
    url: str,
    connect_timeout_seconds: float,
    deadline: float,
) -> _DeadlineSocket:
    """Arm an opened connection's socket with the time the hop has left.

    Opening the connection has already spent part of the hop's budget, and a
    socket timeout bounds one blocking call rather than a sequence of them, so
    the request must be sent under what remains now rather than the timeout the
    socket was opened with.

    Returns
    -------
    _DeadlineSocket
        The captured socket, armed for the request and the body.

    Raises
    ------
    ProverToolError
        If the hop has no time left.
    TransientDownloadError
        If the connection exposes no socket to arm.
    """
    timeout = _hop_timeout(url, connect_timeout_seconds, deadline)
    sock = typ.cast("_DeadlineSocket | None", getattr(connection, "sock", None))
    if sock is None:
        _log_failure(_LOGGER, "transport_error")
        msg = f"failed to download {url}: the connection opened no socket"
        raise TransientDownloadError(msg)
    sock.settimeout(timeout)
    return sock
