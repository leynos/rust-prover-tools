"""Prove the Verus log check holds a run to what a caller expected."""

from __future__ import annotations

import typing as typ
from pathlib import Path

from rust_prover_tools.verus_log import (
    SUPPORTED_VERUS_VERSIONS,
    LogExpectations,
    LogVerdict,
    VerusFailure,
    VerusLog,
    VerusSummary,
    check_verus_log,
    parse_expectation,
    parse_verus_log,
)

FIXTURES: typ.Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "verus_logs"
    / SUPPORTED_VERUS_VERSIONS[0]
)


def log_text(name: str) -> str:
    """Return one committed corpus log."""
    return (FIXTURES / f"{name}.log").read_text(encoding="utf-8")


def verdict(log: VerusLog, expected: LogExpectations) -> LogVerdict:
    """Check one log, so each test reads as the expectation it declares."""
    return check_verus_log(log, expected)


def test_matching_counts_and_a_clean_status_meet() -> None:
    """A declared clean run passes when every fact agrees."""
    log = VerusLog(VerusSummary(3, 0))

    result = verdict(log, LogExpectations(verified=3, errors=0, exit_status=0))

    assert result.meets
    assert result.disagreements == ()


def test_an_undeclared_count_is_not_checked() -> None:
    """A caller states only what it knows; the rest is not a failure."""
    log = VerusLog(VerusSummary(7, 0))

    assert verdict(log, LogExpectations(errors=0)).meets


def test_a_wrong_verified_count_is_reported() -> None:
    """A count that differs names what the log said and what was expected."""
    log = VerusLog(VerusSummary(2, 0))

    result = verdict(log, LogExpectations(verified=3))

    assert not result.meets
    assert result.disagreements == ("the log verified 2 functions, not the expected 3",)


def test_a_wrong_error_count_is_reported() -> None:
    """A failing-function count is held to the same standard."""
    log = VerusLog(VerusSummary(1, 2))

    result = verdict(log, LogExpectations(errors=1))

    assert not result.meets
    assert result.disagreements == (
        "the log reported 2 failing functions, not the expected 1",
    )


def test_an_expected_count_on_a_tool_error_is_reported() -> None:
    """A log that never reached a summary cannot satisfy a count."""
    result = verdict(
        VerusLog(),
        LogExpectations(verified=1, exit_status=1),
    )

    assert not result.meets
    assert result.disagreements == ("the log reached no verification result to count",)


def test_a_declared_region_is_met_by_the_single_failure_inside_it() -> None:
    """A negative control passes when its one failure landed where it should."""
    log = VerusLog(
        VerusSummary(1, 1),
        (VerusFailure("postcondition not satisfied", "mutant.rs", 8, 13),),
    )

    result = verdict(
        log,
        LogExpectations(
            errors=1,
            failure=parse_expectation("mutant.rs:5-12"),
            exit_status=1,
        ),
    )

    assert result.meets


def test_a_declared_region_is_not_met_by_a_failure_outside_it() -> None:
    """A failure somewhere else has not shown the fault would be caught."""
    log = VerusLog(
        VerusSummary(1, 1),
        (VerusFailure("postcondition not satisfied", "mutant.rs", 30, 13),),
    )

    result = verdict(
        log,
        LogExpectations(failure=parse_expectation("mutant.rs:5-12")),
    )

    assert not result.meets
    assert result.disagreements == (
        "no diagnostic fell inside the declared region mutant.rs:5-12",
    )


def test_a_declared_region_demands_exactly_one_failure() -> None:
    """A second diagnostic means the run broke more than the seeded fault."""
    log = VerusLog(
        VerusSummary(0, 2),
        (
            VerusFailure("postcondition not satisfied", "mutant.rs", 8, 13),
            VerusFailure("postcondition not satisfied", "mutant.rs", 20, 13),
        ),
    )

    result = verdict(
        log,
        LogExpectations(errors=2, failure=parse_expectation("mutant.rs:5-12")),
    )

    assert not result.meets
    assert any(
        "expects one diagnostic" in sentence for sentence in result.disagreements
    )
    assert any(
        "expects one failing function" in sentence for sentence in result.disagreements
    )


def test_a_declared_region_requires_a_summary_counting_one_error() -> None:
    """One matching diagnostic cannot make a two-error summary a control.

    A run that broke two functions while reporting one of them has not shown
    that the seeded fault alone was caught, even when the diagnostic that was
    captured lands in the right place.
    """
    log = VerusLog(
        VerusSummary(0, 2),
        (VerusFailure("postcondition not satisfied", "mutant.rs", 8, 13),),
    )

    result = verdict(
        log,
        LogExpectations(failure=parse_expectation("mutant.rs:5-12")),
    )

    assert not result.meets
    assert any(
        "but the summary counted 2" in sentence for sentence in result.disagreements
    )


def test_a_declared_region_requires_a_summary_at_all() -> None:
    """A compiler diagnostic is not a verification control.

    The diagnostic can name the right file and line while the run never
    reached a verification result, so the region alone cannot accept it.
    """
    log = VerusLog(
        None,
        (VerusFailure("mismatched types", "mutant.rs", 8, 13),),
    )

    result = verdict(
        log,
        LogExpectations(failure=parse_expectation("mutant.rs:5-12")),
    )

    assert not result.meets
    assert any(
        "requires a verification summary" in sentence
        for sentence in result.disagreements
    )


def test_an_unlocated_failure_does_not_satisfy_a_region() -> None:
    """A diagnostic with no span names no place the control can claim."""
    log = VerusLog(
        VerusSummary(0, 1),
        (VerusFailure("could not find function absent"),),
    )

    result = verdict(
        log,
        LogExpectations(failure=parse_expectation("mutant.rs")),
    )

    assert not result.meets
    assert "no diagnostic fell inside" in result.disagreements[0]


def test_a_clean_summary_beside_a_failing_status_is_refused() -> None:
    """Verus exits non-zero when it reports errors, so this capture lost facts."""
    result = verdict(
        VerusLog(VerusSummary(3, 0)),
        LogExpectations(exit_status=1),
    )

    assert not result.meets
    assert result.disagreements == (
        "the summary reported no errors, but the invocation exited 1",
    )


def test_a_failing_summary_beside_a_clean_status_is_refused() -> None:
    """A run that reported failures cannot have exited successfully."""
    result = verdict(
        VerusLog(VerusSummary(1, 1)),
        LogExpectations(exit_status=0),
    )

    assert not result.meets
    assert result.disagreements == (
        "the summary reported 1 failing functions, but the invocation exited 0",
    )


def test_an_undeclared_status_is_not_checked() -> None:
    """A capture through a wrapper may report the wrapper's own status."""
    assert verdict(VerusLog(VerusSummary(1, 1)), LogExpectations()).meets


def test_the_corpus_run_meets_its_recorded_counts() -> None:
    """A real capture is held to the facts the corpus records.

    `postcondition` is the corpus's single-failure run, which is the shape a
    seeded-mutant control has, so its one diagnostic is also held to the
    region the failure was expected in.
    """
    log = parse_verus_log(log_text("postcondition"))

    result = verdict(
        log,
        LogExpectations(
            verified=1,
            errors=1,
            failure=parse_expectation("postcondition.rs:8"),
            exit_status=1,
        ),
    )

    assert result.meets, result.disagreements


def test_a_signal_terminated_run_is_not_a_verification_result() -> None:
    """A negative exit status means the process was interrupted, not proved.

    The batch runner classifies a negative status as a signal; the check must
    not let it satisfy a failing summary's status comparison.
    """
    log = VerusLog(
        VerusSummary(1, 1),
        (VerusFailure("postcondition not satisfied", "mutant.rs", 8, 13),),
    )

    result = verdict(
        log,
        LogExpectations(
            errors=1,
            failure=parse_expectation("mutant.rs:5-12"),
            exit_status=-9,
        ),
    )

    assert not result.meets
    assert any(
        "terminated by signal 9" in sentence for sentence in result.disagreements
    )
