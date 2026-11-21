"""Hold one harness block's accumulation and reconciliation rules."""

from __future__ import annotations

import pytest

from rust_prover_tools.kani_log.blocks import BlockCursor, IssueKind
from rust_prover_tools.kani_log.budget import RecordBudget
from rust_prover_tools.kani_log.model import HarnessBlock
from rust_prover_tools.kani_log.other_counts import (
    parse_other_pairs,
    reconcile_other,
)
from rust_prover_tools.kani_log.reconciliation import reconcile
from rust_prover_tools.kani_log.syntax import (
    COVER,
    FailedCheck,
    PropertyResult,
)


def test_a_failure_awaiting_a_location_survives_end_of_stream() -> None:
    """A truncated stream keeps the last failure it received."""
    cursor = BlockCursor()
    cursor.start("harnesses::a")
    cursor.add_failure("assertion failed")

    cursor.finish()

    assert cursor.block.failures == [FailedCheck("assertion failed", None, None)]


def test_a_repeated_check_identifier_is_refused() -> None:
    """One property identifier may describe only one check."""
    cursor = BlockCursor()
    cursor.start("harnesses::a")
    issues = []
    for _ in range(2):
        cursor.begin_check("harnesses::a.assertion.1")
        cursor.add_check_field("Status", "SUCCESS")
        cursor.add_check_field("Description", '"same"')
        cursor.add_check_field("Location", "src/lib.rs:1")
        issues.extend(cursor.flush_check())

    assert any("more than once" in issue.message for issue in issues)
    assert len(cursor.block.properties) == 1


def test_retained_checks_are_bounded() -> None:
    """A block keeps a bounded prefix and reports the truncation."""
    cursor = BlockCursor(RecordBudget(1))
    cursor.start("harnesses::a")
    issues = []
    for index in range(2):
        cursor.begin_check(f"harnesses::a.assertion.{index}")
        cursor.add_check_field("Status", "SUCCESS")
        cursor.add_check_field("Description", '"ok"')
        cursor.add_check_field("Location", "src/lib.rs:1")
        issues.extend(cursor.flush_check())

    assert len(cursor.block.properties) == 1
    assert any(issue.kind is IssueKind.TRUNCATION for issue in issues)


def test_an_undetermined_cover_count_is_parsed() -> None:
    """Kani may print an undetermined cover count instead of unreachable."""
    cursor = BlockCursor()
    cursor.start("harnesses::a")
    match = COVER.match(" ** 0 of 1 cover properties satisfied (1 undetermined)")
    assert match is not None

    assert cursor.add_cover(match) == []
    assert cursor.block.covered_other_counts == ((1, "undetermined"),)


def test_an_omitted_other_count_is_checked_as_zero() -> None:
    """A summary without parentheses cannot hide an undetermined check."""
    block = HarnessBlock(name="harnesses::a")
    block.properties = [
        PropertyResult("harnesses::a.assertion.0", "FAILURE", "a"),
        PropertyResult("harnesses::a.assertion.1", "UNDETERMINED", "b"),
    ]

    issues = reconcile_other(block)

    assert any("0 undetermined" in issue.message for issue in issues)


def test_an_expected_panic_cannot_excuse_an_unwind_failure() -> None:
    """Only assertion failures are excused by Kani's expected-panic note."""
    block = HarnessBlock(
        name="harnesses::a",
        failed_count=1,
        total_count=1,
        verdict="SUCCESSFUL",
        panics_as_expected=True,
        seen_verification_time=True,
    )
    block.properties = [
        PropertyResult("harnesses::a.unwind.0", "FAILURE", "unwind"),
    ]

    issues = reconcile(block)

    assert any("SUCCESSFUL but lists" in issue.message for issue in issues)


def test_an_expected_assertion_panic_remains_successful() -> None:
    """A failed assertion under the expected-panic note is not a contradiction."""
    block = HarnessBlock(
        name="harnesses::a",
        failed_count=1,
        total_count=1,
        verdict="SUCCESSFUL",
        panics_as_expected=True,
        seen_verification_time=True,
    )
    block.properties = [
        PropertyResult("harnesses::a.assertion.0", "FAILURE", "panic"),
    ]

    issues = reconcile(block)

    assert not any("SUCCESSFUL but lists" in issue.message for issue in issues)


def test_a_repeated_other_kind_is_refused() -> None:
    """Two counts of one kind cannot overwrite each other silently."""
    with pytest.raises(ValueError, match="repeated other-result kind"):
        parse_other_pairs("99 unreachable,1 unreachable")
