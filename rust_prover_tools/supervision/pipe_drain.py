"""Drain a child's pipes into bounded lines on the event loop.

Cuprum's line hook accumulates an unterminated line in its own drain buffer
before invoking the hook. The supervisor therefore borrows the child's pipe
ends through ``StdioTarget.fd`` and splits them here, where the partial line is
bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import os
import typing as typ

from cuprum import (
    ExecutionContext,
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    sh,
)
from cuprum.context import UNSET, EnvMode

from rust_prover_tools.supervision.models import OutputLine
from rust_prover_tools.supervision.stream_lines import BoundedLineSplitter

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from cuprum import SafeCmd

    from rust_prover_tools.supervision.launcher import LaunchCommand

__all__ = (
    "DRAIN_GRACE_SECONDS",
    "DrainConfig",
    "PipeDrain",
    "command_for",
    "settle_drains",
)

#: Project name Cuprum records for this package's dynamically admitted runs.
PROJECT_NAME = "rust-prover-tools"

#: Seconds to wait for pipe EOF after the child has exited.
DRAIN_GRACE_SECONDS = 5.0

#: Bytes read from a child pipe per readiness callback.
_READ_SIZE = 64 * 1024


@dc.dataclass(frozen=True, slots=True)
class DrainConfig:
    """The time sources and bounds one pipe drain shares."""

    clock: cabc.Callable[[], dt.datetime]
    monotonic: cabc.Callable[[], float]
    max_line_bytes: int
    on_line: cabc.Callable[[object], None]
    started: float
    on_failure: cabc.Callable[[BaseException], None] | None = None


class PipeDrain:
    """Read one child stream in raw chunks and forward bounded lines.

    Cuprum's line hook would retain an unterminated line in its own buffer
    before invoking the hook. Reading the borrowed descriptor directly keeps
    that buffer here, where `BoundedLineSplitter` caps it.
    """

    def __init__(self, descriptor: int, stream: str, config: DrainConfig) -> None:
        """Create a drain for one borrowed read descriptor."""
        self._descriptor = descriptor
        self._stream = stream
        self._config = config
        self._splitter = BoundedLineSplitter(max_line_bytes=config.max_line_bytes)
        self._closed = False
        #: First callback failure this drain hit, if any.
        self.failure: BaseException | None = None
        self.finished = asyncio.Event()
        os.set_blocking(descriptor, False)

    def start(self) -> None:
        """Register the descriptor with the loop."""
        asyncio.get_running_loop().add_reader(self._descriptor, self._readable)

    def _readable(self) -> None:
        """Read one chunk, forwarding every line it completed."""
        try:
            chunk = os.read(self._descriptor, _READ_SIZE)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self.close()
            return
        for text in self._splitter.feed(chunk):
            if not self._forward(text):
                return

    def _emit(self, text: str) -> bool:
        """Timestamp and forward one line, relaying a callback failure.

        A closed job log is tolerated. Any other failure would otherwise reach
        the event loop's exception handler while the run carried on, so the
        first one is recorded, the drain is closed, and the owner is told.
        """
        config = self._config
        try:
            config.on_line(
                OutputLine(
                    stream=self._stream,
                    text=text,
                    timestamp_utc=config.clock(),
                    elapsed_seconds=max(0.0, config.monotonic() - config.started),
                )
            )
        except BrokenPipeError:
            return True
        except Exception as error:  # ruff: ignore[blind-except] - relayed
            self.failure = error if self.failure is None else self.failure
            self.close()
            if config.on_failure is not None:
                config.on_failure(self.failure)
            return False
        return True

    def _forward(self, text: str) -> bool:
        """Forward one line unless the callback already failed."""
        if self.failure is not None:
            return False
        return self._emit(text)

    def close(self) -> None:
        """Stop reading and forward any retained trailing line.

        The descriptor release and completion signal are in `finally` because
        a forwarding failure - a spool write raising `OSError`, for instance -
        must not leave the pipe open or make `settle_drains` wait its whole
        grace period. Forwarding still happens before either.
        """
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(ValueError, RuntimeError):
            asyncio.get_running_loop().remove_reader(self._descriptor)
        try:
            for text in self._splitter.finish():
                if not self._forward(text):
                    break
        finally:
            with contextlib.suppress(OSError):
                os.close(self._descriptor)
            self.finished.set()


async def settle_drains(drains: cabc.Sequence[PipeDrain]) -> None:
    """Wait briefly for every pipe to reach EOF, then close what remains."""
    waiters = [asyncio.create_task(drain.finished.wait()) for drain in drains]
    try:
        await asyncio.wait(waiters, timeout=DRAIN_GRACE_SECONDS)
    finally:
        for waiter in waiters:
            waiter.cancel()
        for drain in drains:
            drain.close()


def command_for(command: LaunchCommand) -> tuple[SafeCmd, ExecutionContext]:
    """Build the Cuprum command and context for one composed invocation."""
    program = Program(command.argv[0])
    project = ProjectSettings(
        name=PROJECT_NAME,
        programs=(program,),
        documentation_locations=("docs/developers-guide.md",),
        noise_rules=(),
    )
    catalogue = ProgramCatalogue(projects=(project,))
    cmd = sh.make(program, catalogue=catalogue)(*command.argv[1:])
    env = None
    env_mode = EnvMode.INHERIT
    if command.env is not None:
        env = {
            name: UNSET if value is None else value
            for name, value in command.env.values.items()
        }
        env_mode = EnvMode.REPLACE if command.env.replace else EnvMode.OVERLAY
    context = ExecutionContext(
        env=env,
        env_mode=env_mode,
        cwd=command.cwd,
        process_group=ProcessGroupPolicy.OWN_GROUP,
    )
    return cmd, context
