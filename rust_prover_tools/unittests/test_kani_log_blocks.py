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
