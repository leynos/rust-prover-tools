"""Classify one supervised Kani attempt from its parser facts and termination.

This is the Kani-specific half of supervision. The generic layer owns timing,
measurement, evidence, and accounting; this adapter owns what Kani's output
means. It feeds every streamed line to the shared 0.67.0 reader and classifies
the settled attempt under one documented precedence:

1. a supervisor stop (timeout, memory watchdog, interruption);
2. a launch failure;
3. a selection mismatch (no matching record, several records, or the wrong
   identity);
4. malformed output;
5. a failed proof verdict;
6. a nonzero or signalled exit;
7. success, which additionally requires exactly one matching record, a
   reconciled manual summary, and exit zero.

A low-memory watchdog stop is recorded as the supervisor's own intervention.
The `oom_kill` counter delta, when the cgroup evidence provides one, is carried
separately in the report and is never used to claim a kernel OOM caused a
termination.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log import (
    HarnessBlock,
    KaniLog,
    KaniLogError,
    KaniLogReader,
    grammar_for,
)
from rust_prover_tools.kani_release import KANI_RELEASE_VERSION
from rust_prover_tools.supervision.models import (
    ExecutionStatus,
    HarnessSpec,
    OutputLine,
    ProofVerdict,
    StopReason,
)
from rust_prover_tools.supervision.results import HarnessClassification

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.attempt_state import AttemptEvidence
    from rust_prover_tools.supervision.protocols import LineObserver

__all__ = ("KaniAdapter", "KaniObserver")


@dc.dataclass(slots=True)
class KaniObserver:
    """Accumulate Kani's report as its lines stream.

    Attributes
    ----------
    reader : KaniLogReader
        Incremental parser for this attempt.
    last_progress : str | None
        Last structurally recognised report line.
    """

    reader: KaniLogReader = dc.field(default_factory=KaniLogReader)
    last_progress: str | None = None

    def observe(self, line: OutputLine) -> None:
        """Feed one decoded line to the reader and remember its progress."""
        stream = "stdout" if line.stream == "stdout" else "stderr"
        self.reader.feed_line(stream, line.text)
        progress = self.reader.facts.last_progress
        if progress is not None:
            self.last_progress = progress


@dc.dataclass(frozen=True, slots=True)
class KaniAdapter:
    """Classify Kani 0.67.0 attempts.

    Attributes
    ----------
    version : str
        Exact Kani release whose grammar must be registered. The observer
        refuses an unregistered release before any proof runs.
    """

    version: str = KANI_RELEASE_VERSION

    def observer(self, spec: HarnessSpec) -> KaniObserver:
        """Return a fresh parser for one attempt.

        Raises
        ------
        UnsupportedKaniGrammarError
            Raised when this adapter's release has no registered grammar.
        """
        del spec
        grammar_for(self.version)
        return KaniObserver()

    def classify(
        self,
        evidence: AttemptEvidence,
        observer: LineObserver,
    ) -> HarnessClassification:
        """Classify one settled attempt under the documented precedence."""
        kani = typ.cast("KaniObserver", observer)
        stopped = _stop_status(evidence, kani)
        if stopped is not None:
            return stopped
        if evidence.launch_error is not None:
            return _classification(
                ExecutionStatus.ERROR,
                detail=f"launch failed: {evidence.launch_error}",
            )
        selection = _selection_status(evidence, kani)
        if selection is not None:
            return selection
        return self._classify_log(evidence, kani)

    def _classify_log(
        self,
        evidence: AttemptEvidence,
        observer: KaniObserver,
    ) -> HarnessClassification:
        """Classify the attempt from its completed log."""
        banner = observer.reader.facts.banner_version
        if banner is not None and banner != self.version:
            return _classification(
                ExecutionStatus.ERROR,
                detail=(
                    f"the log names Kani {banner}, but this adapter is "
                    f"qualified for {self.version}"
                ),
                progress=observer.last_progress,
            )
        log = _parse(evidence, observer)
        if log is None:
            return _classification(
                ExecutionStatus.ERROR,
                detail="malformed verifier output",
                progress=observer.last_progress,
            )
        (block,) = log.blocks
        verdict = _proof_verdict(block)
        detail = _detail(evidence)
        if _is_failed(block):
            return _classification(
                ExecutionStatus.FAILED,
                verdict=verdict,
                detail=detail,
                progress=observer.last_progress,
            )
        if evidence.exit_code not in {0, None} or evidence.signal_number is not None:
            return _classification(
                ExecutionStatus.EXIT_NONZERO,
                verdict=verdict,
                detail=detail,
                progress=observer.last_progress,
            )
        if block.succeeded:
            return _classification(
                ExecutionStatus.PASSED,
                verdict=ProofVerdict.PASSED,
                detail=detail,
                progress=observer.last_progress,
            )
        return _classification(
            ExecutionStatus.ERROR,
            verdict=ProofVerdict.UNKNOWN,
            detail=f"{detail}; verifier reported no verdict",
            progress=observer.last_progress,
        )


def _stop_status(
    evidence: AttemptEvidence,
    observer: KaniObserver,
) -> HarnessClassification | None:
    """Return the classification a supervisor stop implies, if any."""
    status = {
        StopReason.TIMEOUT: ExecutionStatus.TIMED_OUT,
        StopReason.MEMORY_WATCHDOG: ExecutionStatus.MEMORY_EXHAUSTED,
        StopReason.INTERRUPTED: ExecutionStatus.INTERRUPTED,
    }.get(evidence.stop_reason)
    if status is None:
        return None
    detail = evidence.stop_detail or evidence.stop_reason.value
    if evidence.os_reported_oom_kill is not None:
        detail = f"{detail}; oom_kill_delta={evidence.os_reported_oom_kill}"
    return _classification(
        status,
        detail=detail,
        progress=observer.last_progress,
    )


def _selection_status(
    evidence: AttemptEvidence,
    observer: KaniObserver,
) -> HarnessClassification | None:
    """Return the classification a selection mismatch implies, if any."""
    records = observer.reader.facts.selection_records
    expected = evidence.spec.harness
    matching = [name for name in records if name == expected]
    if not records:
        return _classification(
            ExecutionStatus.NO_MATCH,
            detail=f"Kani selected no harness; requested {expected}",
            progress=observer.last_progress,
        )
    if len(records) > 1:
        return _classification(
            ExecutionStatus.MULTIPLE_MATCH,
            detail=f"Kani selected {len(records)} harnesses: {list(records)}",
            progress=observer.last_progress,
        )
    if matching != [expected]:
        return _classification(
            ExecutionStatus.NO_MATCH,
            detail=f"Kani selected {records[0]!r}; requested {expected!r}",
            progress=observer.last_progress,
        )
    return None


def _parse(
    evidence: AttemptEvidence,
    observer: KaniObserver,
) -> KaniLog | None:
    """Complete the parser, returning the log or `None` when it refuses."""
    try:
        return observer.reader.finish(evidence.exit_code or 0)
    except KaniLogError:
        return None


def _proof_verdict(block: HarnessBlock) -> ProofVerdict:
    """Return the verifier verdict the block states."""
    if block.succeeded:
        return ProofVerdict.PASSED
    if block.verdict == "FAILED":
        return ProofVerdict.FAILED
    return ProofVerdict.UNKNOWN


def _is_failed(block: HarnessBlock) -> bool:
    """Whether the block reports a failure that is not an expected panic."""
    if block.verdict == "FAILED":
        return True
    failed = [item for item in block.checks if item.status == "FAILURE"]
    return bool(failed) and not block.panics_as_expected


def _detail(evidence: AttemptEvidence) -> str:
    """Describe the termination without conflating a policy stop with an OOM."""
    if evidence.exit_code is None and evidence.signal_number is None:
        detail = "the child reported no exit status"
    elif evidence.signal_number is not None:
        detail = f"child ended by signal {evidence.signal_number}"
    else:
        detail = f"child exited {evidence.exit_code}"
    if evidence.os_reported_oom_kill is not None:
        detail = f"{detail}; oom_kill_delta={evidence.os_reported_oom_kill}"
    return detail


def _classification(
    status: ExecutionStatus,
    *,
    verdict: ProofVerdict = ProofVerdict.UNKNOWN,
    detail: str,
    progress: str | None = None,
) -> HarnessClassification:
    """Build one adapter classification."""
    return HarnessClassification(
        status=status,
        proof_verdict=verdict,
        detail=detail,
        last_progress=progress,
    )
