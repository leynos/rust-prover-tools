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
    ASSERTION_KIND,
    HarnessBlock,
    KaniLog,
    KaniLogError,
    KaniLogReader,
    UnsupportedKaniGrammarError,
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
        Last structurally recognized report line.
    banner_error : str | None
        Why an opening banner could not be attributed, when it could not be.
    failure_reason : str | None
        Why the parser refused the completed log, when it did.
    """

    reader: KaniLogReader = dc.field(default_factory=KaniLogReader)
    last_progress: str | None = None
    banner_error: str | None = None
    failure_reason: str | None = None

    def observe(self, line: OutputLine) -> None:
        """Feed one decoded line to the reader and remember its progress.

        An unregistered banner is retained as evidence instead of escaping
        through the drain loop. Losing the whole run to one line would discard
        the measurements and records already emitted for earlier harnesses.
        """
        if self.banner_error is not None:
            return
        stream = "stdout" if line.stream == "stdout" else "stderr"
        try:
            self.reader.feed_line(stream, line.text)
        except UnsupportedKaniGrammarError as error:
            self.banner_error = str(error)
            return
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
        observed = _observed(evidence, kani)
        stopped = _stop_status(evidence, kani, observed)
        if stopped is not None:
            return stopped
        if evidence.launch_error is not None:
            return _classification(
                ExecutionStatus.ERROR,
                detail=f"launch failed: {evidence.launch_error}",
                observed=observed,
            )
        if kani.banner_error is not None:
            return _classification(
                ExecutionStatus.ERROR,
                detail=kani.banner_error,
                observed=observed,
            )
        selection = _selection_status(evidence, kani, observed)
        if selection is not None:
            return selection
        return self._classify_log(evidence, kani, observed)

    def _classify_log(
        self,
        evidence: AttemptEvidence,
        observer: KaniObserver,
        observed: _Observed,
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
                observed=observed,
            )
        log = _parse(evidence, observer)
        if log is None:
            reason = observer.failure_reason or "unknown refusal"
            return _classification(
                ExecutionStatus.ERROR,
                detail=f"malformed verifier output: {reason}",
                observed=observed,
            )
        (block,) = log.blocks
        verdict = dc.replace(observed, verdict=_proof_verdict(block))
        detail = _detail(evidence)
        if _is_failed(block):
            return _classification(
                ExecutionStatus.FAILED, detail=detail, observed=verdict
            )
        if evidence.exit_code is None:
            status, reason = ExecutionStatus.ERROR, "no exit status"
        elif evidence.exit_code != 0 or evidence.signal_number is not None:
            status, reason = ExecutionStatus.EXIT_NONZERO, detail
        else:
            status = None
        if status is not None:
            return _classification(status, detail=reason, observed=verdict)
        if block.succeeded:
            return _classification(
                ExecutionStatus.PASSED,
                detail=detail,
                observed=dc.replace(verdict, verdict=ProofVerdict.PASSED),
            )
        return _classification(
            ExecutionStatus.ERROR,
            detail=f"{detail}; verifier reported no verdict",
            observed=dc.replace(verdict, verdict=ProofVerdict.UNKNOWN),
        )


def _observed(evidence: AttemptEvidence, observer: KaniObserver) -> _Observed:
    """Return every fact the attempt produced, independent of its status."""
    facts = observer.reader.facts
    return _Observed(
        verdict=_observed_verdict(evidence, observer),
        progress=facts.last_progress,
        identities=tuple(
            f"{evidence.spec.package}::{name}" for name in facts.selection_records
        ),
    )


def _observed_verdict(
    evidence: AttemptEvidence,
    observer: KaniObserver,
) -> ProofVerdict:
    """Return the verdict the attempt's own log already reported, if any.

    A stop outranks the status, but not evidence the verifier already
    produced. Recording the verdict separately keeps a timeout from erasing
    the fact that the harness had verified before the supervisor ended it.
    """
    records = observer.reader.facts.verdicts
    matching = [verdict for name, verdict in records if name == evidence.spec.harness]
    if len(matching) != 1:
        return ProofVerdict.UNKNOWN
    return ProofVerdict.PASSED if matching[0] == "SUCCESSFUL" else ProofVerdict.FAILED


def _verifier_ran(observer: KaniObserver) -> bool:
    """Whether the attempt produced any evidence the verifier itself ran.

    A child that exits nonzero without printing a banner or a selection record
    may never have reached the verifier. Calling that "no match" asserts a
    selection diagnosis the output does not support.
    """
    facts = observer.reader.facts
    return bool(
        facts.banner_version
        or facts.selection_records
        or facts.verdicts
        or facts.last_progress
    )


def _stop_status(
    evidence: AttemptEvidence,
    observer: KaniObserver,
    observed: _Observed,
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
    del observer
    return _classification(status, detail=detail, observed=observed)


def _selection_status(
    evidence: AttemptEvidence,
    observer: KaniObserver,
    observed: _Observed,
) -> HarnessClassification | None:
    """Return the classification a selection mismatch implies, if any."""
    records = observer.reader.facts.selection_records
    expected = evidence.spec.harness
    matching = [name for name in records if name == expected]
    if not records:
        if not _verifier_ran(observer):
            # Nothing was printed, so nothing about the selection is known.
            # The raw exit status is the only evidence, and inventing a
            # selection diagnosis would name a cause the output never showed.
            return _classification(
                ExecutionStatus.EXIT_NONZERO,
                detail=_no_output_detail(evidence),
                observed=observed,
            )
        return _classification(
            ExecutionStatus.NO_MATCH,
            detail=f"Kani selected no harness; requested {expected}",
            observed=observed,
        )
    if len(records) > 1:
        return _classification(
            ExecutionStatus.MULTIPLE_MATCH,
            detail=f"Kani selected {len(records)} harnesses: {list(records)}",
            observed=observed,
        )
    if matching != [expected]:
        return _classification(
            ExecutionStatus.NO_MATCH,
            detail=f"Kani selected {records[0]!r}; requested {expected!r}",
            observed=observed,
        )
    return None


def _no_output_detail(evidence: AttemptEvidence) -> str:
    """Describe a silent failure without claiming a cause it did not show."""
    if evidence.signal_number is not None:
        return f"child ended by signal {evidence.signal_number} with no output"
    return (
        f"child exited {evidence.exit_code} with no output; the verifier never "
        "reported a selection"
    )


def _parse(
    evidence: AttemptEvidence,
    observer: KaniObserver,
) -> KaniLog | None:
    """Complete the parser, returning the log or `None` when it refuses."""
    try:
        return observer.reader.finish(evidence.exit_code or 0)
    except KaniLogError as error:
        observer.failure_reason = error.reason
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
    if not failed or not block.panics_as_expected:
        return bool(failed)
    return any(item.kind != ASSERTION_KIND for item in failed)


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


@dc.dataclass(frozen=True, slots=True)
class _Observed:
    """The evidence one attempt produced, independent of its status."""

    verdict: ProofVerdict = ProofVerdict.UNKNOWN
    progress: str | None = None
    identities: tuple[str, ...] = ()


def _classification(
    status: ExecutionStatus,
    *,
    detail: str,
    observed: _Observed | None = None,
) -> HarnessClassification:
    """Build one adapter classification."""
    facts = observed or _Observed()
    return HarnessClassification(
        status=status,
        proof_verdict=facts.verdict,
        detail=detail,
        last_progress=facts.progress,
        observed=facts.identities,
    )
