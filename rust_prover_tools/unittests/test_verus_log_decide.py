"""Prove the Verus decision matches the corpus and the negative-control rule."""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rust_prover_tools.cli_support import WINDOWS_PATHS
from rust_prover_tools.verus_log import (
    FULL_RUN,
    PARTIAL_RUN,
    SUPPORTED_VERUS_VERSIONS,
    FailureExpectation,
    VerusAnalysis,
    VerusFailure,
    VerusLog,
    VerusLogError,
    VerusOutcome,
    VerusSummary,
    analyse_verus_log,
    from_verus_log_error,
    parse_expectation,
    parse_verus_log,
)
from tests import verus_log_corpus as corpus

CLEAN = VerusLog(VerusSummary(2, 0))
PARTIAL_CLEAN = VerusLog(VerusSummary(1, 0, is_partial=True))
FAILED = VerusLog(
    VerusSummary(1, 1),
    (VerusFailure("postcondition not satisfied", "proofs/mutant.rs", 8, 13),),
)


DECISION_CASES: typ.Final[tuple[tuple[str, str], ...]] = tuple(
    (version, name)
    for version in SUPPORTED_VERUS_VERSIONS
    for name in sorted(corpus.expected_for(version)["logs"])
)


@pytest.mark.parametrize(("version", "name"), DECISION_CASES)
def test_every_committed_log_decides_as_recorded(version: str, name: str) -> None:
    """Each corpus log decides as its own release records."""
    recorded = corpus.expected_for(version)["logs"][name]
    log = parse_verus_log(
        (corpus.CORPUS_ROOT / version / f"{name}.log").read_text(encoding="utf-8"),
    )
    analysis = analyse_verus_log(log)

    assert analysis.outcome.value == recorded["outcome"]
    assert analysis.run_mode == recorded["run_mode"]
    assert analysis.passed is recorded["passed"]


def test_a_clean_full_run_is_verified() -> None:
    """Report verified, the full run mode and a reason naming the count."""
    analysis = analyse_verus_log(CLEAN)

    assert analysis.outcome is VerusOutcome.VERIFIED
    assert analysis.run_mode == FULL_RUN
    assert analysis.reason == "the log reports 2 verified and no errors"
    assert analysis.passed is True


def test_a_clean_selected_run_is_verified_but_partial() -> None:
    """A selected run passes while reporting that it proved only a selection."""
    analysis = analyse_verus_log(PARTIAL_CLEAN)

    assert analysis.outcome is VerusOutcome.VERIFIED
    assert analysis.run_mode == PARTIAL_RUN
    assert "selected run" in analysis.reason
    assert analysis.passed is True


def test_errors_are_reported_with_every_failure() -> None:
    """A failing run reports the count and carries each diagnostic through."""
    analysis = analyse_verus_log(FAILED)

    assert analysis.outcome is VerusOutcome.ERRORS
    assert analysis.reason == "the log reports 1 failing function"
    assert analysis.failures == FAILED.failures
    assert analysis.passed is False


def test_a_log_without_a_summary_is_a_tool_error() -> None:
    """A run that never printed results proved nothing either way."""
    analysis = analyse_verus_log(VerusLog())

    assert analysis.outcome is VerusOutcome.TOOL_ERROR
    assert analysis.run_mode is None
    assert analysis.reason == "the log records no verification results"
    assert analysis.passed is False


def test_a_tool_error_reason_names_the_first_diagnostic() -> None:
    """The first diagnostic explains why no summary was reached."""
    log = VerusLog(
        failures=(VerusFailure("mismatched types", "src/lib.rs", 9, 21),),
    )

    analysis = analyse_verus_log(log)

    assert analysis.outcome is VerusOutcome.TOOL_ERROR
    assert analysis.failures == log.failures
    assert analysis.reason == (
        "the log records no verification results: mismatched types at src/lib.rs:9:21"
    )


def test_a_structural_error_decides_as_a_tool_error() -> None:
    """A log the parser refused supports only a tool-error analysis."""
    analysis = from_verus_log_error(
        VerusLogError("the log carries two verification results summaries"),
    )

    assert analysis.outcome is VerusOutcome.TOOL_ERROR
    assert analysis.run_mode is None
    assert analysis.passed is False
    assert analysis.reason == "the log carries two verification results summaries"


def test_a_negative_control_passes_only_where_it_was_meant_to_fail() -> None:
    """The declared region turns a failing run into a passing control."""
    expectation = FailureExpectation("proofs/mutant.rs", 5, 12)

    analysis = analyse_verus_log(FAILED, expectation=expectation)

    assert analysis.outcome is VerusOutcome.ERRORS
    assert analysis.expectation_met is True
    assert analysis.passed is True


def test_a_negative_control_failing_elsewhere_does_not_pass() -> None:
    """A failure outside the declared region shows nothing about the fault."""
    expectation = FailureExpectation("proofs/other.rs")

    analysis = analyse_verus_log(FAILED, expectation=expectation)

    assert analysis.expectation_met is False
    assert analysis.passed is False


def test_a_negative_control_must_see_exactly_one_failure() -> None:
    """Two located failures mean the run broke more than the seeded fault.

    The summary counts one failing function here, so only the diagnostic-count
    rule can reject the control; the separate summary-count test below covers
    the other direction.
    """
    log = VerusLog(
        VerusSummary(0, 1),
        (
            VerusFailure("postcondition not satisfied", "proofs/mutant.rs", 8, 13),
            VerusFailure("assertion failed", "proofs/mutant.rs", 20, 5),
        ),
    )
    expectation = FailureExpectation("proofs/mutant.rs", 5, 12)

    analysis = analyse_verus_log(log, expectation=expectation)

    assert analysis.expectation_met is False
    assert analysis.passed is False


def test_a_negative_control_needs_the_summary_to_count_one_failure() -> None:
    """A summary counting two failures fails the control with one diagnostic.

    The capture kept only the diagnostic it was given, but the summary states
    that two functions failed, so the run broke more than the seeded fault.
    Reading only the diagnostics would pass a control that proved nothing about
    isolation.
    """
    log = VerusLog(
        VerusSummary(0, 2),
        (VerusFailure("postcondition not satisfied", "proofs/mutant.rs", 8, 13),),
    )

    analysis = analyse_verus_log(
        log,
        expectation=FailureExpectation("proofs/mutant.rs"),
    )

    assert analysis.reported_errors == 2
    assert analysis.expectation_met is False
    assert analysis.passed is False


@pytest.mark.parametrize("reported_errors", [1, 2])
def test_an_unlocated_failure_beside_the_seeded_one_fails_the_control(
    reported_errors: int,
) -> None:
    """A run that also broke somewhere else has not isolated the seeded fault.

    The extra diagnostic names no source position, so it cannot be placed. The
    control reads the complete failure collection rather than the located
    subset, which would otherwise see exactly one failure and pass. The summary
    count is varied because a count other than one is refused before the
    collection is consulted, so only the count of one exercises the guard that
    reads it.
    """
    log = VerusLog(
        VerusSummary(0, reported_errors),
        (
            VerusFailure("postcondition not satisfied", "proofs/mutant.rs", 8, 13),
            VerusFailure("could not find function absent"),
        ),
    )

    analysis = analyse_verus_log(
        log,
        expectation=FailureExpectation("proofs/mutant.rs"),
    )

    assert analysis.expectation_met is False
    assert analysis.passed is False


def test_a_verified_run_never_meets_a_failure_expectation() -> None:
    """A control that did not fail has not shown that the fault is caught."""
    analysis = analyse_verus_log(CLEAN, expectation=FailureExpectation("any.rs"))

    assert analysis.outcome is VerusOutcome.VERIFIED
    assert analysis.expectation_met is False
    assert analysis.passed is False


def test_only_located_failures_can_meet_an_expectation() -> None:
    """An unlocated usage error cannot stand in for a seeded fault."""
    log = VerusLog(
        VerusSummary(0, 1),
        (VerusFailure("could not find function absent"),),
    )

    analysis = analyse_verus_log(
        log,
        expectation=FailureExpectation("proofs/mutant.rs"),
    )

    assert analysis.located_failures == ()
    assert analysis.passed is False


def test_an_analysis_without_an_expectation_is_never_met() -> None:
    """Nothing to meet is not the same as met."""
    assert analyse_verus_log(CLEAN).expectation_met is False


def test_parse_expectation_reads_a_line_range() -> None:
    """A trailing range is read as one, not as part of the path."""
    expectation = parse_expectation("proofs/mutant.rs:12-18")

    assert expectation == FailureExpectation("proofs/mutant.rs", 12, 18)
    assert expectation.describe() == "proofs/mutant.rs:12-18"


def test_parse_expectation_treats_a_single_line_as_one_line() -> None:
    """``path:line`` accepts that line only and is echoed unchanged."""
    expectation = parse_expectation("proofs/mutant.rs:12")

    assert expectation.describe() == "proofs/mutant.rs:12"
    assert expectation.contains(VerusFailure("failed", "proofs/mutant.rs", 12, 1))
    assert not expectation.contains(
        VerusFailure("failed", "proofs/mutant.rs", 11, 1),
    )
    assert not expectation.contains(
        VerusFailure("failed", "proofs/mutant.rs", 13, 1),
    )


def test_parse_expectation_keeps_a_bare_path() -> None:
    """A path with no range accepts any line in that file."""
    expectation = parse_expectation(" proofs/mutant.rs ")

    assert expectation == FailureExpectation("proofs/mutant.rs")
    assert expectation.describe() == "proofs/mutant.rs"


@pytest.mark.parametrize(
    "text",
    ["", "   ", "proofs/mutant.rs:0", "proofs/mutant.rs:12-4"],
)
def test_parse_expectation_refuses_an_unusable_region(text: str) -> None:
    """An empty or impossible region is rejected where it is typed."""
    with pytest.raises(ValueError, match="failure expectation"):
        parse_expectation(text)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (VerusFailure("failed", "proofs/mutant.rs", 8, 13), True),
        (VerusFailure("failed", "./proofs/mutant.rs", 8, 13), True),
        (VerusFailure("failed", "proofs/./mutant.rs", 8, 13), True),
        # On POSIX a backslash is an ordinary filename character, not a
        # separator, so this names a different file from the expectation.  On
        # Windows the two spellings are the same file, which the dedicated
        # Windows test covers.
        pytest.param(
            VerusFailure("failed", "proofs\\mutant.rs", 8, 13),
            False,
            marks=pytest.mark.skipif(
                WINDOWS_PATHS,
                reason="a Windows backslash is a separator",
            ),
        ),
        # A ``..`` step is kept: through a symbolic link it can name another
        # file, so only the exact path may satisfy the expectation.
        (VerusFailure("failed", "proofs/../proofs/mutant.rs", 8, 13), False),
        (VerusFailure("failed", "proofs/mutant.rs", 4, 13), False),
        (VerusFailure("failed", "proofs/mutant.rs", 13, 13), False),
        (VerusFailure("failed", "proofs/other.rs", 8, 13), False),
        (VerusFailure("failed"), False),
    ],
)
def test_expectation_containment_compares_normalized_paths(
    failure: VerusFailure,
    *,
    expected: bool,
) -> None:
    """Spellings that cannot change the file match; a ``..`` step does not."""
    expectation = FailureExpectation("proofs/mutant.rs", 5, 12)

    assert expectation.contains(failure) is expected


def test_a_windows_capture_treats_either_separator_as_the_same_file() -> None:
    """A Windows flavour makes both separators name the same file."""
    subject = FailureExpectation("proofs/mutant.rs", 5, 12, windows_paths=True)

    assert subject.contains(VerusFailure("failed", "proofs\\mutant.rs", 8, 13))


@given(
    line=st.integers(min_value=1, max_value=500),
    start=st.integers(min_value=1, max_value=500),
    width=st.integers(min_value=0, max_value=50),
)
def test_a_range_contains_exactly_the_lines_between_its_ends(
    line: int,
    start: int,
    width: int,
) -> None:
    """Containment is the inclusive interval its two ends describe."""
    expectation = FailureExpectation("proofs/mutant.rs", start, start + width)
    failure = VerusFailure("failed", "proofs/mutant.rs", line, 1)

    assert expectation.contains(failure) is (start <= line <= start + width)


def test_summary_total_counts_verified_and_failed() -> None:
    """The total is what the run accounted for."""
    assert VerusSummary(3, 2).total == 5


@pytest.mark.parametrize(
    ("failure", "where"),
    [
        (VerusFailure("failed", "src/lib.rs", 9, 21), "src/lib.rs:9:21"),
        (VerusFailure("failed", "src/lib.rs", 9), "src/lib.rs:9"),
        (VerusFailure("failed"), ""),
        (VerusFailure("failed", "src/lib.rs"), ""),
    ],
)
def test_failure_where_renders_the_span_rustc_printed(
    failure: VerusFailure,
    where: str,
) -> None:
    """An incomplete span renders as empty rather than as a partial position."""
    assert failure.where == where
    assert failure.is_located is bool(where)


def test_an_analysis_defaults_to_carrying_nothing() -> None:
    """A hand-built analysis has an empty reason and no failures."""
    analysis = VerusAnalysis(VerusOutcome.VERIFIED)

    assert analysis.failures == ()
    assert analysis.reason == ""
    assert analysis.run_mode is None
