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
import dataclasses as dc
import typing as typ

import cuprum
from cuprum import (
    BrokenPipePolicy,
    ExecutionContext,
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    RunOutputOptions,
    sh,
)
from cuprum.context import EnvMode

from rust_prover_tools.supervision.models import (
    LaunchResult,
    OutputLine,
    StopReason,
)
from rust_prover_tools.supervision.sinks import bounded_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from cuprum.context import UnsetType
    from cuprum.events import ExecEvent

    from rust_prover_tools.supervision.models import HarnessSpec

__all__ = (
    "CommandBuilder",
    "CuprumHarnessLauncher",
    "LaunchCommand",
    "PidSource",
    "RunningHarness",
)

#: Project name Cuprum records for this package's dynamically admitted runs.
_PROJECT_NAME = "rust-prover-tools"


@dc.dataclass(frozen=True, slots=True)
class LaunchCommand:
    """One composed invocation the launcher will run.

    Attributes
    ----------
    argv : tuple[str, ...]
        Program and arguments.
    cwd : Path | None
        Working directory for the child.
    env : cabc.Mapping[str, str | UnsetType] | None
        Environment for the child. Wrapper values and removal markers are
        retained verbatim.
    env_mode : cuprum.EnvMode
        How `env` relates to the child's inherited environment. A supervised
        legacy route uses `REPLACE` so a later host change cannot reach a
        derived command; a snapshot route keeps the overlay its removal
        markers require.
    """

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: cabc.Mapping[str, str | UnsetType] | None = None
    env_mode: EnvMode = EnvMode.OVERLAY


type CommandBuilder = cabc.Callable[[HarnessSpec], LaunchCommand]
"""Composes the argv and environment for one harness identity."""


class PidSource(typ.Protocol):
    """A source of the child's process-group identifier."""

    @property
    def pid(self) -> int | None:
        """The reported pid, or `None` when none is known."""
        ...


class _PidRecorder:
    """Capture the child PID from the run's ``start`` observe event."""

    def __init__(self) -> None:
        """Create a recorder that has seen no start event."""
        self.pid: int | None = None

    def __call__(self, event: ExecEvent) -> None:
        """Record the pid the run announced."""
        if event.phase == "start" and event.pid is not None:
            self.pid = event.pid


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
            A cancelled run reports the status Cuprum recorded before the stop
            when one exists, and otherwise reports only the stop reason the
            supervisor already holds.
        """
        if self._result is not None:
            return self._result
        try:
            outcome = await self._task
        except asyncio.CancelledError:
            # Cuprum settles the owned group before its cancellation surfaces,
            # so by here every descendant has been terminated and reaped. A
            # stop the supervisor requested is the termination evidence; no
            # exit status exists for a child that was signalled by the
            # supervisor rather than by itself.
            if self._reason is None:
                raise
            # The supervisor's own stop sent the signal, so no self-inflicted
            # number is recorded as if the child had terminated itself.
            outcome = LaunchResult(exit_code=None, signal_number=None)
        self._result = outcome
        return outcome


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
        recorder = _PidRecorder()
        task = asyncio.get_running_loop().create_task(
            self._run(spec, build, on_line, recorder)
        )
        return RunningHarness(task, recorder)

    async def _run(
        self,
        spec: HarnessSpec,
        build: CommandBuilder,
        on_line: cabc.Callable[[object], None],
        recorder: _PidRecorder,
    ) -> LaunchResult:
        """Run one invocation on its own task, scoping the pid observer."""
        started = self._monotonic()
        try:
            command = build(spec)
            program = Program(command.argv[0])
            project = ProjectSettings(
                name=_PROJECT_NAME,
                programs=(program,),
                documentation_locations=("docs/developers-guide.md",),
                noise_rules=(),
            )
            catalogue = ProgramCatalogue(projects=(project,))
            cmd = sh.make(program, catalogue=catalogue)(*command.argv[1:])
            context = ExecutionContext(
                env=command.env,
                env_mode=command.env_mode,
                cwd=command.cwd,
                process_group=ProcessGroupPolicy.OWN_GROUP,
            )
            output = RunOutputOptions(
                capture=False,
                echo=False,
                on_line=self._line_hook(on_line, started),
                broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT,
            )
            with cuprum.observe(recorder):
                result = await cmd.run(output=output, context=context)
        except (OSError, cuprum.UnknownProgramError) as error:
            return LaunchResult(launch_error=f"{type(error).__name__}: {error}")
        return _exit_status(result.exit_code)

    def _line_hook(
        self,
        on_line: cabc.Callable[[object], None],
        started: float,
    ) -> cabc.Callable[[cuprum.LineEvent], None]:
        """Return the Cuprum line hook that timestamps one line."""

        def _observe(event: cuprum.LineEvent) -> None:
            """Timestamp and forward one decoded child line."""
            text = _bounded(event.text, self._max_line_bytes)
            with contextlib.suppress(BrokenPipeError):
                on_line(
                    OutputLine(
                        stream=event.stream,
                        text=text,
                        timestamp_utc=self._clock(),
                        elapsed_seconds=max(0.0, self._monotonic() - started),
                    )
                )

        return _observe


def _bounded(text: str, limit: int) -> str:
    """Return `text` cut to at most `limit` bytes with an explicit marker."""
    return bounded_text(text, limit)


def _exit_status(code: int) -> LaunchResult:
    """Translate a Cuprum exit status into the typed launch result."""
    if code < 0:
        return LaunchResult(exit_code=code, signal_number=-code)
    return LaunchResult(exit_code=code)
