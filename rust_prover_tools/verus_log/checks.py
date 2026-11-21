"""Check one Verus run against what a caller expected it to do.

`rust_prover_tools.verus_log.decide` answers *what a log proves*.  This module
answers a narrower and more demanding question: *did this run do what it was
asked to?*  A qualification run supplies the counts and the place it expected a
failure, and the check reports whether the log agrees with every one of those
expectations rather than only whether the summary counted no errors.

The split matters because a seeded mutant run is expected to fail.  Reporting
its failure as an error would be correct about the proof and useless as a
qualification, so the check reads the expectation instead of the outcome.

Exit-status consistency is checked here too.  Verus exits non-zero when it
reports errors, so a log with a clean summary captured beside a non-zero status
is a capture that lost something, and a failing summary captured beside status
zero is a run the caller never saw fail.  Neither is a fact the summary alone
can reveal.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.verus_log.expectation import FailureExpectation
    from rust_prover_tools.verus_log.model import VerusLog

__all__ = ("LogExpectations", "LogVerdict", "check_verus_log")

#: The exit status Verus uses for a run that reported no errors.
CLEAN_STATUS = 0


@dc.dataclass(frozen=True, slots=True)
class LogExpectations:
    """What a caller expected one run to report.

    Every field is optional so a caller states only what it knows.  A count
    left ``None`` is not checked, which keeps a log from an unknown release
    usable while still letting a qualification run pin what it saw.

    Attributes
    ----------
    verified : int | None
        The number of functions the run was expected to verify.
    errors : int | None
        The number of failing functions the run was expected to report.
    failure : FailureExpectation | None
        Where the run's single failure was expected to land.  Supplying one
        makes the run a negative control: exactly one diagnostic must fall
        inside it.
    exit_status : int | None
        The status the invocation returned, for the consistency check.
    """

    verified: int | None = None
    errors: int | None = None
    failure: FailureExpectation | None = None
    exit_status: int | None = None


@dc.dataclass(frozen=True, slots=True)
class LogVerdict:
    """What one log said and whether it met every expectation.

    Attributes
    ----------
    meets : bool
        Whether every declared expectation held.
    disagreements : tuple[str, ...]
        One sentence per expectation that did not hold, in the order they were
        checked.  Empty when ``meets`` is true, because a verdict with no
        reason is only useful when there is nothing to explain.
    """

    meets: bool
    disagreements: tuple[str, ...] = ()


def check_verus_log(log: VerusLog, expected: LogExpectations) -> LogVerdict:
    """Hold one parsed log to the facts a caller expected it to report.

    Parameters
    ----------
    log : VerusLog
        Facts parsed from one captured invocation.
    expected : LogExpectations
        The counts, failure region and exit status the caller declared.

    Returns
    -------
    LogVerdict
        Whether every declared expectation held, with a sentence per one that
        did not.

    Examples
    --------
    A run that reported two verified and no errors, exiting zero, meets a
    clean expectation:

    >>> from rust_prover_tools.verus_log.model import VerusLog, VerusSummary
    >>> check_verus_log(VerusLog(VerusSummary(2, 0)), LogExpectations(verified=2)).meets
    True
    """
    disagreements = [
        *_count_disagreements(log, expected),
        *_failure_disagreements(log, expected),
        *_status_disagreements(log, expected),
    ]
    return LogVerdict(not disagreements, tuple(disagreements))


def _count_disagreements(
    log: VerusLog,
    expected: LogExpectations,
) -> cabc.Iterator[str]:
    """Yield one sentence per summary count that differs from the expectation."""
    summary = log.summary
    if summary is None:
        if expected.verified is not None or expected.errors is not None:
            yield "the log reached no verification result to count"
        return
    if expected.verified is not None and summary.verified != expected.verified:
        yield (
            f"the log verified {summary.verified} functions, "
            f"not the expected {expected.verified}"
        )
    if expected.errors is not None and summary.errors != expected.errors:
        yield (
            f"the log reported {summary.errors} failing functions, "
            f"not the expected {expected.errors}"
        )


def _failure_disagreements(
    log: VerusLog,
    expected: LogExpectations,
) -> cabc.Iterator[str]:
    """Yield a sentence when the declared negative control was not met.

    A declared region demands a summary counting exactly one failing function
    *and* exactly one diagnostic, because either fact alone can mislead: a
    capture can lose a diagnostic, and a run can break two functions while
    reporting one of them.  Checking both is what keeps a matching compiler
    diagnostic, which has no summary beside it, from passing as a control.
    """
    region = expected.failure
    if region is None:
        return
    if log.summary is None:
        yield (
            "the declared failure region requires a verification summary, "
            "but the log reached none"
        )
    elif log.summary.errors != 1:
        yield (
            "the declared failure region expects one failing function, "
            f"but the summary counted {log.summary.errors}"
        )
    if len(log.failures) != 1:
        yield (
            f"the declared failure region expects one diagnostic, "
            f"but the log reported {len(log.failures)}"
        )
    located = [item for item in log.failures if region.contains(item)]
    if not located:
        yield f"no diagnostic fell inside the declared region {region.describe()}"


def _status_disagreements(
    log: VerusLog,
    expected: LogExpectations,
) -> cabc.Iterator[str]:
    """Yield a sentence when the exit status and the summary disagree.

    A status the caller did not declare is not checked, because a capture may
    have been taken through a wrapper that reports its own status.  When one is
    declared, the two facts must tell the same story: Verus exits non-zero
    exactly when it reports errors.
    """
    if expected.exit_status is None or log.summary is None:
        return
    if expected.exit_status < 0:
        yield (
            f"the invocation was terminated by signal {-expected.exit_status}; "
            "a run that did not finish is not a verification result"
        )
        return
    reported_failure = log.summary.errors > 0
    exited_failing = expected.exit_status != CLEAN_STATUS
    if reported_failure and not exited_failing:
        yield (
            f"the summary reported {log.summary.errors} failing functions, "
            f"but the invocation exited {expected.exit_status}"
        )
    if not reported_failure and exited_failing:
        yield (
            "the summary reported no errors, "
            f"but the invocation exited {expected.exit_status}"
        )
