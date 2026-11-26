"""Hold one attempt's accumulated evidence and its summary reductions.

The supervisor accumulates; this module states what the accumulated values
mean. A summary is a function of the samples that were actually taken, so an
absence stays an absence: a run with no samples reports why, never zero.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.models import (
    CgroupObservation,
    HarnessSpec,
    LaunchResult,
    Measurement,
    ResourceSample,
    StopReason,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from rust_prover_tools.supervision.protocols import CgroupReader

__all__ = (
    "AttemptEvidence",
    "AttemptSettlement",
    "cancellation_baseline",
    "cancellation_pending",
    "finish_cgroup",
    "idle_seconds",
    "join_reporting_cancellation",
    "latest",
    "minimum",
    "peak",
    "resolved_reason",
    "start_cgroup",
    "waiter_failed",
)

#: Reason reported when a summary has no sample to summarise.
NO_SAMPLE = "no sample was taken"


@dc.dataclass(frozen=True, slots=True)
class AttemptEvidence:
    """Everything one supervised attempt observed before classification.

    Attributes
    ----------
    spec : HarnessSpec
        Identity that was invoked.
    stop_reason : StopReason
        Why the supervisor stopped waiting.
    exit_code : int | None
        Raw child status, or `None` when the child was signalled or never
        started.
    signal_number : int | None
        POSIX signal that ended the child.
    launch_error : str | None
        Launch failure description, when the child never started.
    started_at : datetime.datetime
        Attempt start time.
    finished_at : datetime.datetime
        Attempt finish time.
    duration_seconds : float
        Attempt duration.
    samples : tuple[ResourceSample, ...]
        Every sample taken, in order.
    peak_group_rss : Measurement
        Largest sampled process-group RSS.
    min_host_available : Measurement
        Smallest sampled host ``MemAvailable``.
    host_total : Measurement
        Latest sampled host ``MemTotal``.
    cgroup : CgroupObservation
        Shared cgroup evidence read before and after the invocation.
    last_output : str | None
        Last decoded output line, bounded by the launcher.
    stop_detail : str
        Bounded explanation for the first stop, when one was requested.
    """

    spec: HarnessSpec
    stop_reason: StopReason
    exit_code: int | None
    signal_number: int | None
    launch_error: str | None
    started_at: dt.datetime
    finished_at: dt.datetime
    duration_seconds: float
    samples: tuple[ResourceSample, ...]
    peak_group_rss: Measurement
    min_host_available: Measurement
    host_total: Measurement
    cgroup: CgroupObservation
    last_output: str | None
    stop_detail: str = ""

    @property
    def os_reported_oom_kill(self) -> int | None:
        """The observed ``oom_kill`` increase, when the cgroup evidence allows it."""
        return self.cgroup.oom_kill_delta


@dc.dataclass(frozen=True, slots=True)
class AttemptSettlement:
    """The raw facts one settled attempt contributes to its evidence.

    The fields travel together from the attempt loop; grouping them keeps the
    evidence builder a single translation rather than a ten-argument call.
    """

    spec: HarnessSpec
    stop_reason: StopReason
    result: LaunchResult
    started_at: dt.datetime
    finished_at: dt.datetime
    duration: float
    samples: tuple[ResourceSample, ...]
    cgroup: CgroupObservation
    last_output: str | None
    stop_detail: str

    def to_evidence(self) -> AttemptEvidence:
        """Build the attempt's evidence with each summary from its samples."""
        return AttemptEvidence(
            spec=self.spec,
            stop_reason=self.stop_reason,
            exit_code=self.result.exit_code,
            signal_number=self.result.signal_number,
            launch_error=self.result.launch_error,
            started_at=self.started_at,
            finished_at=self.finished_at,
            duration_seconds=self.duration,
            samples=self.samples,
            peak_group_rss=peak([sample.group_rss for sample in self.samples]),
            min_host_available=minimum([
                sample.host_available for sample in self.samples
            ]),
            host_total=latest([sample.host_total for sample in self.samples]),
            cgroup=self.cgroup,
            last_output=self.last_output,
            stop_detail=self.stop_detail,
        )


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


def cancellation_baseline() -> int:
    """Return this task's cancellation count as the attempt starts."""
    task = asyncio.current_task()
    return 0 if task is None else task.cancelling()


def cancellation_pending(*, recorded: bool, baseline: int) -> bool:
    """Whether a caller cancellation arrived during this attempt.

    `RunningHarness.settle` deliberately keeps joining after a cancellation, so
    the request can be consumed without ever reaching the caller. The task's
    own `cancelling()` count still reports it, but a count inherited from work
    before the attempt must not turn a completed run into a cancelled one, so
    only an increase over the attempt's baseline counts.
    """
    if recorded:
        return True
    task = asyncio.current_task()
    return task is not None and task.cancelling() > baseline


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


def start_cgroup(reader: CgroupReader) -> CgroupObservation:
    """Read the first cgroup observation before the child starts."""
    path = reader.discover()
    return CgroupObservation(path=path, start=reader.read(path), finish=None)


def finish_cgroup(
    reader: CgroupReader,
    observation: CgroupObservation,
) -> CgroupObservation:
    """Read the finish-time cgroup values for an observation."""
    if observation.path is None:
        return observation
    return dc.replace(observation, finish=reader.read(observation.path))


def idle_seconds(last_output_elapsed: float | None, elapsed: float) -> float:
    """Seconds since the last observed output, or since the start."""
    if last_output_elapsed is None:
        return elapsed
    return max(0.0, elapsed - last_output_elapsed)
