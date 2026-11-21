"""Refuse every way a Kani log can look complete without being trustworthy.

Each case starts from a real capture and changes one thing: it cuts the log
short, deletes a report line, raises a count, or splices one block into
another. Every edit must be refused for a reason that names the construct the
grammar could not account for, because every caller collapses the refusal to
one reason code and the sentence is the only part an author can act on.
"""

from __future__ import annotations

import pytest

from rust_prover_tools.kani_log import parse
from rust_prover_tools.unittests.kani_log_support import (
    COMPLETE,
    HARNESS_START,
    MANUAL,
    RESULTS,
    SUMMARY,
    TIME,
    VERIFICATION,
    lines_of,
    refusal_of,
)


def test_a_block_cut_before_its_verification_time_is_truncated() -> None:
    """Require the truncation to name the harness whose block never closed."""
    lines = lines_of("verified")
    end = next(index for index, line in enumerate(lines) if line.startswith(TIME))
    assert "truncated inside the block for" in refusal_of(lines[:end])


def test_a_summary_without_a_count_line_has_no_summary() -> None:
    """Require a block that lists checks but no count to be refused."""
    lines = lines_of("verified")
    index = next(index for index, line in enumerate(lines) if line == SUMMARY)
    end = next(
        position
        for position in range(index, len(lines))
        if lines[position].startswith(" ** ")
    )
    assert "has no summary" in refusal_of(lines[:end] + lines[end + 1 :])


def test_a_second_summary_count_is_refused_by_name() -> None:
    """Refuse two failure counts in one block, which the format prints once."""
    lines = lines_of("verified")
    index = next(index for index, line in enumerate(lines) if line.startswith(" ** "))
    lines.insert(index + 1, lines[index])
    assert "has two failure counts" in refusal_of(lines)


def test_a_second_cover_count_is_refused_by_name() -> None:
    """Refuse two cover counts in one block."""
    lines = lines_of("verified")
    index = next(
        index
        for index, line in enumerate(lines)
        if "cover properties satisfied" in line
    )
    lines.insert(index + 1, lines[index])
    assert "has two cover counts" in refusal_of(lines)


def test_a_second_verification_result_is_refused() -> None:
    """Refuse a block that states its verdict twice."""
    lines = lines_of("verified")
    index = next(
        index for index, line in enumerate(lines) if line.startswith(VERIFICATION)
    )
    lines.insert(index + 1, lines[index])
    assert "reports two verification results" in refusal_of(lines)


def test_a_failure_without_its_source_location_is_refused() -> None:
    """Require the `` File:`` line, which carries the fact a record cannot invent."""
    lines = lines_of("counterexample")
    index = next(
        index for index, line in enumerate(lines) if line.startswith(" File: ")
    )
    assert "without a source location" in refusal_of(lines[:index] + lines[index + 1 :])


def test_a_check_listed_that_the_count_denies_is_refused() -> None:
    """Require the count and the listed checks to agree in both directions."""
    lines = lines_of("verified")
    start = next(index for index, line in enumerate(lines) if line.startswith("Check "))
    end = start + 1
    while end < len(lines) and lines[end].startswith("\t - "):
        end += 1
    assert "counts 2 checks but lists 1" in refusal_of(lines[:start] + lines[end:])


def test_a_check_missing_a_field_is_refused() -> None:
    """Refuse a ``Check`` entry whose location field was dropped."""
    lines = lines_of("verified")
    index = next(
        index for index, line in enumerate(lines) if line.startswith("\t - Location:")
    )
    assert "is missing Location" in refusal_of(lines[:index] + lines[index + 1 :])


def test_a_check_with_an_unknown_status_is_refused() -> None:
    """Refuse a status outside the closed vocabulary."""
    lines = [
        line.replace("Status: SUCCESS", "Status: PROBABLY")
        for line in lines_of("verified")
    ]
    assert "has unknown status 'PROBABLY'" in refusal_of(lines)


def test_a_log_without_a_manual_summary_is_truncated() -> None:
    """Require the run's final count line, which makes a cut tail detectable."""
    lines = [line for line in lines_of("verified") if not line.startswith(MANUAL)]
    assert "has no manual harness summary" in refusal_of(lines)


def test_a_second_manual_summary_is_refused_at_its_line() -> None:
    """Refuse two summaries, naming the line the second one started on."""
    lines = lines_of("verified")
    index = next(index for index, line in enumerate(lines) if line.startswith(COMPLETE))
    lines.insert(index + 1, MANUAL)
    assert f"line {index + 2}: a second manual harness summary" in refusal_of(lines)


def test_a_second_summary_header_inside_the_first_is_refused() -> None:
    """A summary header where a failure name or count belongs is refused."""
    lines = lines_of("verified")
    index = next(index for index, line in enumerate(lines) if line.startswith(MANUAL))
    lines.insert(index + 1, MANUAL)
    assert f"line {index + 2}: unrecognised manual summary line" in refusal_of(lines)


def test_a_garbage_line_after_the_summary_is_refused_at_its_own_line() -> None:
    """Require the summary to refuse an unexplained line once both counts exist."""
    lines = lines_of("verified")
    index = next(index for index, line in enumerate(lines) if line.startswith(" ** "))
    lines.insert(index + 1, "decisive chatter from a future release")
    assert (
        f"line {index + 2}: unrecognised summary line for 'harnesses::verified'"
        in refusal_of(lines)
    )


def test_a_garbage_line_after_the_verdict_is_refused_in_the_block() -> None:
    """Require the block's transcript skip to stop once a verdict is recorded."""
    lines = lines_of("verified")
    index = next(
        index for index, line in enumerate(lines) if line.startswith(VERIFICATION)
    )
    lines.insert(index + 1, "decisive chatter from a future release")
    assert (
        f"line {index + 2}: unrecognised line in the block for 'harnesses::verified'"
        in refusal_of(lines)
    )


def test_a_field_outside_a_check_is_refused() -> None:
    """Refuse a check field before any ``Check`` entry began."""
    lines = lines_of("verified")
    index = next(index for index, line in enumerate(lines) if line == RESULTS)
    lines.insert(index + 1, "\t - Status: SUCCESS")
    assert "field outside a check" in refusal_of(lines)


def test_an_inventory_and_a_verification_run_cannot_share_a_log() -> None:
    """Refuse a log that claims both formats."""
    lines = lines_of("verified")
    lines.insert(1, "Wrote list results to /tmp/kani-list.json")
    assert "both an inventory and a verification run" in refusal_of(lines)


def test_a_duplicated_harness_block_is_refused() -> None:
    """Refuse one harness reported twice in the same run."""
    lines = lines_of("verified")
    start = next(
        index for index, line in enumerate(lines) if line.startswith(HARNESS_START)
    )
    end = next(index for index, line in enumerate(lines) if line.startswith(TIME))
    lines[end + 1 : end + 1] = lines[start : end + 1]
    assert "reported twice in the same run" in refusal_of(lines)


def test_a_manual_count_that_denies_the_blocks_is_refused() -> None:
    """Refuse a summary whose counts disagree with the blocks."""
    lines = [
        line.replace(
            "Complete - 1 successfully verified harnesses, 0 failures, 1 total.",
            "Complete - 2 successfully verified harnesses, 0 failures, 2 total.",
        )
        for line in lines_of("verified")
    ]
    assert "the manual summary claims 2 verified" in refusal_of(lines)


def test_a_manual_failure_name_that_denies_the_blocks_is_refused() -> None:
    """Refuse a summary that names a different failed harness."""
    lines = [
        line.replace(
            "Verification failed for - harnesses::counterexample",
            "Verification failed for - harnesses::verified",
        )
        for line in lines_of("counterexample")
    ]
    assert "the manual summary names failures" in refusal_of(lines)


def test_a_block_that_never_reported_a_verdict_is_refused() -> None:
    """Refuse a block that closes without ever stating its verdict."""
    lines = lines_of("verified")
    verdict = next(
        index for index, line in enumerate(lines) if line.startswith(VERIFICATION)
    )
    summary = next(index for index, line in enumerate(lines) if line == SUMMARY)
    time_index = next(
        index for index, line in enumerate(lines) if line.startswith(TIME)
    )
    assert "ends without a verification result" in refusal_of(
        lines[:summary] + lines[time_index:]
    )
    assert verdict > summary


def test_an_explained_failure_the_count_denies_is_refused() -> None:
    """Refuse a block that explains more failures than it counts."""
    lines = lines_of("counterexample")
    index = next(
        index for index, line in enumerate(lines) if line.startswith("Failed Checks: ")
    )
    lines.insert(index, "Failed Checks: an invented second failure")
    lines.insert(index + 1, ' File: "src/harnesses.rs", line 9, in harnesses::x')
    assert "failures but explains" in refusal_of(lines)


def test_a_cover_count_the_listed_covers_deny_is_refused() -> None:
    """Refuse a cover count that disagrees with the listed witnesses."""
    lines = [
        line.replace(
            " ** 1 of 1 cover properties satisfied",
            " ** 2 of 2 cover properties satisfied",
        )
        for line in lines_of("verified")
    ]
    assert "covers but lists" in refusal_of(lines)


def test_the_reachable_warning_inside_a_block_is_skipped() -> None:
    """Kani may print its reachability warning inside a block's preamble."""
    lines = lines_of("verified")
    index = next(
        index for index, line in enumerate(lines) if line.startswith(HARNESS_START)
    )
    warning = next(
        line
        for line in lines_of("unsupported")
        if line.startswith("** WARNING: A Rust construct")
    )
    lines.insert(index + 1, warning)
    parse("\n".join(lines))


def test_the_reachable_warning_after_the_summary_is_skipped() -> None:
    """The warning may also sit between the summary counts and the verdict."""
    lines = lines_of("verified")
    index = next(
        index
        for index, line in enumerate(lines)
        if "cover properties satisfied" in line
    )
    warning = next(
        line
        for line in lines_of("unsupported")
        if line.startswith("** WARNING: A Rust construct")
    )
    lines.insert(index + 1, warning)
    parse("\n".join(lines))


@pytest.mark.parametrize("name", ["verified", "counterexample", "list"])
def test_an_extra_blank_line_changes_nothing(name: str) -> None:
    """Blank lines are not report structure and must be tolerated."""
    lines = lines_of(name)
    parse("\n".join([lines[0], "", *lines[1:]]))


def test_a_successful_verdict_with_an_ordinary_failure_is_refused() -> None:
    """A successful verdict cannot excuse an unexplained failed check."""
    lines = [
        line.replace(
            "VERIFICATION:- SUCCESSFUL (encountered one or more panics as expected)",
            "VERIFICATION:- SUCCESSFUL",
        )
        for line in lines_of("expected_panic")
    ]
    assert "reports SUCCESSFUL but lists" in refusal_of(lines)


def test_an_explicit_zero_other_count_is_refused() -> None:
    """An omitted count is optional; an explicit wrong zero is not."""
    lines = [
        line.replace(
            " ** 0 of 2 failed (1 unreachable)",
            " ** 0 of 2 failed (0 unreachable)",
        )
        for line in lines_of("verified")
    ]
    assert "counts 0 unreachable results but lists 1" in refusal_of(lines)
