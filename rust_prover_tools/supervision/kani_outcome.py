"""Outcome construction for one Kani attempt.

The adapter decides which outcome applies; this module builds that outcome's
classification from the evidence it retained. Keeping it apart lets the
adapter read as the documented precedence and keeps each module small.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log import ASSERTION_KIND, HarnessBlock, KaniLog
from rust_prover_tools.supervision.models import (
    ExecutionStatus,
    ProofVerdict,
)
from rust_prover_tools.supervision.results import HarnessClassification

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.attempt_evidence import AttemptEvidence
    from rust_prover_tools.supervision.kani_observer import KaniLiveFacts

__all__ = (
    "Observed",
    "classification",
    "classify_log",
    "observe",
    "observed_verdict",
)


@dc.dataclass(frozen=True, slots=True)
class Observed:
    """The evidence one attempt produced, independent of its status."""

    verdict: ProofVerdict = ProofVerdict.UNKNOWN
    progress: str | None = None
    identities: tuple[str, ...] = ()


def observe(evidence: AttemptEvidence, facts: KaniLiveFacts) -> Observed:
    """Return every fact the attempt produced, independent of its status."""
    return Observed(
        verdict=observed_verdict(evidence, facts),
        progress=facts.last_progress,
        identities=tuple(
            f"{evidence.spec.package}::{name}" for name in facts.selection_records
        ),
    )


def observed_verdict(
    evidence: AttemptEvidence,
    facts: KaniLiveFacts,
) -> ProofVerdict:
    """Return the verdict the attempt's own log already reported, if any.

    A stop outranks the status, but not evidence the verifier already
    produced. Recording the verdict separately keeps a timeout from erasing
    the fact that the harness had verified before the supervisor ended it.
    """
    selected = [
        name for name in facts.selection_records if name == evidence.spec.harness
    ]
    matching = [
        verdict for name, verdict in facts.verdicts if name == evidence.spec.harness
    ]
    if len(selected) != 1 or len(matching) != 1:
        # Two targets can print one pretty name; with more than one matching
        # selection there is no single attempt to attribute the verdict to.
        return ProofVerdict.UNKNOWN
    return ProofVerdict.PASSED if matching[0] == "SUCCESSFUL" else ProofVerdict.FAILED


def proof_verdict(block: HarnessBlock) -> ProofVerdict:
    """Return the verifier verdict the block states."""
    if block.succeeded:
        return ProofVerdict.PASSED
    if block.verdict == "FAILED":
        return ProofVerdict.FAILED
    return ProofVerdict.UNKNOWN


def is_failed(block: HarnessBlock) -> bool:
    """Whether the block reports a failure that is not an expected panic."""
    if block.verdict == "FAILED":
        return True
    failed = [item for item in block.checks if item.status == "FAILURE"]
    if not failed or not block.panics_as_expected:
        return bool(failed)
    return any(item.kind != ASSERTION_KIND for item in failed)


def termination_status(
    evidence: AttemptEvidence,
    detail: str,
) -> tuple[ExecutionStatus, str] | None:
    """Return the termination classification after a passing log, if any."""
    if evidence.signal_number is not None:
        return ExecutionStatus.EXIT_NONZERO, detail
    if evidence.exit_code is None:
        return ExecutionStatus.ERROR, "no exit status"
    if evidence.exit_code != 0:
        return ExecutionStatus.EXIT_NONZERO, detail
    return None


def termination_detail_text(evidence: AttemptEvidence) -> str:
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


def classification(
    status: ExecutionStatus,
    *,
    detail: str,
    observed: Observed | None = None,
) -> HarnessClassification:
    """Build one adapter classification."""
    facts = observed or Observed()
    return HarnessClassification(
        status=status,
        proof_verdict=facts.verdict,
        detail=detail,
        last_progress=facts.progress,
        observed=facts.identities,
    )


def classify_log(
    evidence: AttemptEvidence,
    log: KaniLog | None,
    observed: Observed,
    failure_reason: str | None = None,
) -> HarnessClassification:
    """Classify the attempt from its parsed log."""
    if log is None:
        reason = failure_reason or "unknown refusal"
        return classification(
            ExecutionStatus.ERROR,
            detail=f"malformed verifier output: {reason}",
            observed=observed,
        )
    if len(log.blocks) != 1:
        return classification(
            ExecutionStatus.ERROR,
            detail=(
                "the parsed log does not contain exactly one harness block: "
                f"{len(log.blocks)}"
            ),
            observed=observed,
        )
    (block,) = log.blocks
    verdict = dc.replace(observed, verdict=proof_verdict(block))
    termination_detail = termination_detail_text(evidence)
    if is_failed(block):
        return classification(
            ExecutionStatus.FAILED, detail=termination_detail, observed=verdict
        )
    termination = termination_status(evidence, termination_detail)
    if termination is not None:
        return classification(termination[0], detail=termination[1], observed=verdict)
    if block.succeeded:
        return classification(
            ExecutionStatus.PASSED,
            detail=termination_detail,
            observed=dc.replace(verdict, verdict=ProofVerdict.PASSED),
        )
    return classification(
        ExecutionStatus.ERROR,
        detail=f"{termination_detail}; verifier reported no verdict",
        observed=dc.replace(verdict, verdict=ProofVerdict.UNKNOWN),
    )
