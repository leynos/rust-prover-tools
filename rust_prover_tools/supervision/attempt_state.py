"""Reduce one attempt's accumulated samples into its summary facts.

The supervisor accumulates; this module states what the accumulated values
mean. A summary is a function of the samples that were actually taken, so an
absence stays an absence: a run with no samples reports why, never zero.
"""

from __future__ import annotations

import asyncio
import typing as typ

from rust_prover_tools.supervision.models import (
    LaunchResult,
    Measurement,
    StopReason,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


__all__ = (
    "cancellation_pending",
    "idle_seconds",
    "join_reporting_cancellation",
    "latest",
    "minimum",
    "peak",
    "resolved_reason",
    "waiter_failed",
)

#: Reason reported when a summary has no sample to summarise.
NO_SAMPLE = "no sample was taken"


def peak(values: cabc.Sequence[Measurement]) -> Measurement:
    """Return the largest available value, or an explained absence.

    Parameters
    ----------
    values : cabc.Sequence[Measurement]
        Measurements in sample order.

    Returns
    -------
    Measurement
        The maximum, or the first absence when nothing was available.
    """
    available = [value.value for value in values if value.value is not None]
    if available:
        return Measurement.available(max(typ.cast("list[int]", available)))
    if values:
        return values[0]
    return Measurement.unavailable(NO_SAMPLE)


def minimum(values: cabc.Sequence[Measurement]) -> Measurement:
    """Return the smallest available value, or an explained absence.

    Parameters
    ----------
    values : cabc.Sequence[Measurement]
        Measurements in sample order.

    Returns
    -------
    Measurement
        The minimum, or the first absence when nothing was available.
    """
    available = [value.value for value in values if value.value is not None]
    if available:
        return Measurement.available(min(typ.cast("list[int]", available)))
    if values:
        return values[0]
    return Measurement.unavailable(NO_SAMPLE)


def latest(values: cabc.Sequence[Measurement]) -> Measurement:
    """Return the newest available value, or an explained absence.

    Parameters
    ----------
    values : cabc.Sequence[Measurement]
        Measurements in sample order.

    Returns
    -------
    Measurement
        The last available value, or the first absence when none was.
    """
    for value in reversed(values):
        if value.value is not None:
            return value
    if values:
        return values[0]
    return Measurement.unavailable(NO_SAMPLE)


def resolved_reason(stop_reason: StopReason | None, result: LaunchResult) -> StopReason:
    """Name why the supervisor stopped waiting for the child."""
    if stop_reason is not None:
        return stop_reason
    if result.launch_error is not None:
        return StopReason.LAUNCH_FAILED
    return StopReason.EXITED


def waiter_failed(waiter: asyncio.Task[LaunchResult]) -> bool:
    """Whether a finished waiter failed, without consuming a cancellation."""
    if not waiter.done() or waiter.cancelled():
        return False
    return waiter.exception() is not None


def cancellation_pending(
    *,
    recorded: bool,
    baseline: int,
    current_count: int,
) -> bool:
    """Whether a caller cancellation arrived during this attempt.

    `RunningHarness.settle` deliberately keeps joining after a cancellation, so
    the request can be consumed without ever reaching the caller. The supplied
    count still reports it, but a count inherited from work before the attempt
    must not turn a completed run into a cancelled one, so only an increase
    over the attempt's baseline counts. This stays a pure decision: the count
    comes from the caller's `CancellationProbe`.
    """
    return recorded or current_count > baseline


async def join_reporting_cancellation(task: asyncio.Task[typ.Any]) -> bool:
    """Join a task, reporting whether the caller was cancelled while joining.

    Teardown must not abandon a child because a caller was cancelled, so the
    cancellation is recorded here and re-raised by the caller once settlement
    has finished. That keeps the attempt reportable as interrupted instead of
    silently turning a cancelled run into a completed one.
    """
    try:
        await asyncio.gather(task, return_exceptions=True)
    except asyncio.CancelledError:
        return True
    return False


def idle_seconds(last_output_elapsed: float | None, elapsed: float) -> float:
    """Seconds since the last observed output, or since the start."""
    if last_output_elapsed is None:
        return elapsed
    return max(0.0, elapsed - last_output_elapsed)
