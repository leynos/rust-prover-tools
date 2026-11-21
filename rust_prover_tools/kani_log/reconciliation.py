"""Reconcile one harness block's counts against the evidence it lists."""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.issues import Issue, IssueKind
from rust_prover_tools.kani_log.other_counts import reconcile_other
from rust_prover_tools.kani_log.syntax import ASSERTION_KIND, OTHER_STATUS

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.model import HarnessBlock

__all__ = ("reconcile",)


def reconcile(block: HarnessBlock) -> list[Issue]:
    """Check the block's counts against the checks and failures it lists."""
    if not block.seen_verification_time:
        return [
            Issue(
                IssueKind.TRUNCATION,
                f"the log is truncated: the block for {block.name!r} has "
                "no verification time",
            )
        ]
    if block.failed_count is None or block.total_count is None:
        return [
            Issue(
                IssueKind.STRUCTURAL,
                f"the block for {block.name!r} has no summary",
            )
        ]
    return [
        *_reconcile_checks(block),
        *_reconcile_failures(block),
        *_reconcile_covers(block),
        *reconcile_other(block),
    ]


def _reconcile_checks(block: HarnessBlock) -> list[Issue]:
    """Check the failure count against the listed checks."""
    name = block.name
    checks = block.checks
    reasons: list[Issue] = []
    if len(checks) != block.total_count:
        reasons.append(
            Issue(
                IssueKind.CONTRADICTION,
                f"the block for {name!r} counts {block.total_count} "
                f"checks but lists {len(checks)}",
            )
        )
    failed = sum(1 for item in checks if item.status == "FAILURE")
    if failed != block.failed_count:
        reasons.append(
            Issue(
                IssueKind.CONTRADICTION,
                f"the block for {name!r} counts {block.failed_count} "
                f"failures but lists {failed}",
            )
        )
    if _unexpected_failure(block, failed):
        reasons.append(
            Issue(
                IssueKind.CONTRADICTION,
                f"the block for {name!r} reports SUCCESSFUL but lists "
                f"{failed} failed check(s)",
            )
        )
    return reasons


def _unexpected_failure(block: HarnessBlock, failed: int) -> bool:
    """Whether a successful verdict contradicts a failure it cannot excuse."""
    if block.verdict != "SUCCESSFUL" or not failed:
        return False
    if not block.panics_as_expected:
        return True
    failed_checks = [item for item in block.checks if item.status == "FAILURE"]
    return any(item.kind != ASSERTION_KIND for item in failed_checks)


def _reconcile_failures(block: HarnessBlock) -> list[Issue]:
    """Check the failure explanations against the failure count.

    Every failed check is explained once. A ``File:`` line is retained
    when Kani prints one, but a failure without a location is accepted:
    Kani omits the location when its trace or source fields are absent.
    Requiring a location would reject valid failed proofs; the count still
    makes deleting a ``Failed Checks:`` line detectable.
    """
    name = block.name
    reasons: list[Issue] = []
    if len(block.failures) != block.failed_count:
        reasons.append(
            Issue(
                IssueKind.CONTRADICTION,
                f"the block for {name!r} counts {block.failed_count} "
                f"failures but explains {len(block.failures)}",
            )
        )
    return reasons


def _reconcile_covers(block: HarnessBlock) -> list[Issue]:
    """Check the cover counts against the listed witnesses."""
    name = block.name
    covers = block.covers
    reasons: list[Issue] = []
    if block.covered_total is None and covers:
        reasons.append(
            Issue(
                IssueKind.CONTRADICTION,
                f"the block for {name!r} lists {len(covers)} covers but has "
                "no cover count",
            )
        )
    if block.covered_total is not None and len(covers) != block.covered_total:
        reasons.append(
            Issue(
                IssueKind.CONTRADICTION,
                f"the block for {name!r} counts {block.covered_total} "
                f"covers but lists {len(covers)}",
            )
        )
    satisfied = sum(1 for item in covers if item.status == "SATISFIED")
    if block.covered_satisfied is not None and satisfied != block.covered_satisfied:
        reasons.append(
            Issue(
                IssueKind.CONTRADICTION,
                f"the block for {name!r} counts "
                f"{block.covered_satisfied} satisfied covers but lists "
                f"{satisfied}",
            )
        )
    expected = {kind: count for count, kind in block.covered_other_counts}
    for kind, status in OTHER_STATUS.items():
        count = expected.get(kind, 0)
        observed = sum(1 for item in covers if item.status == status)
        if count != observed:
            reasons.append(
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {name!r} counts {count} {kind} covers "
                    f"but lists {observed}",
                )
            )
    return reasons
