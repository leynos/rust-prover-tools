"""Typed values shared by supervised harness execution.

The models keep four questions apart, because collapsing any two of them loses
evidence a failed run needs:

- What did the child process do? ``StopReason`` and the raw exit status.
- What did the verifier say about the proof? ``ProofVerdict``.
- What does the run report? ``ExecutionStatus``, which combines the two under a
  documented precedence.
- What did the machine measure? ``Measurement``, which is either an available
  integer or an explicit reason it is unavailable.

Every measurement is optional by construction. A host without cgroup v2, a
process that exited between sampling reads, or a launcher that never reported a
pid produces an ``Unavailable`` value rather than a zero that could be mistaken
for evidence.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    import datetime as dt

__all__ = (
    "CgroupMembership",
    "CgroupMemory",
    "CgroupObservation",
    "ExecutionStatus",
    "HarnessSpec",
    "LaunchResult",
    "Measurement",
    "OutputLine",
    "ProofVerdict",
    "ResourceSample",
    "StopReason",
)


class ExecutionStatus(enum.StrEnum):
    """The documented outcome of one supervised harness."""

    PASSED = "passed"
    FAILED = "failed"
    NO_MATCH = "no-match"
    MULTIPLE_MATCH = "multiple-match"
    TIMED_OUT = "timed-out"
    MEMORY_EXHAUSTED = "memory-exhausted"
    INTERRUPTED = "interrupted"
    EXIT_NONZERO = "exit-nonzero"
    ERROR = "error"
    NOT_RUN = "not-run"


class ProofVerdict(enum.StrEnum):
    """What the verifier reported about the proof itself."""

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class StopReason(enum.StrEnum):
    """Why the supervisor stopped waiting for the child."""

    EXITED = "exited"
    TIMEOUT = "timeout"
    MEMORY_WATCHDOG = "memory-watchdog"
    INTERRUPTED = "interrupted"
    LAUNCH_FAILED = "launch-failed"


@dc.dataclass(frozen=True, slots=True)
class Measurement:
    """One optional byte measurement.

    Attributes
    ----------
    value : int | None
        Measured bytes, or `None` when the value is unavailable.
    unavailable_reason : str | None
        Why the value is unavailable, or `None` when it is available.
    """

    value: int | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Refuse a measurement that is neither available nor explained."""
        if (self.value is None) == (self.unavailable_reason is None):
            msg = "a measurement needs exactly one of value or unavailable_reason"
            raise ValueError(msg)
        if self.value is not None:
            if not isinstance(self.value, int) or isinstance(self.value, bool):
                msg = "a measurement value must be an integer"
                raise ValueError(msg)
            if self.value < 0:
                msg = "a measurement cannot be negative"
                raise ValueError(msg)

    @classmethod
    def available(cls, value: int) -> Measurement:
        """Return an available measurement.

        Parameters
        ----------
        value : int
            Measured bytes. Negative values are refused.

        Returns
        -------
        Measurement
            The available value.
        """
        if value < 0:
            msg = "a measurement cannot be negative"
            raise ValueError(msg)
        return cls(value=value)

    @classmethod
    def unavailable(cls, reason: str) -> Measurement:
        """Return an unavailable measurement with its reason.

        Parameters
        ----------
        reason : str
            Why the value could not be measured.

        Returns
        -------
        Measurement
            The explicit absence.
        """
        return cls(unavailable_reason=reason)

    @property
    def is_available(self) -> bool:
        """Whether the value was measured."""
        return self.value is not None


@dc.dataclass(frozen=True, slots=True)
class OutputLine:
    """One decoded child output line with its arrival evidence.

    Attributes
    ----------
    stream : str
        ``"stdout"`` or ``"stderr"``.
    text : str
        Decoded line without its terminator.
    timestamp_utc : datetime.datetime
        Wall-clock arrival time, timezone-aware.
    elapsed_seconds : float
        Seconds since the harness invocation started.
    """

    stream: str
    text: str
    timestamp_utc: dt.datetime
    elapsed_seconds: float


@dc.dataclass(frozen=True, slots=True)
class LaunchResult:
    """What the launcher learned when a child finished or failed to start.

    Attributes
    ----------
    exit_code : int | None
        Raw child status, negative when the child was signalled, or `None`
        when no status exists.
    signal_number : int | None
        POSIX signal that ended the child, when it was signalled.
    launch_error : str | None
        Description of a failure before or during launch, when one occurred.
    """

    exit_code: int | None = None
    signal_number: int | None = None
    launch_error: str | None = None


@dc.dataclass(frozen=True, slots=True)
class HarnessSpec:
    """One harness invocation identity.

    Attributes
    ----------
    package : str
        Cargo package that owns the harness.
    harness : str
        Fully qualified Kani pretty name used with ``--harness``.
    attempt : int
        One-based attempt ordinal. The supervisor runs one attempt per harness;
        the field identifies the invocation in evidence.
    """

    package: str
    harness: str
    attempt: int = 1

    def __post_init__(self) -> None:
        """Refuse an empty identity or a non-positive attempt."""
        if not self.package or not self.harness:
            msg = "a harness identity needs a package and a harness name"
            raise ValueError(msg)
        if self.attempt < 1:
            msg = "a harness attempt must be positive"
            raise ValueError(msg)

    @property
    def qualified(self) -> str:
        """The package-qualified identity used in reports and accounting."""
        return f"{self.package}::{self.harness}"


@dc.dataclass(frozen=True, slots=True)
class ResourceSample:
    """One periodic resource observation for an active harness.

    Attributes
    ----------
    timestamp_utc : datetime.datetime
        Wall-clock sample time.
    elapsed_seconds : float
        Seconds since the harness invocation started.
    package : str
        Package that owns the harness.
    harness : str
        Harness pretty name.
    attempt : int
        Attempt ordinal.
    group_rss : Measurement
        Summed resident set size of the owned process group.
    host_available : Measurement
        Host ``MemAvailable``.
    host_total : Measurement
        Host ``MemTotal``.
    """

    timestamp_utc: dt.datetime
    elapsed_seconds: float
    package: str
    harness: str
    attempt: int
    group_rss: Measurement
    host_available: Measurement
    host_total: Measurement


#: Shared absence for a counter an older reader did not provide.
_COUNTER_NOT_READ = Measurement.unavailable(
    "the reader did not report this memory.events counter"
)


@dc.dataclass(frozen=True, slots=True)
class CgroupMemory:
    """The cgroup v2 memory files read at one instant.

    Attributes
    ----------
    current : Measurement
        ``memory.current``.
    peak : Measurement
        ``memory.peak``, which older kernels do not provide.
    oom_kill : Measurement
        ``oom_kill`` counter from ``memory.events``.
    oom : Measurement
        ``oom`` counter from ``memory.events``.
    max_events : Measurement
        ``max`` counter from ``memory.events``: how often the cgroup's
        ``memory.max`` limit was reached.
    """

    current: Measurement
    peak: Measurement
    oom_kill: Measurement
    oom: Measurement = _COUNTER_NOT_READ
    max_events: Measurement = _COUNTER_NOT_READ


@dc.dataclass(frozen=True, slots=True)
class CgroupMembership:
    """The cgroup v2 directory a supervisor discovered, or why it found none.

    A host with no cgroup v2 membership and a membership file that could not
    be read are different facts: the first is ordinary, the second means the
    evidence is missing for a reason worth reporting. Keeping them apart stops
    a read failure from being reported as an absent hierarchy.

    Attributes
    ----------
    path : str | None
        The resolved cgroup directory, or `None` when none was found.
    unavailable_reason : str | None
        Why no path was found, or `None` when `path` is set.
    """

    path: str | None = None
    unavailable_reason: str | None = None

    @classmethod
    def absent(cls) -> CgroupMembership:
        """Report a host that exposes no cgroup v2 membership."""
        return cls(unavailable_reason="no cgroup v2 membership was found")

    @classmethod
    def unreadable(cls, reason: str) -> CgroupMembership:
        """Report a membership that could not be read, with the reason."""
        return cls(unavailable_reason=reason)


@dc.dataclass(frozen=True, slots=True)
class CgroupObservation:
    """The shared cgroup evidence for one harness attempt.

    Attributes
    ----------
    path : str | None
        Discovered cgroup v2 path, or `None` when none was found.
    start : CgroupMemory | None
        Values read when the harness started.
    finish : CgroupMemory | None
        Values read when the harness finished.
    unavailable_reason : str | None
        Why the path or its values are unavailable.
    """

    path: str | None
    start: CgroupMemory | None
    finish: CgroupMemory | None
    unavailable_reason: str | None = None

    @property
    def oom_kill_delta(self) -> int | None:
        """The observed ``oom_kill`` increase, when both reads are available."""
        if self.start is None or self.finish is None:
            return None
        if not self.start.oom_kill.is_available:
            return None
        if not self.finish.oom_kill.is_available:
            return None
        start = typ.cast("int", self.start.oom_kill.value)
        finish = typ.cast("int", self.finish.oom_kill.value)
        return max(0, finish - start)
