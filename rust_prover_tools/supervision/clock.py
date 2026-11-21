"""Production time sources for supervised execution.

The clock is two sources, not one: wall-clock time for evidence a human reads,
and a monotonic count for durations and deadlines. Mixing them would make a
deadline jump when the host adjusts its wall clock, and a report's duration
could go backwards. Both are injectable so tests can advance virtual time
without sleeping.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

__all__ = ("AsyncSleeper", "SystemClock")


class SystemClock:
    """Read the host's wall clock and monotonic clock."""

    @staticmethod
    def now() -> dt.datetime:
        """Return the current timezone-aware UTC time.

        Returns
        -------
        datetime.datetime
            Current UTC time.
        """
        return dt.datetime.now(tz=dt.UTC)

    @staticmethod
    def monotonic() -> float:
        """Return the host's monotonic second count.

        Returns
        -------
        float
            Monotonic seconds, comparable only within one host run.
        """
        return time.monotonic()


class AsyncSleeper:
    """Pause a monitor loop on the running event loop."""

    @staticmethod
    async def sleep(seconds: float) -> None:
        """Wait for `seconds` without blocking the loop.

        Parameters
        ----------
        seconds : float
            Non-negative delay.
        """
        await asyncio.sleep(max(0.0, seconds))
