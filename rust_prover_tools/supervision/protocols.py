"""The seams that make supervised execution testable without a live prover.

Every dependency the runner cannot control — time, process launch, resource
inspection, signal delivery, evidence destinations — is a protocol here. The
production implementations are small; the test doubles are smaller still, and
neither the runner nor an adapter needs to know which it received.

The launcher protocol deliberately returns the outcome of a finished child
rather than a live handle. A handle would force each caller to reconcile
settlement, streams, and cancellation itself, which is exactly the machinery a
consumer should not have to copy. Stopping a running child is expressed by
``stop`` receiving a reason, so the launcher owns how a stop is delivered.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from rust_prover_tools.supervision.launcher import LaunchCommand
    from rust_prover_tools.supervision.models import (
        CgroupMemory,
        HarnessSpec,
        LaunchResult,
        ResourceSample,
        StopReason,
    )

__all__ = (
    "CgroupReader",
    "Clock",
    "EvidenceSink",
    "HarnessLauncher",
    "InterruptSource",
    "Launcher",
    "LineConsumer",
    "ResourceProbe",
    "RunningHarnessHandle",
    "Sleeper",
    "Stopper",
)


class Clock(typ.Protocol):
    """The wall-clock and monotonic sources a run reads."""

    def now(self) -> dt.datetime:
        """Return the current timezone-aware UTC time."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic second count, for durations only."""
        ...


class Sleeper(typ.Protocol):
    """The pause a monitor loop makes between checks."""

    async def sleep(self, seconds: float) -> None:
        """Wait for `seconds`, or return early when the run is stopping."""
        ...


class LineConsumer(typ.Protocol):
    """A destination for one decoded child output line."""

    def __call__(self, spec: HarnessSpec, line: object) -> None:
        """Consume one `OutputLine` for `spec`."""
        ...


class RunningHarnessHandle(typ.Protocol):
    """The supervisor's view of one running child."""

    @property
    def done(self) -> bool:
        """Whether the child has finished, whether normally or after a stop."""
        ...

    @property
    def group_id(self) -> int | None:
        """The child's process-group ID, when the run has reported one."""
        ...

    @property
    def stop_reason(self) -> StopReason | None:
        """The stop the supervisor requested, if any."""
        ...

    def stop(self, reason: StopReason) -> None:
        """Request a stop for `reason`, keeping the first request."""
        ...

    async def wait(self) -> LaunchResult:
        """Wait for the child to finish and return its termination evidence."""
        ...


class HarnessLauncher(typ.Protocol):
    """Starts one harness invocation and returns a handle to it."""

    def start(
        self,
        spec: HarnessSpec,
        build: cabc.Callable[[HarnessSpec], LaunchCommand],
        on_line: cabc.Callable[[object], None],
    ) -> RunningHarnessHandle:
        """Start one invocation and return its running handle."""
        ...


class Launcher(typ.Protocol):
    """Runs one harness invocation and owns its process group."""

    async def run(
        self,
        spec: HarnessSpec,
        on_line: cabc.Callable[[object], None],
        stop: Stopper,
    ) -> LaunchResult:
        """Run one invocation to completion or to a requested stop.

        Parameters
        ----------
        spec : HarnessSpec
            Identity and attempt to run.
        on_line : cabc.Callable[[object], None]
            Receives one `OutputLine` per decoded child line, in arrival
            order. It is called synchronously from the drain loop, so it must
            not block.
        stop : Stopper
            Handle the supervisor uses to stop the child, and to learn whether
            a stop was requested before the child exited.

        Returns
        -------
        LaunchResult
            The child's status, or the launch failure.
        """
        ...


class Stopper(typ.Protocol):
    """The supervisor's view of a running child, for stopping it."""

    @property
    def requested(self) -> StopReason | None:
        """The stop the supervisor has requested, if any."""
        ...

    def request(self, reason: StopReason) -> None:
        """Request a stop for `reason`, recording the first request only."""
        ...

    async def stopped(self) -> None:
        """Wait until the child has been stopped and reaped."""
        ...


class ResourceProbe(typ.Protocol):
    """Reads the process-group and host memory values for one sample."""

    def sample(
        self,
        spec: HarnessSpec,
        *,
        group_id: int | None,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> ResourceSample:
        """Return one resource sample for a running invocation.

        Parameters
        ----------
        spec : HarnessSpec
            Identity the sample belongs to.
        group_id : int | None
            Owned process-group ID, or `None` when the launcher reported none.
        started_at : datetime.datetime
            Wall-clock start of the invocation.
        started_monotonic : float
            Monotonic start of the invocation.

        Returns
        -------
        ResourceSample
            The measured values, with explained absences where a read failed.
        """
        ...


class CgroupReader(typ.Protocol):
    """Reads the shared cgroup evidence at the start and finish of a harness."""

    def discover(self) -> str | None:
        """Return the applicable cgroup v2 path, or `None` when none exists."""
        ...

    def read(self, path: str | None) -> CgroupMemory:
        """Return one instant of the cgroup's memory files."""
        ...


class InterruptSource(typ.Protocol):
    """Reports whether a signal has asked the run to stop."""

    @property
    def signum(self) -> int | None:
        """The interrupting signal, or `None` while the run may continue."""
        ...

    async def wait(self) -> None:
        """Wait until a signal asks the run to stop."""
        ...


class EvidenceSink(typ.Protocol):
    """A destination for versioned evidence records."""

    def write(self, record: dict[str, object]) -> None:
        """Write one record, flushed before returning."""
        ...

    def flush(self) -> None:
        """Flush everything written so far."""
        ...

    @property
    def offset(self) -> int:
        """The number of bytes written so far."""
        ...
