"""Preserve the process exit status when an output stream is a closed pipe.

`sys.exit` sets the status the interpreter should report, but CPython does not
honour it unconditionally: at shutdown it flushes `sys.stdout` and `sys.stderr`,
and if either flush fails it exits with status 120 instead. A reader that has
gone away, such as `| head -n 5`, closes the pipe and makes exactly that flush
fail. A combined redirect such as `2>&1 | head` points both streams at the one
dead descriptor, so both streams need guarding.

The prover's status is the command's result, so it must survive a reader that
stops early. `guard_shutdown_flush` installs the `atexit` hook that CPython's
own `signal` module documentation recommends: flush while the failure can still
be caught, then point each dead descriptor at the null device so the
interpreter's final flush succeeds and the requested status stands. A dead
stream with no descriptor cannot be pointed anywhere, so it is replaced by a
sink that discards output.

Example
-------
```python
from rust_prover_tools.exit_status import guard_shutdown_flush

guard_shutdown_flush()
raise SystemExit(run_the_work())
```
"""

from __future__ import annotations

import atexit
import contextlib
import io
import os
import sys
import typing as typ

__all__ = ("guard_shutdown_flush", "print_lines")


class _NullTextSink(io.TextIOBase):
    """A text stream that discards everything written to it."""

    @typ.override
    def writable(self) -> bool:
        """Report that this stream accepts writes."""
        return True

    @typ.override
    def write(self, text: str) -> int:
        """Discard the text and report it as written."""
        return len(text)


def _guard_output(name: str) -> None:
    """Point a dead output stream at the null device, or replace it."""
    # Buffered output left after the failed flush reaches the null device at the
    # interpreter's final flush. The reader is gone, so the data is lost either
    # way; the exit status no longer is.
    stream = getattr(sys, name)
    if stream is None:
        # A windowed interpreter has no such stream to flush.
        return
    try:
        stream.flush()
    except BrokenPipeError:
        pass
    else:
        return
    try:
        descriptor = stream.fileno()
    except OSError:
        # `io.UnsupportedOperation`, raised by a stream with no descriptor,
        # subclasses `OSError`. The interpreter still flushes such a stream at
        # shutdown, so leaving it installed would fail again and report 120.
        # Replace it with a sink whose flush cannot fail.
        setattr(sys, name, _NullTextSink())
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, descriptor)
    finally:
        os.close(devnull)


def _redirect_closed_outputs() -> None:
    """Point every dead output stream at the null device."""
    # Check both streams: `2>&1 | head` merges them onto one dead descriptor,
    # and an unguarded standard error would replace the preserved status.
    _guard_output("stdout")
    _guard_output("stderr")


def guard_shutdown_flush() -> None:
    """Keep closed output streams from overriding the exit status.

    Notes
    -----
    Call this from the process entry point, before doing work that writes to
    standard output. Registering more than once is harmless: each hook only
    inspects the streams as they are when the interpreter shuts down.
    """
    atexit.register(_redirect_closed_outputs)


def print_lines(
    lines: tuple[str, ...],
    *,
    stream: typ.TextIO | None = None,
) -> None:
    """Print lines to a text stream, ignoring a reader that has gone away.

    Parameters
    ----------
    lines : tuple[str, ...]
        Lines to print, each followed by a newline.
    stream : TextIO | None
        Destination stream; defaults to the current standard output.

    Notes
    -----
    The shutdown guard covers only the interpreter's final flush. A write made
    while the command is still running, which an unbuffered standard output
    performs immediately, needs its own protection; otherwise a closed reader
    replaces a completed command's status with a broken-pipe failure.
    """
    destination = sys.stdout if stream is None else stream
    # The loop stops at the first failed write: there is no reader left, and
    # the remaining lines would meet the same dead descriptor.
    with contextlib.suppress(BrokenPipeError):
        for line in lines:
            print(line, file=destination)
