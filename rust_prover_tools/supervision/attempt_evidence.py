"""The typed records one attempt's evidence is assembled from.

These are values, not decisions: the settlement carries the raw facts the
attempt loop observed, and the evidence carries those facts with each summary
already reduced from the samples. Keeping them apart from the reductions lets
the supervisor and the adapters share the shape without importing the policy.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.attempt_state import latest, minimum, peak

if typ.TYPE_CHECKING:
    import datetime as dt

    from rust_prover_tools.supervision.models import (
        CgroupObservation,
        HarnessSpec,
        LaunchResult,
        Measurement,
        ResourceSample,
        StopReason,
    )

__all__ = ("AttemptEvidence", "AttemptSettlement")


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
