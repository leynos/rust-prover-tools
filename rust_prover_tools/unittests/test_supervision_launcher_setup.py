"""Release every descriptor the launcher allocates before a spawn.

A pipe allocation or a loop registration can fail between the first descriptor
and the child. Each descriptor must then be closed exactly once, and the
original failure must reach the caller as the launch failure rather than being
swallowed or duplicated.
"""

from __future__ import annotations

import asyncio
import os
import typing as typ

import pytest

from rust_prover_tools.supervision.clock import SystemClock
from rust_prover_tools.supervision.launch_command import LaunchCommand
from rust_prover_tools.supervision.launcher import CuprumHarnessLauncher
from rust_prover_tools.supervision.pipe_drain import PipeDrain
from rust_prover_tools.unittests.supervision_doubles import spec

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _launcher() -> CuprumHarnessLauncher:
    """Return a launcher using the system clock."""
    clock = SystemClock()
    return CuprumHarnessLauncher(clock=clock.now, monotonic=clock.monotonic)


def _build(identity: object) -> LaunchCommand:
    """Compose a command that never needs to run."""
    del identity
    return LaunchCommand(("driver", "kani"))


def _assert_closed_once(
    descriptors: cabc.Sequence[int],
    closes: cabc.Sequence[int],
) -> None:
    """Each allocated descriptor is closed exactly once and is now invalid."""
    for descriptor in descriptors:
        count = closes.count(descriptor)
        assert count == 1, f"descriptor {descriptor} was closed {count} times"
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)


def test_a_failing_second_pipe_closes_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial allocation closes what it owns and reports the failure."""
    real_pipe = os.pipe
    real_close = os.close
    allocated: list[int] = []
    closes: list[int] = []

    def failing_pipe() -> tuple[int, int]:
        """Allocate the first pipe, then fail as a descriptor limit would."""
        if allocated:
            msg = "no more pipes"
            raise OSError(msg)
        read, write = real_pipe()
        allocated.extend((read, write))
        return read, write

    def counting_close(descriptor: int) -> None:
        """Record each close of an allocated end before delegating."""
        if descriptor in allocated:
            closes.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "pipe", failing_pipe)
    monkeypatch.setattr(os, "close", counting_close)

    async def scenario() -> None:
        """Start the launcher and observe its launch failure."""
        running = _launcher().start(spec(), _build, lambda line: None)
        result = await running.wait()
        assert result.launch_error == "OSError: no more pipes"

    asyncio.run(scenario())

    _assert_closed_once(allocated, closes)


def test_a_failing_drain_start_closes_every_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop registration failure closes both pipes exactly once."""
    real_pipe = os.pipe
    real_close = os.close
    allocated: list[int] = []
    closes: list[int] = []

    def tracking_pipe() -> tuple[int, int]:
        """Allocate a real pipe and remember both ends."""
        read, write = real_pipe()
        allocated.extend((read, write))
        return read, write

    def counting_close(descriptor: int) -> None:
        """Record each close of an allocated end before delegating."""
        if descriptor in allocated:
            closes.append(descriptor)
        real_close(descriptor)

    def refuse_start(self: PipeDrain) -> None:
        """Fail as a closed event loop would."""
        del self
        msg = "the loop is closed"
        raise RuntimeError(msg)

    monkeypatch.setattr(os, "pipe", tracking_pipe)
    monkeypatch.setattr(os, "close", counting_close)
    monkeypatch.setattr(PipeDrain, "start", refuse_start)

    async def scenario() -> None:
        """Start the launcher and observe the setup failure."""
        running = _launcher().start(spec(), _build, lambda line: None)
        with pytest.raises(RuntimeError, match="the loop is closed"):
            await running.wait()

    asyncio.run(scenario())

    _assert_closed_once(allocated, closes)
