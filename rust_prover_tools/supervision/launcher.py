"""Launch one harness through Cuprum with an owned, observable process group.

The supervisor needs three things Cuprum's synchronous ``run_command`` cannot
give it: a live child it can stop, the child's process-group ID so it can
measure the group rather than the direct child, and output observed while it
arrives rather than captured at the end. This module supplies exactly that and
nothing else; timing, classification, and reporting stay outside.

Two Cuprum details are load-bearing:

- ``SafeCmd.start()`` forces capture on its handle, because a handle must be
  able to report what the child said. A proof log can be arbitrarily large, so
  this launcher uses ``SafeCmd.run()`` with ``capture=False`` and a bounded
  ``on_line`` consumer instead.
- The ``start`` observe event carries the pid only on a hook registered in the
  same context as the run, so the hook lives for the run's task rather than
  being installed globally. The pid is the group leader's, because
  ``OWN_GROUP`` spawns the child as a new session and process-group leader.

Cancelling the run task is how a stop is delivered. Cuprum's cancellation path
settles the owned group before it re-raises, so every descendant the verifier
started is terminated with the child that started it.

Example
-------
```python
running = launcher.start(spec, build, on_line)
outcome = await running.wait()
```
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import typing as typ

import cuprum
from cuprum import (
    BrokenPipePolicy,
    RunOutputOptions,
)
from cuprum.sh.stdio import StdioTarget

from rust_prover_tools.supervision.launch_command import (
    CommandBuilder,
    LaunchCommand,
)
from rust_prover_tools.supervision.launch_pid import PidRecorder
from rust_prover_tools.supervision.models import (
    LaunchResult,
    StopReason,
)
from rust_prover_tools.supervision.pipe_drain import (
    DrainConfig,
    PipeDrain,
    command_for,
    settle_drains,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from rust_prover_tools.supervision.models import HarnessSpec

__all__ = (
    "CommandBuilder",
    "CuprumHarnessLauncher",
    "LaunchCommand",
    "PidSource",
    "RunningHarness",
)


class PidSource(typ.Protocol):
    """A source of the child's process-group identifier and settled status.

    ``settled_exit_code`` is the status Cuprum's ``settled`` event carried when
    the run ended. It is often ``None`` for a run the supervisor stopped,
    because a signalled child has no self-reported status; it is the only
    termination evidence a cancelled run leaves behind.
    """

    @property
    def pid(self) -> int | None:
        """The reported pid, or `None` when none is known."""
        ...

    @property
    def settled_exit_code(self) -> int | None:
        """The status Cuprum settled with, or `None` when it reported none."""
        ...


class RunningHarness:
    """One harness invocation that is already running or starting.

    The handle records the first stop request only, because the first reason is
    the one that explains the termination. A later request, such as an
    interruption arriving while a timeout stop is already in flight, must not
    overwrite it.
    """

    def __init__(
        self,
        task: asyncio.Task[LaunchResult],
        recorder: PidSource,
    ) -> None:
        """Wrap the run task and its pid source."""
        self._task = task
        self._recorder = recorder
        self._reason: StopReason | None = None
        self._result: LaunchResult | None = None

    @property
    def done(self) -> bool:
        """Whether the child has finished, whether normally or after a stop."""
        return self._task.done()

    @property
    def group_id(self) -> int | None:
        """The child's process-group ID, when the run has reported one."""
        return self._recorder.pid

    @property
    def stop_reason(self) -> StopReason | None:
        """The stop the supervisor requested, if any."""
        return self._reason

    def stop(self, reason: StopReason) -> None:
        """Request a stop for `reason`, keeping the first request.

        Parameters
        ----------
        reason : StopReason
            Why the supervisor is stopping the child.
        """
        if self._reason is None:
            self._reason = reason
        if not self._task.done():
            self._task.cancel()

    async def wait(self) -> LaunchResult:
        """Wait for the child to finish and return its termination evidence.

        Returns
        -------
        LaunchResult
            The child's status, the signalled status, or the launch failure.
            A run the supervisor stopped reports the status Cuprum settled
            with when one exists, and otherwise reports only the stop reason
            the supervisor already holds.

        A waiter's own cancellation is not the child's: the launch task is
        shielded, so this coroutine can be cancelled while the child keeps
        running. In that case the stop is still joined before any evidence is
        reported, so no caller can mistake an unsettled group for a finished
        invocation.
        """
        if self._result is not None:
            return self._result
        try:
            outcome = await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if self._reason is None:
                raise
            # A stop was requested, but this waiter may have been cancelled
            # before Cuprum finished settling the owned group. Join the run
            # before reporting, so the next harness never starts beside a
            # child that is still being torn down.
            outcome = await self.settle()
        self._result = outcome
        return outcome

    async def settle(self) -> LaunchResult:
        """Wait until the launch task has finished, whatever ended it.

        This is the cleanup primitive. Unlike `wait`, it is not tied to one
        observer's lifetime: a cancellation delivered while this coroutine is
        joining does not abandon the child. Cuprum's teardown is bounded, so
        the wait is bounded too.

        Returns
        -------
        LaunchResult
            The settled status, or the stop-only evidence a cancelled run
            leaves behind.
        """
        while not self._task.done():
            try:
                await asyncio.wait({self._task})
            except asyncio.CancelledError:
                # The group is still being torn down. Keep joining: returning
                # now would let a later harness start beside this one.
                continue
        return self._settled_result()

    def _settled_result(self) -> LaunchResult:
        """Return the evidence a finished launch task left behind."""
        if self._result is not None:
            return self._result
        if self._task.cancelled():
            # The supervisor's own stop sent the signal, so no self-inflicted
            # number is recorded as if the child had terminated itself.
            return _exit_status(self._recorder.settled_exit_code)
        exception = self._task.exception()
        if exception is not None:
            raise exception
        return self._task.result()


class CuprumHarnessLauncher:
    """Run one harness invocation through Cuprum and report its termination.

    Parameters
    ----------
    clock : cabc.Callable[[], datetime.datetime]
        Wall-clock source for output timestamps.
    monotonic : cabc.Callable[[], float]
        Monotonic source for elapsed times.
    max_line_bytes : int
        Bound applied to one observed line before it is forwarded.
    """

    def __init__(
        self,
        *,
        clock: cabc.Callable[[], dt.datetime],
        monotonic: cabc.Callable[[], float],
        max_line_bytes: int = 8 * 1024,
    ) -> None:
        """Create a launcher using the supplied time sources."""
        self._clock = clock
        self._monotonic = monotonic
        self._max_line_bytes = max_line_bytes

    @property
    def max_line_bytes(self) -> int:
        """The bound this launcher applies to one forwarded line.

        Returns
        -------
        int
            The configured per-line byte bound.
        """
        return self._max_line_bytes

    def start(
        self,
        spec: HarnessSpec,
        build: CommandBuilder,
        on_line: cabc.Callable[[object], None],
    ) -> RunningHarness:
        """Start one invocation and return its handle.

        Parameters
        ----------
        spec : HarnessSpec
            Identity the invocation belongs to.
        build : CommandBuilder
            Composes the argv and environment for `spec`.
        on_line : cabc.Callable[[object], None]
            Receives one `OutputLine` per decoded child line, in arrival
            order. It is called from the drain loop, so it must not block.

        Returns
        -------
        RunningHarness
            The running child. The task is created on the running loop, so this
            must be called from a coroutine.
        """
        recorder = PidRecorder()
        task = asyncio.get_running_loop().create_task(
            self._run(spec, build, on_line, recorder)
        )
        return RunningHarness(task, recorder)

    async def _run(
        self,
        spec: HarnessSpec,
        build: CommandBuilder,
        on_line: cabc.Callable[[object], None],
        recorder: PidRecorder,
    ) -> LaunchResult:
        """Run one invocation on its own task, scoping the pid observer."""
        started = self._monotonic()
        drains: list[PipeDrain] = []
        try:
            cmd, context = command_for(build(spec))
            output, drains = self._streams(recorder, on_line, started)
            with cuprum.observe(recorder):
                result = await cmd.run(output=output, context=context)
            await settle_drains(drains)
        except (OSError, cuprum.UnknownProgramError) as error:
            return LaunchResult(launch_error=f"{type(error).__name__}: {error}")
        finally:
            recorder.close_parent_ends()
            for drain in drains:
                drain.close()
        return _exit_status(result.exit_code)

    def _streams(
        self,
        recorder: PidRecorder,
        on_line: cabc.Callable[[object], None],
        started: float,
    ) -> tuple[RunOutputOptions, list[PipeDrain]]:
        """Create the child's pipes and the drains that own their read ends."""
        readable: list[int] = []
        writable: list[int] = []
        try:
            out_read, out_write = os.pipe()
            readable.append(out_read)
            writable.append(out_write)
            err_read, err_write = os.pipe()
            readable.append(err_read)
            writable.append(err_write)
        except OSError:
            # Nothing is registered with the recorder yet, so no other
            # cleanup path owns these descriptors.
            for descriptor in (*readable, *writable):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise
        recorder.own(out_write, err_write)
        output = RunOutputOptions(
            capture=False,
            echo=False,
            stdout=StdioTarget.fd(out_write),
            stderr=StdioTarget.fd(err_write),
            broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT,
        )
        config = DrainConfig(
            clock=self._clock,
            monotonic=self._monotonic,
            max_line_bytes=self._max_line_bytes,
            on_line=on_line,
            started=started,
        )
        drains: list[PipeDrain] = []
        try:
            drains = [
                PipeDrain(out_read, "stdout", config),
                PipeDrain(err_read, "stderr", config),
            ]
            for drain in drains:
                drain.start()
        except _SETUP_FAILURES:
            # The recorder owns the write ends and the caller's finally closes
            # them; the read ends belong here until a drain takes ownership.
            for drain in drains:
                drain.close()
            for descriptor in (out_read, err_read):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise
        return output, drains


#: Failures that can occur between allocating the pipes and starting the
#: drains: an allocation or descriptor error, an invalid drain configuration,
#: or a loop registration failure.
_SETUP_FAILURES: typ.Final[tuple[type[BaseException], ...]] = (
    OSError,
    ValueError,
    RuntimeError,
)


def _exit_status(code: int | None) -> LaunchResult:
    """Translate a Cuprum exit status into the typed launch result."""
    if code is None:
        return LaunchResult(exit_code=None, signal_number=None)
    if code < 0:
        return LaunchResult(exit_code=code, signal_number=-code)
    return LaunchResult(exit_code=code)
