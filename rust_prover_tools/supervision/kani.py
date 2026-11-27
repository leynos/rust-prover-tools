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
from rust_prover_tools.kani_log import SUPPORTED_KANI_VERSIONS, KaniLog
from rust_prover_tools.kani_release import KANI_RELEASE_VERSION
from rust_prover_tools.supervision.kani_facts import KaniLiveFacts
from rust_prover_tools.supervision.kani_observer import (
    DEFAULT_MAX_LOG_BYTES,
    MAX_LIVE_RECORDS,
    KaniObservation,
    KaniObserver,
    SpoolFactory,
    TemporaryKaniSpool,
)
from rust_prover_tools.supervision.kani_outcome import (
    Observed,
    classification,
    classify_log,
    observe,
    proof_verdict,
)
from rust_prover_tools.supervision.models import (
    ExecutionStatus,
    HarnessSpec,
    ProofVerdict,
    StopReason,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.attempt_evidence import AttemptEvidence
    from rust_prover_tools.supervision.results import HarnessClassification

__all__ = (
    "DEFAULT_MAX_LOG_BYTES",
    "KaniAdapter",
    "KaniLiveFacts",
    "KaniObservation",
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
    spool_factory : SpoolFactory
        Opens the per-attempt spool the observer writes its log to.
    """

    version: str = KANI_RELEASE_VERSION
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    spool_factory: SpoolFactory = TemporaryKaniSpool

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
        return KaniObserver(
            max_log_bytes=self.max_log_bytes,
            spool_factory=self.spool_factory,
        )

    def classify(
        self,
        evidence: AttemptEvidence,
        observation: KaniObservation,
    ) -> HarnessClassification:
        """Classify one settled attempt under the documented precedence.

        The observation is the immutable result of the attempt's observer:
        this method only reads it, so it never parses the log or owns the
        spool.
        """
        observed = observe(evidence, observation.facts)
        if observation.facts_truncated or observation.log_truncated:
            # A dropped live record or a shortened log makes the retained
            # verdict ambiguous. This precedes the stop so a truncated stop
            # reports UNKNOWN as well.
            observed = dc.replace(observed, verdict=ProofVerdict.UNKNOWN)
        elif observation.log is not None and len(observation.log.blocks) == 1:
            # A stop outranks the status but not a verdict the retained log
            # states. `finish` parses before classification, so a stopped
            # attempt can carry one.
            observed = dc.replace(
                observed, verdict=proof_verdict(observation.log.blocks[0])
            )
        stopped = _stop_status(evidence, observed)
        if stopped is not None:
            return stopped
        refused = _refusal_status(evidence, observation, observed, self.version)
        if refused is not None:
            return refused
        log = observation.log
        if log is not None:
            # The parse is authoritative for selection and verdicts, so a
            # live verdict cannot survive a different parsed outcome.
            observed = dc.replace(
                observed,
                identities=tuple(
                    f"{evidence.spec.package}::{block.name}" for block in log.blocks
                ),
                verdict=ProofVerdict.UNKNOWN,
            )
        if log is None and observation.facts_truncated:
            return classification(
                ExecutionStatus.ERROR,
                detail="the verifier produced more selection records than "
                f"the supervisor retains ({MAX_LIVE_RECORDS})",
                observed=observed,
            )
        selection = _selection_status(evidence, observation.facts, observed, log)
        if selection is not None:
            return selection
        return classify_log(evidence, log, observed, observation.failure_reason)


def _refusal_status(
    evidence: AttemptEvidence,
    observation: KaniObservation,
    observed: Observed,
    version: str,
) -> HarnessClassification | None:
    """Return the classification a launch, spool, or banner refusal implies.

    These refusals precede the banner check in the documented precedence: a
    child that never launched has no log to check, and an attempt whose spool
    could not be opened has no log to retain, whatever its banner said.
    """
    if evidence.launch_error is not None:
        return classification(
            ExecutionStatus.ERROR,
            detail=f"launch failed: {evidence.launch_error}",
            observed=observed,
        )
    if observation.spool_error is not None:
        return classification(
            ExecutionStatus.ERROR,
            detail=f"cannot retain the verifier log: {observation.spool_error}",
            observed=observed,
        )
    banner = observation.banner_version
    if banner is not None and banner != version:
        return classification(
            ExecutionStatus.ERROR,
            detail=(
                f"the log names Kani {banner}, but this adapter is qualified "
                f"for {version}"
            ),
            observed=observed,
        )
    return None


def _verifier_ran(facts: KaniLiveFacts) -> bool:
    """Whether the attempt produced any evidence the verifier itself ran.

    A child that exits nonzero without printing a selection record, a verdict,
    or a completion line may never have reached the verifier. Calling that
    "no match" asserts a selection diagnosis the output does not support.
    """
    return bool(
        facts.selection_records
        or facts.verdicts
        or facts.completed
        or facts.no_match_reason is not None
    )


def _stop_status(
    evidence: AttemptEvidence,
    observed: Observed,
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
    return classification(status, detail=detail, observed=observed)


def _selection_status(
    evidence: AttemptEvidence,
    facts: KaniLiveFacts,
    observed: Observed,
    log: KaniLog | None,
) -> HarnessClassification | None:
    """Return the classification a selection mismatch implies, if any.

    The parsed log is authoritative when it exists: a description may print a
    ``Checking harness`` line as content, and only the parser tracks those
    frames. The live facts are the fallback for a log that could not be parsed.
    """
    records = (
        tuple(block.name for block in log.blocks)
        if log is not None and log.blocks
        else facts.selection_records
    )
    expected = evidence.spec.harness
    matching = [name for name in records if name == expected]
    if not records:
        return _no_selection_status(evidence, facts, observed, log)
    if len(records) > 1:
        return classification(
            ExecutionStatus.MULTIPLE_MATCH,
            detail=f"Kani selected {len(records)} harnesses: {list(records)}",
            observed=observed,
        )
    if matching != [expected]:
        return classification(
            ExecutionStatus.NO_MATCH,
            detail=f"Kani selected {records[0]!r}; requested {expected!r}",
            observed=observed,
        )
    return None


def _no_selection_status(
    evidence: AttemptEvidence,
    facts: KaniLiveFacts,
    observed: Observed,
    log: KaniLog | None,
) -> HarnessClassification:
    """Classify an attempt that produced no selection record."""
    if facts.no_match_reason is not None:
        return classification(
            ExecutionStatus.NO_MATCH,
            detail=facts.no_match_reason,
            observed=observed,
        )
    if log is None and not _verifier_ran(facts):
        # Nothing parsed and nothing live, so the selection is unknown. The
        # raw termination is the only evidence, and inventing a selection
        # diagnosis would name a cause the output never showed. Without a
        # code or a signal there is no termination evidence at all.
        if evidence.signal_number is None and evidence.exit_code in {None, 0}:
            return classification(
                ExecutionStatus.ERROR,
                detail=_missing_termination_detail(evidence),
                observed=observed,
            )
        return classification(
            ExecutionStatus.EXIT_NONZERO,
            detail=_no_output_detail(evidence),
            observed=observed,
        )
    return classification(
        ExecutionStatus.NO_MATCH,
        detail=f"Kani selected no harness; requested {evidence.spec.harness}",
        observed=observed,
    )


def _missing_termination_detail(evidence: AttemptEvidence) -> str:
    """Describe a child whose status and signal are both unknown."""
    if evidence.exit_code is None:
        return "the child reported neither an exit status nor a signal"
    return "child exited 0 with no verifier output"


def _no_output_detail(evidence: AttemptEvidence) -> str:
    """Describe a silent failure without claiming a cause it did not show."""
    if evidence.signal_number is not None:
        return f"child ended by signal {evidence.signal_number} with no output"
    return (
        f"child exited {evidence.exit_code} with no output; the verifier never "
        "reported a selection"
    )
