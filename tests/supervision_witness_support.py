"""Shared timing helpers for the real-process supervision witness.

`RunningHarness.settle` suppresses cancellation so it can keep joining, which
means wrapping it in `asyncio.wait_for` cannot bound it. These helpers race the
join against an independent timer and stop the child if the timer wins, so a
regression fails promptly instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
import typing as typ

from rust_prover_tools.supervision import StopReason

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("SETTLE_SECONDS", "run_bounded", "settle_within")

#: Seconds a witness waits for one settle before failing.
SETTLE_SECONDS = 30.0


async def settle_within(running: object, *, deadline: float = SETTLE_SECONDS) -> None:
    """Stop and reap the child under independent deadlines.

    `RunningHarness.settle` suppresses cancellation so it can keep joining,
    which means neither `asyncio.wait_for` nor cancelling the task can bound
    it. Each phase below waits on its own timer instead of awaiting the settle
    task directly: first for the ordinary join, then after a stop, then after
    a signal to the process group. If none finishes, the task is cancelled and
    the test fails rather than waiting indefinitely.
    """
    handle = typ.cast("typ.Any", running)
    settle = asyncio.create_task(handle.settle())
    if await _finished(settle, deadline):
        await settle
        return
    handle.stop(StopReason.INTERRUPTED)
    if await _finished(settle, deadline):
        await settle
        return
    group = handle.group_id
    if group is not None:
        with contextlib.suppress(OSError):
            os.killpg(group, signal.SIGKILL)
        if await _finished(settle, deadline):
            await settle
            return
    settle.cancel()
    msg = "the child did not stop within the settlement deadline"
    raise AssertionError(msg)


async def _finished(task: asyncio.Task[object], deadline: float) -> bool:
    """Wait at most `deadline` seconds for `task`, without awaiting it after."""
    timer = asyncio.create_task(asyncio.sleep(deadline))
    try:
        await asyncio.wait(
            {typ.cast("typ.Any", task), timer},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        timer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timer
    return task.done()


def run_bounded(
    scenario: cabc.Coroutine[typ.Any, typ.Any, None],
    *,
    deadline: float = SETTLE_SECONDS,
) -> None:
    """Run an async scenario, bounded including event-loop shutdown.

    `asyncio.run` cancels and gathers pending tasks when the scenario returns
    or raises, and a task that suppresses cancellation can keep it waiting.
    A watchdog thread therefore exits the process with status 2 if the whole
    scenario, shutdown included, has not finished within `deadline`. Use this
    in a subprocess when the scenario is expected to fail; never in the test
    process itself.
    """
    expired = threading.Event()

    def expire() -> None:
        """Exit the process once the deadline has passed."""
        expired.set()
        os._exit(2)

    watchdog = threading.Timer(deadline, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        asyncio.run(scenario)
    finally:
        watchdog.cancel()
        watchdog.join()
