"""Decide what one captured Verus run proves.

The parser says what the log reported; this module says what that means.  Three
facts carry the decision: whether Verus printed a summary at all, how many
functions failed, and whether the run selected part of the crate.

A run with no summary never verified anything - a compiler error, a rejected
option, or a killed process all look the same from the outside - so it is a
tool error rather than a pass or a failure.  A run that selected part of the
crate proved what it selected and nothing else, which is why the run mode is
reported beside the outcome instead of being folded into it.

A declared failure expectation turns the analysis into a negative control: the
run is expected to fail, and it passes only when the single reported failure
lands where the expectation says it should.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

from rust_prover_tools.verus_log.syntax import FULL_RUN, PARTIAL_RUN, VerusLogError

if typ.TYPE_CHECKING:
    from rust_prover_tools.verus_log.expectation import FailureExpectation
    from rust_prover_tools.verus_log.model import VerusFailure, VerusLog

__all__ = (
    "FULL_RUN",
    "PARTIAL_RUN",
    "VerusAnalysis",
    "VerusOutcome",
    "analyse_verus_log",
    "from_verus_log_error",
)


class VerusOutcome(enum.StrEnum):
    """The strongest fact one Verus run's log supports."""

    VERIFIED = "verified"
    ERRORS = "errors"
    TOOL_ERROR = "tool-error"


@dc.dataclass(frozen=True, slots=True)
class VerusAnalysis:
    """The outcome of one decision over a captured Verus run."""

    outcome: VerusOutcome
    run_mode: str | None = None
    failures: tuple[VerusFailure, ...] = ()
    reason: str = ""
    expectation: FailureExpectation | None = None
    #: The number of failing functions the summary reported.  The diagnostics
    #: are evidence of *where* a function failed, not of how many did: a
    #: capture can lose one, and a diagnostic the parser could not place still
    #: counts as a failure.
    reported_errors: int = 0

    @property
    def located_failures(self) -> tuple[VerusFailure, ...]:
        """The failures that name a source position."""
        return tuple(item for item in self.failures if item.is_located)

    @property
    def expectation_met(self) -> bool:
        """Report whether the run failed exactly where it was meant to.

        The summary must count exactly one failing function *and* the parser
        must have found exactly one diagnostic, inside the expected region.
        Both are required because each can be right while the other is
        misleading: a capture that lost a diagnostic would otherwise pass on
        the strength of the one it kept, and a run that broke in two functions
        while reporting one of them is not the single seeded failure the
        control exists to show.  Checking the complete collection rather than
        only the located failures keeps an unlocated diagnostic from hiding
        beside the seeded fault.  An expectation that was never declared is
        never met.
        """
        if self.expectation is None:
            return False
        if self.reported_errors != 1 or len(self.failures) != 1:
            return False
        return self.expectation.contains(self.failures[0])

    @property
    def passed(self) -> bool:
        """Report whether the run did what it was asked to do.

        A plain run passes when Verus reported no errors; a negative control
        passes when its failure landed in the declared region.  A partial run
        can pass: it proved what it was asked to prove, and whether that is
        enough is the caller's decision, taken with :attr:`run_mode`.
        """
        if self.expectation is not None:
            return self.expectation_met
        return self.outcome is VerusOutcome.VERIFIED


def analyse_verus_log(
    log: VerusLog,
    *,
    expectation: FailureExpectation | None = None,
) -> VerusAnalysis:
    """Decide what one parsed Verus run proves.

    Parameters
    ----------
    log : VerusLog
        Facts parsed from one captured invocation.
    expectation : FailureExpectation | None
        Where a negative-control run was required to fail, if anywhere.

    Returns
    -------
    VerusAnalysis
        The outcome, the run mode, every failure the log reported and a short
        reason.

    Examples
    --------
    >>> from rust_prover_tools.verus_log.model import VerusLog, VerusSummary
    >>> analyse_verus_log(VerusLog(VerusSummary(3, 0))).passed
    True
    """
    summary = log.summary
    if summary is None:
        return _tool_error(log)
    run_mode = PARTIAL_RUN if summary.is_partial else FULL_RUN
    if summary.errors > 0:
        # The reason is printed on the text report and carried in the JSON
        # payload, so one failure has to read as one function rather than as
        # the count followed by a fixed plural.
        noun = "function" if summary.errors == 1 else "functions"
        reason = f"the log reports {summary.errors} failing {noun}"
        return VerusAnalysis(
            VerusOutcome.ERRORS,
            run_mode,
            log.failures,
            reason,
            expectation,
            summary.errors,
        )
    return VerusAnalysis(
        VerusOutcome.VERIFIED,
        run_mode,
        log.failures,
        _verified_reason(run_mode, summary.verified),
        expectation,
        summary.errors,
    )


def from_verus_log_error(error: VerusLogError) -> VerusAnalysis:
    """Return the analysis a structurally invalid log supports."""
    return VerusAnalysis(VerusOutcome.TOOL_ERROR, reason=error.reason)


def _verified_reason(run_mode: str, verified: int) -> str:
    """Describe a run that reported no errors."""
    if run_mode == PARTIAL_RUN:
        return f"the log reports {verified} verified and no errors in a selected run"
    return f"the log reports {verified} verified and no errors"


def _tool_error(log: VerusLog) -> VerusAnalysis:
    """Build the analysis for a log that never reached a verification result."""
    if not log.failures:
        return VerusAnalysis(
            VerusOutcome.TOOL_ERROR,
            reason="the log records no verification results",
        )
    first = log.failures[0]
    where = first.where
    located = f" at {where}" if where else ""
    reason = f"the log records no verification results: {first.message}{located}"
    return VerusAnalysis(VerusOutcome.TOOL_ERROR, failures=log.failures, reason=reason)
