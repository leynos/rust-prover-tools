"""Turn a delivered SIGINT or SIGTERM into an awaitable stop for one run.

Signal handling lives here and nowhere else. The library installs no handlers,
so an embedding application keeps control; `install_interrupt_handlers`
installs SIGINT and SIGTERM for the duration of the supervised execution
phase, and `SignalInterrupt` records the first signal so the run can finalize
its report and return ``128 + signal``. Handler installation takes no
suspension point, and `SignalInterrupt.wait` installs nothing, so neither can
block the other. Discovery and route qualification run before those handlers
exist, so a signal there ends the process under its normal disposition instead
of being recorded for a phase that has not started.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("SignalInterrupt", "install_interrupt_handlers")


class SignalInterrupt:
    """A SIGINT/SIGTERM source the supervision layer can await.

    The library never installs a handler. This adapter owns the installation,
    restoration, and translation of a signal into an awaitable stop, so an
    embedding application's own signal policy is untouched.
    """

    def __init__(self) -> None:
        """Create a source that has received no signal."""
        self._signum: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    @property
    def signum(self) -> int | None:
        """The interrupting signal, or `None` while the run may continue."""
        return self._signum

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the source to the run's event loop."""
        self._loop = loop
        self._event = asyncio.Event()
        if self._signum is not None:
            self._event.set()

    @contextlib.contextmanager
    def installed(self) -> cabc.Generator[None]:
        """Install the handlers until the block exits.

        Installation itself belongs to `install_interrupt_handlers`; this
        method only names the conventional block form for a caller that
        already holds the source.

        Yields
        ------
        None
            The block during which the handlers are installed.
        """
        with install_interrupt_handlers(self):
            yield

    def _handle(self, signum: int, frame: object) -> None:
        """Record the first signal and release the monitor."""
        del frame
        if self._signum is None:
            self._signum = signum
        self._notify()

    def _notify(self) -> None:
        """Release the monitor when its loop is still usable."""
        loop = self._loop
        event = self._event
        if loop is None or event is None:
            return
        if not loop.is_closed():
            loop.call_soon_threadsafe(event.set)

    async def wait(self) -> None:
        """Wait until a signal asks the run to stop."""
        if self._signum is not None:
            return
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()


@contextlib.contextmanager
def install_interrupt_handlers(
    interrupt: SignalInterrupt,
) -> cabc.Generator[None]:
    """Install SIGINT and SIGTERM handlers for the duration of the block.

    Installation is synchronous and has no suspension point, so a caller
    cannot be interrupted while the previous dispositions are being recorded
    and replaced. Restoring them is the block's only exit action.

    Parameters
    ----------
    interrupt : SignalInterrupt
        Source that receives each delivered signal.

    Yields
    ------
    None
        The block during which the handlers are installed.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt._handle)
    try:
        yield
    finally:
        for signum, disposition in previous.items():
            signal.signal(signum, typ.cast("typ.Any", disposition))
