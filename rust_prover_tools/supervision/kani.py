"""Classify one supervised Kani attempt from the shared parser and termination.

This is the Kani-specific half of supervision. The generic layer owns timing,
measurement, evidence, and accounting; this adapter owns what Kani's output
means. It reuses the repository's single Kani parser, ``kani_log``, and
classifies the settled attempt under one documented precedence:

1. a supervisor stop (timeout, memory watchdog, interruption);
2. a launch failure;
3. a selection mismatch (no matching record, several records, or the wrong
   identity);
4. malformed output;
5. a failed proof verdict;
6. a nonzero or signalled exit;
7. success, which additionally requires exactly one matching record, a
   reconciled manual summary, and exit zero.

Live progress is read from the same grammar the parser uses, so there is one
definition of what a banner, a selection record, and a verdict look like. The
log itself is spooled to a temporary file and parsed once the child settles:
the parser needs random access over the whole report, and a file keeps that
working set off the supervisor's heap. A log larger than the configured budget
is reported as truncated evidence rather than retained without bound.

A low-memory watchdog stop is recorded as the supervisor's own intervention.
The `oom_kill` counter delta, when the cgroup evidence provides one, is carried
separately in the report and is never used to claim a kernel OOM caused a
termination.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_cli_grammar import UnsupportedKaniGrammarError
from rust_prover_tools.kani_log import (
    ASSERTION_KIND,
    SUPPORTED_KANI_VERSIONS,
    HarnessBlock,
)
from rust_prover_tools.kani_release import KANI_RELEASE_VERSION
from rust_prover_tools.supervision.kani_observer import (
    DEFAULT_MAX_LOG_BYTES,
    KaniLiveFacts,
    KaniObserver,
)
from rust_prover_tools.supervision.models import (
    ExecutionStatus,
    HarnessSpec,
    ProofVerdict,
    StopReason,
)
from rust_prover_tools.supervision.results import HarnessClassification

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.attempt_state import AttemptEvidence
    from rust_prover_tools.supervision.protocols import LineObserver

__all__ = (
    "DEFAULT_MAX_LOG_BYTES",
    "KaniAdapter",
    "KaniLiveFacts",
    "KaniObserver",
)


@dc.dataclass(frozen=True, slots=True)
class KaniAdapter:
    """Classify Kani 0.67.0 attempts.

    Attributes
    ----------
    version : str
        Exact Kani release whose grammar must be registered. The observer
        refuses an unregistered release before any proof runs.
    max_log_bytes : int
        Largest log retained for the final parse.
    """

    version: str = KANI_RELEASE_VERSION
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES

    def observer(self, spec: HarnessSpec) -> KaniObserver:
        """Return a fresh observer for one attempt.

        Raises
        ------
        UnsupportedKaniGrammarError
            Raised when this adapter's release is not one the shared parser
            reads.
        """
        del spec
        if self.version not in SUPPORTED_KANI_VERSIONS:
            raise UnsupportedKaniGrammarError(self.version, SUPPORTED_KANI_VERSIONS)
        return KaniObserver(max_log_bytes=self.max_log_bytes)

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
        if kani.banner_version is not None and kani.banner_version != self.version:
            return _classification(
                ExecutionStatus.ERROR,
                detail=(
                    f"the log names Kani {kani.banner_version}, but this adapter "
                    f"is qualified for {self.version}"
                ),
                observed=observed,
            )
        selection = _selection_status(evidence, kani, observed)
        if selection is not None:
            return selection
        return _classify_log(self.version, evidence, kani, observed)


def _observed(evidence: AttemptEvidence, observer: KaniObserver) -> _Observed:
    """Return every fact the attempt produced, independent of its status."""
    facts = observer.facts
    return _Observed(
        verdict=_observed_verdict(evidence, facts),
        progress=facts.last_progress,
        identities=tuple(
            f"{evidence.spec.package}::{name}" for name in facts.selection_records
        ),
    )


def _observed_verdict(
    evidence: AttemptEvidence,
    facts: KaniLiveFacts,
) -> ProofVerdict:
    """Return the verdict the attempt's own log already reported, if any.

    A stop outranks the status, but not evidence the verifier already
    produced. Recording the verdict separately keeps a timeout from erasing
    the fact that the harness had verified before the supervisor ended it.
    """
    matching = [
        verdict for name, verdict in facts.verdicts if name == evidence.spec.harness
    ]
    if len(matching) != 1:
        return ProofVerdict.UNKNOWN
    return ProofVerdict.PASSED if matching[0] == "SUCCESSFUL" else ProofVerdict.FAILED


def _verifier_ran(observer: KaniObserver) -> bool:
    """Whether the attempt produced any evidence the verifier itself ran.

    A child that exits nonzero without printing a selection record, a verdict,
    or a completion line may never have reached the verifier. Calling that
    "no match" asserts a selection diagnosis the output does not support.
    """
    facts = observer.facts
    return bool(
        facts.selection_records
        or facts.verdicts
        or facts.completed
        or facts.no_match_reason is not None
    )


@dc.dataclass(frozen=True, slots=True)
class _Observed:
    """The evidence one attempt produced, independent of its status."""

    verdict: ProofVerdict = ProofVerdict.UNKNOWN
    progress: str | None = None
    identities: tuple[str, ...] = ()


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
    facts = observer.facts
    records = facts.selection_records
    expected = evidence.spec.harness
    matching = [name for name in records if name == expected]
    if not records:
        if facts.no_match_reason is not None:
            return _classification(
                ExecutionStatus.NO_MATCH,
                detail=facts.no_match_reason,
                observed=observed,
            )
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


def _classify_log(
    version: str,
    evidence: AttemptEvidence,
    observer: KaniObserver,
    observed: _Observed,
) -> HarnessClassification:
    """Classify the attempt from its parsed log."""
    log = observer.finish(evidence.exit_code or 0)
    if log is None:
        reason = observer.failure_reason or "unknown refusal"
        return _classification(
            ExecutionStatus.ERROR,
            detail=f"malformed verifier output: {reason}",
            observed=observed,
        )
    if len(log.blocks) != 1:
        return _classification(
            ExecutionStatus.ERROR,
            detail=(
                "the parsed log does not contain exactly one harness block: "
                f"{len(log.blocks)}"
            ),
            observed=observed,
        )
    (block,) = log.blocks
    verdict = dc.replace(observed, verdict=_proof_verdict(block))
    detail = _detail(evidence)
    if _is_failed(block):
        return _classification(ExecutionStatus.FAILED, detail=detail, observed=verdict)
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
