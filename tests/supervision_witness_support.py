"""Shared timing helpers for the real-process supervision witness.

`RunningHarness.settle` suppresses cancellation so it can keep joining, which
means wrapping it in `asyncio.wait_for` cannot bound it. These helpers race the
join against an independent timer and stop the child if the timer wins, so a
regression fails promptly instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

from rust_prover_tools.supervision import StopReason

__all__ = ("SETTLE_SECONDS", "settle_within")

#: Seconds a witness waits for one settle before failing.
SETTLE_SECONDS = 30.0


async def settle_within(running: object) -> None:
    """Stop and join the child, failing if the join exceeds its deadline."""
    handle = typ.cast("typ.Any", running)
    settle = asyncio.create_task(handle.settle())
    limit = asyncio.create_task(asyncio.sleep(SETTLE_SECONDS))
    done, _ = await asyncio.wait({settle, limit}, return_when=asyncio.FIRST_COMPLETED)
    limit.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await limit
    if settle not in done:
        # Stop the child, then allow one more bounded window for the join.
        # The settle task is cancelled and never awaited without a deadline:
        # it suppresses cancellation, so an unbounded await could hang.
        handle.stop(StopReason.INTERRUPTED)
        second = asyncio.create_task(asyncio.sleep(SETTLE_SECONDS))
        try:
            await asyncio.wait({settle, second}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            second.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await second
        if not settle.done():
            settle.cancel()
            msg = "the child did not stop within the settlement deadline"
            raise AssertionError(msg)
    await settle
