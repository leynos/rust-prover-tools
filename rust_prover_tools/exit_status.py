"""Preserve the process exit status when standard output is a closed pipe.

`sys.exit` sets the status the interpreter should report, but CPython does not
honour it unconditionally: at shutdown it flushes `sys.stdout`, and if that
flush fails it exits with status 120 instead. A reader that has gone away, such
as `| head -n 5`, closes the pipe and makes exactly that flush fail.

The prover's status is the command's result, so it must survive a reader that
stops early. `guard_shutdown_flush` installs the `atexit` hook that CPython's
own `signal` module documentation recommends: flush while the failure can still
be caught, then point the dead descriptor at the null device so the
interpreter's final flush succeeds and the requested status stands.

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
import os
import sys

__all__ = ("guard_shutdown_flush",)


def _redirect_closed_stdout() -> None:
    """Point a dead standard output at the null device.

    Notes
    -----
    Any buffered output left after the failed flush is written to the null
    device by the interpreter's own final flush. The data is lost either way,
    because the reader that would have received it is gone; what changes is that
    losing it no longer replaces the exit status.
    """
    stream = sys.stdout
    if stream is None:
        # A windowed interpreter has no standard output to flush.
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
        # subclasses `OSError`. Such a stream is not flushed at shutdown either,
        # so no later flush can fail.
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, descriptor)
    finally:
        os.close(devnull)


def guard_shutdown_flush() -> None:
    """Keep a closed standard output from overriding the exit status.

    Notes
    -----
    Call this from the process entry point, before doing work that writes to
    standard output. Registering more than once is harmless: each hook only
    inspects the current standard output.
    """
    atexit.register(_redirect_closed_stdout)
