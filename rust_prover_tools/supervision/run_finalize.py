"""Turn a finished ledger into the typed run report.

Finalization is a pure mapping from the facts a run accumulated to the values
it publishes. Keeping it apart from the loop makes the aggregate rules — what
counts as complete, what counts as passed, and how a not-run harness is
represented — readable without reading the orchestration that produced them.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.accounting import compare_identities
from rust_prover_tools.supervision.models import (
    ExecutionStatus,
    HarnessSpec,
    Measurement,
    ProofVerdict,
    StopReason,
)
from rust_prover_tools.supervision.results import (
    HarnessClassification,
    HarnessResult,
    InventoryDrift,
    RunOutcome,
    RunReport,
)

if typ.TYPE_CHECKING:
    import datetime as dt

    from rust_prover_tools.supervision.attempt_state import AttemptEvidence
    from rust_prover_tools.supervision.results import DiagnosticRef

__all__ = (
    "CANCELLED_DETAIL",
    "INTERRUPTED_DETAIL",
    "NOT_RUN_DETAIL",
    "ReportInput",
    "RunLedger",
    "assemble_report",
    "not_run_result",
    "result_for",
)

NOT_RUN_DETAIL: typ.Final[str] = "the run stopped before this harness"
INTERRUPTED_DETAIL: typ.Final[str] = "a signal interrupted this harness"
CANCELLED_DETAIL: typ.Final[str] = (
    "the run task was cancelled before this harness completed"
)


@dc.dataclass(slots=True)
class RunLedger:
    """The accumulating facts of one run."""

    expected: tuple[HarnessSpec, ...]
    results: list[HarnessResult] = dc.field(default_factory=list)
    executed: list[str] = dc.field(default_factory=list)
    interrupted: bool = False
    interrupt_signal: int | None = None
    finished_at: dt.datetime | None = None
    diagnostic_error: Exception | None = None
    active: HarnessResult | None = None


@dc.dataclass(frozen=True, slots=True)
class ReportInput:
    """The ledger facts a report is assembled from.

    Attributes
    ----------
    expected : tuple[HarnessSpec, ...]
        Expected identities.
    results : tuple[HarnessResult, ...]
        One result per expected identity.
    executed : tuple[str, ...]
        Package-qualified identities that actually ran.
    interrupted : bool
        Whether a signal interrupted the run.
    interrupt_signal : int | None
        Signal that interrupted the run.
    run_id : str
        Run identifier.
    started_at : datetime.datetime
        Run start time.
    finished_at : datetime.datetime
        Run finish time.
    """

    expected: tuple[HarnessSpec, ...]
    results: tuple[HarnessResult, ...]
    executed: tuple[str, ...]
    interrupted: bool
    interrupt_signal: int | None
    run_id: str
    started_at: dt.datetime
    finished_at: dt.datetime


def assemble_report(data: ReportInput) -> RunReport:
    """Assemble the typed report and its aggregate outcome."""
    accounting = compare_identities(
        (spec.qualified for spec in data.expected), data.executed
    )
    outcome = outcome_for(data, accounting)
    return RunReport(
        schema_version=1,
        run_id=data.run_id,
        started_at=data.started_at,
        finished_at=data.finished_at,
        expected=data.expected,
        results=data.results,
        accounting=accounting,
        outcome=outcome,
        interrupted=data.interrupted,
        interrupt_signal=data.interrupt_signal,
    )


def outcome_for(data: ReportInput, accounting: InventoryDrift) -> RunOutcome:
    """Decide the aggregate outcome from the accounting and the statuses."""
    if data.interrupted:
        return RunOutcome.INTERRUPTED
    if not accounting.is_empty:
        return RunOutcome.INCOMPLETE
    if all(result.status is ExecutionStatus.PASSED for result in data.results):
        return RunOutcome.PASSED
    return RunOutcome.FAILED


def result_for(
    evidence: AttemptEvidence,
    classification: HarnessClassification,
    diagnostic: DiagnosticRef | None,
) -> HarnessResult:
    """Combine an attempt's evidence with its adapter classification."""
    return HarnessResult(
        spec=evidence.spec,
        status=classification.status,
        stop_reason=evidence.stop_reason,
        exit_code=evidence.exit_code,
        signal_number=evidence.signal_number,
        proof_verdict=evidence_proof_verdict(classification),
        started_at=evidence.started_at,
        finished_at=evidence.finished_at,
        duration_seconds=evidence.duration_seconds,
        peak_group_rss=evidence.peak_group_rss,
        min_host_available=evidence.min_host_available,
        host_total=evidence.host_total,
        cgroup=evidence.cgroup,
        last_output=evidence.last_output,
        last_progress=classification.last_progress,
        diagnostic=diagnostic,
        detail=classification.detail or evidence.stop_detail,
        executed=evidence.stop_reason is not StopReason.LAUNCH_FAILED,
    )


def evidence_proof_verdict(classification: HarnessClassification) -> ProofVerdict:
    """Return the verifier verdict an adapter classification carries."""
    return classification.proof_verdict


def not_run_result(spec: HarnessSpec, detail: str) -> HarnessResult:
    """Return the result for a harness the run did not execute."""
    absent = Measurement.unavailable("the harness did not run")
    return HarnessResult(
        spec=spec,
        status=ExecutionStatus.NOT_RUN,
        stop_reason=None,
        exit_code=None,
        signal_number=None,
        proof_verdict=ProofVerdict.UNKNOWN,
        started_at=None,
        finished_at=None,
        duration_seconds=0.0,
        peak_group_rss=absent,
        min_host_available=absent,
        host_total=absent,
        cgroup=None,
        last_output=None,
        last_progress=None,
        diagnostic=None,
        detail=detail,
        executed=False,
    )
