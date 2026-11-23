"""Reconcile one finished harness block against the checks it listed.

The reader collects a block's fields as it walks the log; this module decides
whether the collected fields describe a report that Kani could have printed.
Every check here is a structural one: a verdict that never arrived, a section
that was lost to truncation, or a count that disagrees with the checks beneath
it.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log.syntax import (
    COVER_PROPERTY_STATUSES,
    OTHER_STATUS,
    UNWINDING_DESCRIPTIONS,
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
    # A status Kani never renders is a more specific fault than any verdict
    # disagreement it also causes, so it is reported first.
    _require_cover_statuses(builder)
    _require_panic_note(builder)
    if builder.timed_out:
        # A timeout aborts CBMC before ``RESULTS:``, so a block carrying the
        # marker has nothing further to reconcile; anything that did arrive
        # makes the marker contradictory, which ``_require_timeout`` refuses.
        _require_timeout(builder)
        return
    _require_unwinding_note(builder)
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
    """Reconcile the verdict against the checks it reports.

    Kani calls a harness successful only when no check failed, unless it
    excused the failed property as the panic the harness declared.  The
    expected-panic note is printed only under that excuse, and only for a
    successful verdict.  A plain ``SUCCESSFUL`` verdict above a failed property
    is therefore a report the pinned release cannot produce, and accepting it
    would turn a counterexample into a clean result.
    """
    failed = any(item.status == "FAILURE" for item in builder.properties)
    if builder.panics_as_expected:
        if not failed:
            msg = (
                f"the block for {builder.name!r} reports the expected-panic note "
                "but no check failed"
            )
            raise KaniLogError(msg)
        if builder.verdict != "SUCCESSFUL":
            msg = (
                f"the block for {builder.name!r} reports {builder.verdict} with "
                "the expected-panic note, which Kani prints only for a "
                "successful verdict"
            )
            raise KaniLogError(msg)
        return
    if builder.verdict == "SUCCESSFUL" and failed:
        msg = (
            f"the block for {builder.name!r} reports SUCCESSFUL while a check "
            "failed, without the expected-panic note"
        )
        raise KaniLogError(msg)


def _require_unwinding_note(builder: _BlockBuilder) -> None:
    """Reconcile the unwinding-failure note against the checks beneath it.

    The renderer prints the note when any failed property's description holds
    one of its bound phrases; the property identifier does not decide it.  A
    block carrying the note without such a failure is a report the pinned
    release cannot produce, and accepting it would certify a run whose stated
    failure the analysis never saw.  The identifier still classifies a failed
    bound check as an unwinding failure rather than a counterexample; this
    check only asks whether the note is explicable.  A timed-out block keeps
    its own path: `_require_timeout` reconciles it and refuses every completed
    field.
    """
    if not builder.unwinding_reported:
        return
    if not any(
        item.status == "FAILURE"
        and any(phrase in item.description for phrase in UNWINDING_DESCRIPTIONS)
        for item in builder.properties
    ):
        msg = (
            f"the block for {builder.name!r} reports unwinding failures "
            "but no failed check describes a bound"
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
        builder.unwinding_reported,
        builder.saw_summary,
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
    _require_subtotal(
        builder,
        _Subtotal(
            "unreachable",
            builder.covered_unreachable,
            sum(1 for item in covers if item.status == "UNREACHABLE"),
            "covers",
        ),
    )
    _require_subtotal(
        builder,
        _Subtotal(
            "undetermined",
            builder.covered_undetermined,
            sum(1 for item in covers if item.status == "UNDETERMINED"),
            "covers",
        ),
    )


@dc.dataclass(frozen=True, slots=True)
class _Subtotal:
    """One parenthesised status count and the checks it must agree with."""

    kind: str
    counted: int | None
    observed: int
    subject: str


def _require_subtotal(builder: _BlockBuilder, subtotal: _Subtotal) -> None:
    """Require a positive status count to have its printed subtotal.

    The renderer prints a parenthesised subtotal for every positive count and
    omits it otherwise, so a subtotal is not optional evidence.  A block that
    lists such a status without its subtotal is a report the pinned release
    cannot produce, and accepting it would leave the count it exists to
    validate unexamined.
    """
    if subtotal.counted is None:
        if subtotal.observed:
            msg = (
                f"the block for {builder.name!r} lists {subtotal.observed} "
                f"{subtotal.kind} {subtotal.subject} but has no "
                f"{subtotal.kind} count"
            )
            raise KaniLogError(msg)
        return
    if subtotal.counted != subtotal.observed:
        msg = (
            f"the block for {builder.name!r} counts {subtotal.counted} "
            f"{subtotal.kind} {subtotal.subject} but lists {subtotal.observed}"
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

    The renderer prints a subtotal for every positive count, so a check that
    carries one of these statuses without its subtotal is a report the pinned
    release cannot produce; the subtotal would otherwise be the one piece of
    evidence that the status counts agree with the checks.  A printed zero must
    likewise agree that no check carried that status.
    """
    for kind, expected in OTHER_STATUS.items():
        observed = sum(1 for item in checks if item.status == expected)
        _require_subtotal(
            builder,
            _Subtotal(kind, builder.other_counts.get(kind), observed, "results"),
        )
