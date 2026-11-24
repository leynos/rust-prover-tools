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
from rust_prover_tools.supervision.kani_observer import (
    DEFAULT_MAX_LOG_BYTES,
    MAX_LIVE_RECORDS,
    KaniLiveFacts,
    KaniObserver,
)
from rust_prover_tools.supervision.kani_outcome import (
    Observed,
    classification,
    classify_log,
    observe,
)
from rust_prover_tools.supervision.models import (
    ExecutionStatus,
    HarnessSpec,
    StopReason,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.attempt_state import AttemptEvidence
    from rust_prover_tools.supervision.protocols import LineObserver
    from rust_prover_tools.supervision.results import HarnessClassification

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
        observed = observe(evidence, kani.facts)
        try:
            stopped = _stop_status(evidence, kani, observed)
            if stopped is not None:
                return stopped
            if evidence.launch_error is not None:
                return classification(
                    ExecutionStatus.ERROR,
                    detail=f"launch failed: {evidence.launch_error}",
                    observed=observed,
                )
            if kani.facts_truncated:
                return classification(
                    ExecutionStatus.ERROR,
                    detail="the verifier produced more selection records than "
                    f"the supervisor retains ({MAX_LIVE_RECORDS})",
                    observed=observed,
                )
            if kani.banner_version is not None and kani.banner_version != self.version:
                return classification(
                    ExecutionStatus.ERROR,
                    detail=(
                        f"the log names Kani {kani.banner_version}, but this "
                        f"adapter is qualified for {self.version}"
                    ),
                    observed=observed,
                )
            log = kani.finish(evidence.exit_code or 0)
            if log is not None:
                observed = dc.replace(
                    observed,
                    identities=tuple(
                        f"{evidence.spec.package}::{block.name}" for block in log.blocks
                    ),
                )
            selection = _selection_status(evidence, kani, observed, log)
            if selection is not None:
                return selection
            return classify_log(evidence, log, observed, kani.failure_reason)
        finally:
            kani.close()


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


def _stop_status(
    evidence: AttemptEvidence,
    observer: KaniObserver,
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
    del observer
    return classification(status, detail=detail, observed=observed)


def _selection_status(
    evidence: AttemptEvidence,
    observer: KaniObserver,
    observed: Observed,
    log: KaniLog | None,
) -> HarnessClassification | None:
    """Return the classification a selection mismatch implies, if any.

    The parsed log is authoritative when it exists: a description may print a
    ``Checking harness`` line as content, and only the parser tracks those
    frames. The live facts are the fallback for a log that could not be parsed.
    """
    facts = observer.facts
    records = (
        tuple(block.name for block in log.blocks)
        if log is not None and log.blocks
        else facts.selection_records
    )
    expected = evidence.spec.harness
    matching = [name for name in records if name == expected]
    if not records:
        return _no_selection_status(evidence, observer, observed, log)
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
    observer: KaniObserver,
    observed: Observed,
    log: KaniLog | None,
) -> HarnessClassification:
    """Classify an attempt that produced no selection record."""
    facts = observer.facts
    if facts.no_match_reason is not None:
        return classification(
            ExecutionStatus.NO_MATCH,
            detail=facts.no_match_reason,
            observed=observed,
        )
    if log is None and not _verifier_ran(observer):
        # Nothing parsed and nothing live, so the selection is unknown. The
        # raw termination is the only evidence, and inventing a selection
        # diagnosis would name a cause the output never showed.
        if evidence.exit_code == 0 and evidence.signal_number is None:
            return classification(
                ExecutionStatus.ERROR,
                detail="child exited 0 with no verifier output",
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


def _no_output_detail(evidence: AttemptEvidence) -> str:
    """Describe a silent failure without claiming a cause it did not show."""
    if evidence.signal_number is not None:
        return f"child ended by signal {evidence.signal_number} with no output"
    return (
        f"child exited {evidence.exit_code} with no output; the verifier never "
        "reported a selection"
    )
