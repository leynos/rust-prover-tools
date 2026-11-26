"""Hold the witness settlement helper to its independent deadlines."""

from __future__ import annotations

import asyncio

import pytest

from tests.supervision_witness_support import settle_within


class _StuckHandle:
    """A handle whose settlement suppresses cancellation until released."""

    group_id: int | None = None

    def __init__(self) -> None:
        """Create a handle that has not been released."""
        self.released = asyncio.Event()
        self.stopped = False

    def stop(self, reason: object) -> None:
        """Record the stop without settling."""
        del reason
        self.stopped = True

    async def settle(self) -> None:
        """Wait for the test to release the settlement, ignoring cancellation."""
        try:
            await asyncio.wait_for(self.released.wait(), timeout=60)
        except asyncio.CancelledError:
            await self.released.wait()


def test_a_stuck_settlement_fails_within_its_deadline() -> None:
    """A settlement that never finishes fails promptly instead of hanging."""

    async def scenario() -> None:
        """Require the helper to fail, then let the stuck task finish."""
        handle = _StuckHandle()
        with pytest.raises(AssertionError, match="did not stop"):
            await settle_within(handle, deadline=0.02)
        assert handle.stopped
        handle.released.set()
        # Let the cancelled settle task observe the release before the loop
        # shuts down, so the failure path cannot leave it pending.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_a_settlement_that_finishes_is_not_reported_as_stuck() -> None:
    """An ordinary settlement completes without a failure."""

    class _QuickHandle:
        """A handle that settles immediately."""

        group_id: int | None = None

        def stop(self, reason: object) -> None:
            """Nothing to stop."""

        async def settle(self) -> None:
            """Return immediately."""

    async def scenario() -> None:
        """Await the helper on a quick handle."""
        await settle_within(_QuickHandle(), deadline=1.0)

    asyncio.run(scenario())
