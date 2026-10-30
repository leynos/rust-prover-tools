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

Closed-pipe handling is observable without changing CLI output: every handled
case emits one INFO event with the stream, phase, action, and error category
when the host application enables logging. No logger or handler is installed,
and the event carries no output, path, or exception text.

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
import logging
import os
import sys
import typing as typ

__all__ = ("guard_shutdown_flush", "print_lines")

_LOGGER = logging.getLogger(__name__)


def _observe_closed_pipe(*, stream: str, phase: str, action: str) -> None:
    """Emit one opt-in structured event for a handled closed pipe.

    INFO keeps normal CLI output unchanged unless the host application enables
    logging. Only bounded identifiers are attached: never output, paths, or
    exception text.
    """
    # Observability must not defeat the guard it describes: a logging failure
    # inside the shutdown hook would abort the redirect and restore status 120.
    with contextlib.suppress(Exception):
        _LOGGER.info(
            "Closed-pipe output handling",
            extra={
                "stream": stream,
                "phase": phase,
                "action": action,
                "error_category": "broken_pipe" if action != "failed" else "os_error",
            },
        )


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
        _observe_closed_pipe(stream=name, phase="flush", action="sink")
        return
    devnull: int | None = None
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, descriptor)
    except OSError:
        # The final flush will fail again and report 120 either way; raising
        # here would only add an atexit traceback without preserving the status.
        _observe_closed_pipe(stream=name, phase="flush", action="failed")
    else:
        _observe_closed_pipe(stream=name, phase="flush", action="redirected")
    finally:
        if devnull is not None:
            with contextlib.suppress(OSError):
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


def _stream_name(destination: typ.TextIO) -> str:
    """Name a destination with a bounded identifier rather than its contents."""
    if destination is sys.stdout:
        return "stdout"
    if destination is sys.stderr:
        return "stderr"
    return "custom"


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
    try:
        for line in lines:
            print(line, file=destination)
    except BrokenPipeError:
        _observe_closed_pipe(
            stream=_stream_name(destination),
            phase="write",
            action="suppressed",
        )
