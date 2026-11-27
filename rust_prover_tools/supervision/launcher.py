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
import signal
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
    LaunchEnvironment,
)
from rust_prover_tools.supervision.launch_pid import PidRecorder
from rust_prover_tools.supervision.launch_running import (
    PidSource,
    RunningHarness,
    exit_status,
)
from rust_prover_tools.supervision.models import LaunchResult
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
    "LaunchEnvironment",
    "PidSource",
    "RunningHarness",
)

#: Seconds the group is given to leave after its leader exits.
_GROUP_GRACE_SECONDS: typ.Final[float] = 5.0

#: Seconds between group-liveness probes during settlement.
_GROUP_PROBE_SECONDS: typ.Final[float] = 0.05

#: Failures that mean the group has already gone or is not ours to signal.
#: Held as a tuple so the clause parses on every supported Python.
_GROUP_GONE_FAILURES: typ.Final[tuple[type[BaseException], ...]] = (
    ProcessLookupError,
    PermissionError,
)


def _group_members(group_id: int) -> bool:
    """Whether the owned group still holds a member this process can signal."""
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # This process cannot signal the group, so waiting cannot settle it.
        return False
    return True


def _signal_group(group_id: int, signum: int) -> None:
    """Deliver `signum` to the owned group, tolerating one that has gone."""
    if group_id in {os.getpid(), os.getpgrp()}:
        return
    try:
        os.killpg(group_id, signum)
    except _GROUP_GONE_FAILURES:
        return


async def _settle_group(group_id: int | None) -> None:
    """Terminate what an exited leader left behind in its owned process group.

    Cuprum waits only the direct child on a normal exit, so a descendant the
    leader started survives unless the group is stopped here. The group is
    asked to leave first and compelled after the grace period; the compulsion
    is repeated in `finally`, so a cancellation of this coroutine cannot
    abandon the group.
    """
    if group_id is None or not _group_members(group_id):
        return
    _signal_group(group_id, signal.SIGTERM)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _GROUP_GRACE_SECONDS
        while _group_members(group_id) and loop.time() < deadline:
            await asyncio.sleep(_GROUP_PROBE_SECONDS)
    finally:
        if _group_members(group_id):
            _signal_group(group_id, signal.SIGKILL)


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
        run: asyncio.Task[cuprum.CommandResult] | None = None

        def relay(error: BaseException) -> None:
            """Record the first callback failure and stop the child.

            The failure is kept on the recorder, which survives an outer
            cancellation, so neither a timeout nor a cancellation can hide a
            lost evidence write.
            """
            if recorder.failure is not None:
                return
            recorder.failure = error
            if run is not None and not run.done():
                run.cancel()

        try:
            cmd, context = command_for(build(spec))
            output, drains = self._streams(recorder, on_line, started, relay)
            with cuprum.observe(recorder):
                run = asyncio.ensure_future(cmd.run(output=output, context=context))
                result = await self._await_run(run, recorder)
            # The leader has exited, but a descendant it started can still be
            # in the owned group. Settle that group before reporting the
            # invocation finished; the leader's own status is already held in
            # `result` and is returned unchanged.
            await _settle_group(recorder.pid)
            await settle_drains(drains)
        except (OSError, cuprum.UnknownProgramError) as error:
            # A relayed callback failure is the primary failure even though it
            # arrives as the same OSError a launch problem would.
            if recorder.failure is not None:
                raise recorder.failure from error
            return LaunchResult(launch_error=f"{type(error).__name__}: {error}")
        finally:
            recorder.close_parent_ends()
            for drain in drains:
                drain.close()
        if recorder.failure is not None:
            raise recorder.failure
        return exit_status(result.exit_code)

    @staticmethod
    async def _await_run(
        run: asyncio.Task[cuprum.CommandResult],
        recorder: PidRecorder,
    ) -> cuprum.CommandResult:
        """Await the child, surfacing a relayed callback failure.

        Cancelling the run task for a relayed failure is an internal stop; an
        outer cancellation with no relayed failure is the supervisor's and is
        re-raised for the handle to settle.
        """
        try:
            return await run
        except asyncio.CancelledError:
            if recorder.failure is not None:
                raise recorder.failure from None
            raise

    def _streams(
        self,
        recorder: PidRecorder,
        on_line: cabc.Callable[[object], None],
        started: float,
        on_failure: cabc.Callable[[BaseException], None],
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
            on_failure=on_failure,
        )
        drains: list[PipeDrain] = []
        unowned = [out_read, err_read]
        try:
            for descriptor, stream in ((out_read, "stdout"), (err_read, "stderr")):
                drains.append(PipeDrain(descriptor, stream, config))
                unowned.remove(descriptor)
            for drain in drains:
                drain.start()
        except _SETUP_FAILURES:
            # A drain owns its read end from construction. Closing the drain and
            # then closing the same number again could release an unrelated
            # descriptor the process reused in between, so only read ends no
            # drain took ownership of are closed here. The recorder owns the
            # write ends and the caller's finally closes them.
            for drain in drains:
                drain.close()
            for descriptor in unowned:
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
