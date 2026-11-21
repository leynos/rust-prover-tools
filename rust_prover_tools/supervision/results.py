"""Typed results a supervised run reports.

These are the finished facts: what one harness produced, how the executed
identities compare with the expected ones, and what the run as a whole
reported. They live apart from the mutable and observational values in
``models`` so a report can be constructed and read without importing the
supervision machinery that produced it.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

from rust_prover_tools.supervision.models import (
    ExecutionStatus,
    HarnessSpec,
    Measurement,
    ProofVerdict,
    StopReason,
)

if typ.TYPE_CHECKING:
    import datetime as dt

    from rust_prover_tools.supervision.models import CgroupObservation

__all__ = (
    "AttemptOutcome",
    "DiagnosticRef",
    "HarnessClassification",
    "HarnessResult",
    "InventoryDrift",
    "RunOutcome",
    "RunReport",
)


class RunOutcome(enum.StrEnum):
    """The aggregate outcome of a supervised run."""

    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    INTERRUPTED = "interrupted"


@dc.dataclass(frozen=True, slots=True)
class DiagnosticRef:
    """A pointer to where one harness's evidence begins in a diagnostic sink.

    Attributes
    ----------
    sink : str
        Sink name, such as ``jsonl``.
    offset : int
        Byte offset of the harness's first record in that sink.
    """

    sink: str
    offset: int


@dc.dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """Everything an adapter needs to classify one finished attempt.

    Attributes
    ----------
    spec : HarnessSpec
        Requested identity.
    stop_reason : StopReason
        Why the supervisor stopped waiting.
    exit_code : int | None
        Raw child status.
    signal_number : int | None
        POSIX signal that ended the child.
    duration_seconds : float
        Wall-clock duration of the attempt.
    last_output : str | None
        Last decoded output line, bounded.
    """

    spec: HarnessSpec
    stop_reason: StopReason
    exit_code: int | None
    signal_number: int | None
    duration_seconds: float
    last_output: str | None


@dc.dataclass(frozen=True, slots=True)
class HarnessClassification:
    """An adapter's classification of one attempt.

    Attributes
    ----------
    status : ExecutionStatus
        Documented outcome.
    proof_verdict : ProofVerdict
        What the verifier said about the proof.
    detail : str
        Bounded explanation for the table and report.
    last_progress : str | None
        Last recognised prover progress line, bounded.
    """

    status: ExecutionStatus
    proof_verdict: ProofVerdict = ProofVerdict.UNKNOWN
    detail: str = ""
    last_progress: str | None = None


@dc.dataclass(frozen=True, slots=True)
class HarnessResult:
    """The complete result for one harness identity.

    Attributes
    ----------
    spec : HarnessSpec
        Requested identity.
    status : ExecutionStatus
        Documented outcome.
    stop_reason : StopReason | None
        Why the supervisor stopped, or `None` when the harness never ran.
    exit_code : int | None
        Raw child status.
    signal_number : int | None
        POSIX signal that ended the child.
    proof_verdict : ProofVerdict
        Verifier verdict, when one was available.
    started_at : datetime.datetime | None
        Attempt start time.
    finished_at : datetime.datetime | None
        Attempt finish time.
    duration_seconds : float
        Attempt duration.
    peak_group_rss : Measurement
        Maximum sampled process-group RSS.
    min_host_available : Measurement
        Minimum sampled host ``MemAvailable``.
    host_total : Measurement
        Latest sampled host ``MemTotal``.
    cgroup : CgroupObservation | None
        Shared cgroup evidence.
    last_output : str | None
        Last decoded output line, bounded.
    last_progress : str | None
        Last recognised progress line, bounded.
    diagnostic : DiagnosticRef | None
        Where this harness's evidence begins.
    detail : str
        Bounded explanation.
    executed : bool
        Whether the harness ran.
    """

    spec: HarnessSpec
    status: ExecutionStatus
    stop_reason: StopReason | None
    exit_code: int | None
    signal_number: int | None
    proof_verdict: ProofVerdict
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    duration_seconds: float
    peak_group_rss: Measurement
    min_host_available: Measurement
    host_total: Measurement
    cgroup: CgroupObservation | None
    last_output: str | None
    last_progress: str | None
    diagnostic: DiagnosticRef | None
    detail: str
    executed: bool = True


@dc.dataclass(frozen=True, slots=True)
class InventoryDrift:
    """Differences between the expected and executed identity multisets.

    Attributes
    ----------
    missing : tuple[str, ...]
        Expected identities not executed, one entry per missing occurrence.
    duplicate : tuple[str, ...]
        Identities executed more than once.
    unexpected : tuple[str, ...]
        Identities executed but not expected.
    """

    missing: tuple[str, ...] = ()
    duplicate: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether the multisets agree."""
        return not (self.missing or self.duplicate or self.unexpected)


@dc.dataclass(frozen=True, slots=True)
class RunReport:
    """The typed result of one supervised run.

    Attributes
    ----------
    schema_version : int
        Report schema version.
    run_id : str
        Unique run identifier.
    started_at : datetime.datetime
        Run start time.
    finished_at : datetime.datetime | None
        Run finish time, when the run finalized.
    expected : tuple[HarnessSpec, ...]
        Expected inventory, in selection order.
    results : tuple[HarnessResult, ...]
        One result per expected identity.
    accounting : InventoryDrift
        Multiset comparison of expected and executed identities.
    outcome : RunOutcome
        Aggregate outcome.
    interrupted : bool
        Whether a signal interrupted the run.
    interrupt_signal : int | None
        Signal that interrupted the run.
    """

    schema_version: int
    run_id: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    expected: tuple[HarnessSpec, ...]
    results: tuple[HarnessResult, ...]
    accounting: InventoryDrift
    outcome: RunOutcome
    interrupted: bool = False
    interrupt_signal: int | None = None

    @property
    def is_complete(self) -> bool:
        """Whether every expected harness executed exactly once."""
        return self.accounting.is_empty and all(
            result.executed for result in self.results
        )

    @property
    def passed(self) -> bool:
        """Whether the run is complete and every harness passed."""
        return (
            self.is_complete
            and not self.interrupted
            and self.outcome is RunOutcome.PASSED
        )
