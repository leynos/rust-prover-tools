"""The running-harness handle: stop precedence, joining, and settlement.

A handle records only the first stop request, joins the child before
reporting, and turns a settled status into the typed launch result. It lives
apart from the launcher that creates it so each module stays small.
"""

from __future__ import annotations

import asyncio
import typing as typ

from rust_prover_tools.supervision.models import (
    LaunchResult,
    StopReason,
)

__all__ = ("PidSource", "RunningHarness", "exit_status")


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

    @property
    def failure(self) -> BaseException | None:
        """The first output-callback failure, if one was relayed."""
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
        if self._recorder.failure is not None:
            # A lost evidence write outranks whatever ended the child: the
            # attempt must not be reported from partial facts.
            raise self._recorder.failure
        if self._task.cancelled():
            # The supervisor's own stop sent the signal, so no self-inflicted
            # number is recorded as if the child had terminated itself.
            return exit_status(self._recorder.settled_exit_code)
        exception = self._task.exception()
        if exception is not None:
            raise exception
        return self._task.result()


def exit_status(code: int | None) -> LaunchResult:
    """Translate a Cuprum exit status into the typed launch result."""
    if code is None:
        return LaunchResult(exit_code=None, signal_number=None)
    if code < 0:
        return LaunchResult(exit_code=code, signal_number=-code)
    return LaunchResult(exit_code=code)
