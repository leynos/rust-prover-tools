"""Reconcile a complete Kani report's blocks against its manual summary.

The reader knows what each block said; this module decides whether the run as a
whole can be believed. A log that reports a harness twice, claims to be both an
inventory and a verification run, or prints a manual summary whose counts
disagree with its blocks is not a log with a defect — it is a log the grammar
cannot turn into facts, because there is no consistent reading of it.

The rules are derived from Combobulate's ``docs_validation.kani_log.extract``
at commit ``9dd6e3c`` and are used under the ISC licence recorded in
``ATTRIBUTION.md``.

Examples
--------
>>> reconcile_report((), None, is_inventory=False)[0].message
'the log reports no harness results'
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.blocks import Issue, IssueKind

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_log.model import HarnessBlock, ManualSummary

__all__ = ("reconcile_report",)


def reconcile_report(
    blocks: cabc.Sequence[HarnessBlock],
    manual: ManualSummary | None,
    *,
    is_inventory: bool,
) -> list[Issue]:
    """Return every reason a whole report cannot be read consistently.

    Parameters
    ----------
    blocks : Sequence[HarnessBlock]
        Harness blocks in log order.
    manual : ManualSummary | None
        Final manual summary, absent for an inventory run.
    is_inventory : bool
        Whether the log declared itself a ``kani list`` result.

    Returns
    -------
    list[Issue]
        Structural, truncation, or summary issues. An empty list means the
        report is consistent.
    """
    if is_inventory:
        if blocks:
            return [
                Issue(
                    IssueKind.STRUCTURAL,
                    "the log claims to be both an inventory and a verification run",
                )
            ]
        return []
    if not blocks:
        return [Issue(IssueKind.STRUCTURAL, "the log reports no harness results")]
    if manual is None:
        return [
            Issue(
                IssueKind.TRUNCATION,
                "the log is truncated: it has no manual harness summary",
            )
        ]
    return _reconcile_manual(blocks, manual)


def _reconcile_manual(
    blocks: cabc.Sequence[HarnessBlock],
    manual: ManualSummary,
) -> list[Issue]:
    """Compare the manual summary's counts and names with the blocks."""
    names = tuple(block.name for block in blocks)
    issues: list[Issue] = []
    if len(set(names)) != len(names):
        issues.append(
            Issue(
                IssueKind.STRUCTURAL,
                "a harness was reported twice in the same run",
            )
        )
    succeeded = tuple(block.name for block in blocks if block.succeeded)
    failed = tuple(block.name for block in blocks if block.verdict == "FAILED")
    if (len(succeeded), len(failed), len(names)) != (
        manual.verified,
        manual.failures,
        manual.total,
    ):
        issues.append(
            Issue(
                IssueKind.SUMMARY,
                f"the manual summary claims {manual.verified} verified and "
                f"{manual.failures} failed of {manual.total}, but the blocks "
                f"report {len(succeeded)} verified and {len(failed)} failed of "
                f"{len(names)}",
            )
        )
    if manual.failed != failed:
        issues.append(
            Issue(
                IssueKind.SUMMARY,
                f"the manual summary names failures {list(manual.failed)}, but "
                f"the blocks report {list(failed)}",
            )
        )
    return issues
