"""Hold one harness block's accumulation and reconciliation rules."""

from __future__ import annotations

from rust_prover_tools.kani_log.blocks import BlockCursor
from rust_prover_tools.kani_log.syntax import FailedCheck


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
