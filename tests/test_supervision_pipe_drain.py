"""Hold the pipe drain to releasing its descriptor on every close path."""

from __future__ import annotations

import asyncio
import os
import typing as typ

import pytest

from rust_prover_tools.supervision.pipe_drain import (
    DrainConfig,
    PipeDrain,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt


class _Clock:
    """A fixed wall clock for the drain's timestamps."""

    def now(self) -> dt.datetime:
        """Return one fixed instant."""
        import datetime as dt

        return dt.datetime(2026, 11, 24, tzinfo=dt.UTC)

    def monotonic(self) -> float:
        """Return one fixed monotonic reading."""
        return 0.0


def _config(on_line: cabc.Callable[[object], None]) -> DrainConfig:
    """Return a drain configuration with a fixed clock."""
    clock = _Clock()
    return DrainConfig(
        clock=clock.now,
        monotonic=clock.monotonic,
        max_line_bytes=1024,
        on_line=on_line,
        started=0.0,
    )


def _drain_with_trailing_text(on_line: cabc.Callable[[object], None]) -> int:
    """Create a pipe holding one unterminated line and return its read end."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"trailing text")
    os.close(write_fd)
    return read_fd


def test_a_failing_callback_still_releases_the_descriptor() -> None:
    """A trailing-line failure must not leak the pipe or block settlement.

    The failure arrives from the loop's read callback, where asyncio logs it
    rather than raising it into the test. The drain's own contract is what
    matters: the descriptor is closed and settlement is signalled.
    """

    def refuse(item: object) -> None:
        """Fail the way a full spool write does."""
        del item
        msg = "the spool is full"
        raise OSError(msg)

    descriptor = _drain_with_trailing_text(refuse)

    async def scenario() -> PipeDrain:
        """Let EOF deliver the unterminated text, then inspect the drain."""
        drain = PipeDrain(descriptor, "stdout", _config(refuse))
        drain.start()
        await asyncio.wait_for(drain.finished.wait(), timeout=1)
        return drain

    drain = asyncio.run(scenario())

    assert drain.finished.is_set()
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)
