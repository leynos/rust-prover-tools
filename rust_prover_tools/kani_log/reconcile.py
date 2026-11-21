"""Reconcile one finished harness block against the checks it listed.

The reader collects a block's fields as it walks the log; this module decides
whether the collected fields describe a report that Kani could have printed.
Every check here is a structural one: a verdict that never arrived, a section
that was lost to truncation, or a count that disagrees with the checks beneath
it.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.syntax import (
    COVER_PROPERTY_STATUSES,
    OTHER_STATUS,
    KaniLogError,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.reader import _BlockBuilder
    from rust_prover_tools.kani_log.syntax import PropertyResult

__all__ = ("require_complete",)


def require_complete(builder: _BlockBuilder) -> None:
    """Reconcile one block's counts against the checks it listed.

    Parameters
    ----------
    builder : _BlockBuilder
        The block that is about to be finished.

    Raises
    ------
    KaniLogError
        If the block is missing a verdict, results or summary, or if any count
        disagrees with the listed checks.

    """
    if builder.verdict is None:
        msg = f"the block for {builder.name!r} ends without a verification result"
        raise KaniLogError(msg)
    _require_panic_note(builder)
    if builder.timed_out:
        # A timeout aborts CBMC before ``RESULTS:``, so a block carrying the
        # marker has nothing further to reconcile; anything that did arrive
        # makes the marker contradictory, which ``_require_timeout`` refuses.
        _require_timeout(builder)
        return
    if not builder.has_results:
        # Kani prints ``RESULTS:`` even when there are no checks, so a block
        # that lacks it lost part of its report rather than reporting none.
        msg = f"the block for {builder.name!r} has no results section"
        raise KaniLogError(msg)
    if not builder.seen_verification_time:
        # `Verification Time` is the last line of every non-timeout block.
        # Requiring it is what makes truncating a complete log detectable at
        # all: everything after the summary is optional chatter.
        msg = (
            f"the log is truncated: the block for {builder.name!r} "
            "has no verification time"
        )
        raise KaniLogError(msg)
    if builder.failed_count is None or builder.total_count is None:
        msg = f"the block for {builder.name!r} has no summary"
        raise KaniLogError(msg)
    checks = builder.checks
    if len(checks) != builder.total_count:
        msg = (
            f"the block for {builder.name!r} counts {builder.total_count} "
            f"checks but lists {len(checks)}"
        )
        raise KaniLogError(msg)
    _require_counts(builder, checks)
    _require_covers(builder)
    _require_other(builder, checks)


def _require_panic_note(builder: _BlockBuilder) -> None:
    """Reconcile the expected-panic note against the block it certifies.

    Kani prints the note only when it excused a failed property as the panic
    the harness declared, and that is the same condition under which it calls
    the harness successful.  A failed verdict, or a block with no failed check
    to excuse, is therefore a report the pinned release cannot produce.
    """
    if not builder.panics_as_expected:
        return
    if not any(item.status == "FAILURE" for item in builder.properties):
        msg = (
            f"the block for {builder.name!r} reports the expected-panic note "
            "but no check failed"
        )
        raise KaniLogError(msg)
    if builder.verdict != "SUCCESSFUL":
        msg = (
            f"the block for {builder.name!r} reports {builder.verdict} with the "
            "expected-panic note, which Kani prints only for a successful verdict"
        )
        raise KaniLogError(msg)


def _require_timeout(builder: _BlockBuilder) -> None:
    """Reconcile a block that timed out before reporting any check.

    A timeout aborts CBMC before it prints ``RESULTS:``, so the block has no
    results, summary or verification time; its failed verdict alone is the
    fact.  A successful verdict, or any completed-result field, means the block
    was not the shape a timeout produces and must not take the exception.

    Parameters
    ----------
    builder : _BlockBuilder
        The block carrying the timeout marker.

    Raises
    ------
    KaniLogError
        If the block's verdict is not ``FAILED`` or it also reports a
        completed result.

    """
    completed_fields = (
        builder.has_results,
        builder.seen_verification_time,
        builder.failed_count is not None,
        builder.covered_total is not None,
    )
    if builder.verdict != "FAILED" or any(completed_fields):
        msg = (
            f"the block for {builder.name!r} reports a timeout alongside a "
            "completed result"
        )
        raise KaniLogError(msg)


def _require_counts(
    builder: _BlockBuilder,
    checks: tuple[PropertyResult, ...],
) -> None:
    """Require failed-check counts and explanations to reconcile."""
    failed = sum(1 for item in checks if item.status == "FAILURE")
    if failed != builder.failed_count:
        msg = (
            f"the block for {builder.name!r} counts "
            f"{builder.failed_count} failures but lists {failed}"
        )
        raise KaniLogError(msg)
    if len(builder.failures) != builder.failed_count:
        msg = (
            f"the block for {builder.name!r} counts "
            f"{builder.failed_count} failures but explains "
            f"{len(builder.failures)}"
        )
        raise KaniLogError(msg)


def _require_covers(builder: _BlockBuilder) -> None:
    """Require cover counts and statuses to reconcile."""
    _require_cover_statuses(builder)
    if builder.covered_total is None and builder.covers:
        msg = (
            f"the block for {builder.name!r} lists {len(builder.covers)} "
            "covers but has no cover count"
        )
        raise KaniLogError(msg)
    covers = builder.covers
    if builder.covered_total is not None and len(covers) != builder.covered_total:
        msg = (
            f"the block for {builder.name!r} counts "
            f"{builder.covered_total} covers but lists {len(covers)}"
        )
        raise KaniLogError(msg)
    satisfied = sum(1 for item in covers if item.status == "SATISFIED")
    if builder.covered_satisfied is not None and satisfied != builder.covered_satisfied:
        msg = (
            f"the block for {builder.name!r} counts "
            f"{builder.covered_satisfied} satisfied covers but lists "
            f"{satisfied}"
        )
        raise KaniLogError(msg)
    observed_unreachable = sum(1 for item in covers if item.status == "UNREACHABLE")
    if (
        builder.covered_unreachable is not None
        and builder.covered_unreachable != observed_unreachable
    ):
        msg = (
            f"the block for {builder.name!r} counts "
            f"{builder.covered_unreachable} unreachable covers but lists "
            f"{observed_unreachable}"
        )
        raise KaniLogError(msg)
    observed_undetermined = sum(1 for item in covers if item.status == "UNDETERMINED")
    if (
        builder.covered_undetermined is not None
        and builder.covered_undetermined != observed_undetermined
    ):
        msg = (
            f"the block for {builder.name!r} counts "
            f"{builder.covered_undetermined} undetermined covers but lists "
            f"{observed_undetermined}"
        )
        raise KaniLogError(msg)


def _require_cover_statuses(builder: _BlockBuilder) -> None:
    """Refuse a cover status Kani cannot print.

    A cover line is not one of the checks whose count is reconciled, so an
    impossible status there would otherwise pass unexamined: the entry is
    excluded from both the check total and every cover count.  Kani converts a
    cover CBMC reported as ``FAILURE`` into ``SATISFIED`` before rendering, so
    the status is evidence the report did not come from the pinned release.
    """
    for cover in builder.covers:
        if cover.status not in COVER_PROPERTY_STATUSES:
            msg = (
                f"the block for {builder.name!r} reports cover "
                f"{cover.identifier!r} as {cover.status}, which Kani never prints"
            )
            raise KaniLogError(msg)


def _require_other(
    builder: _BlockBuilder,
    checks: tuple[PropertyResult, ...],
) -> None:
    """Require each parenthesised status count to reconcile.

    An absent count says nothing about the checks, but a printed one does -
    including a printed zero, which must agree that no check carried that
    status.
    """
    for kind, counted in builder.other_counts.items():
        expected = OTHER_STATUS.get(kind, "")
        observed = sum(1 for item in checks if item.status == expected)
        if counted != observed:
            msg = (
                f"the block for {builder.name!r} counts {counted} "
                f"{kind} results but lists {observed}"
            )
            raise KaniLogError(msg)
