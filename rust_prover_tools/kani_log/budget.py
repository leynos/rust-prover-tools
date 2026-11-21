"""Bound the parser issues one attempt retains.

A harness can emit unrecognised lines for as long as its wall-clock budget
allows. Retaining every refusal would let a noisy run size the attempt's
memory. The budget keeps a bounded prefix: `finish` raises the first retained
issue, so the verdict is unchanged, and a sentinel records that later issues
were dropped so a reader of `facts` can tell the two cases apart.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.issues import Issue, IssueKind

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "MAX_RETAINED_ISSUES",
    "MAX_RETAINED_RECORDS",
    "IssueBudget",
    "RecordBudget",
    "retention_issue",
)

#: Retained issues per attempt.
MAX_RETAINED_ISSUES: typ.Final[int] = 256

#: Retained blocks, checks, failures, and manual failure names per attempt.
MAX_RETAINED_RECORDS: typ.Final[int] = 10_000


class IssueBudget:
    """A bounded prefix of classified issues and an exhaustion marker."""

    def __init__(self) -> None:
        """Start with an empty, unexhausted budget."""
        self._issues: list[Issue] = []
        self._exhausted = False

    @property
    def issues(self) -> tuple[Issue, ...]:
        """The retained issues, in arrival order, including the sentinel."""
        return tuple(self._issues)

    def record(self, issues: cabc.Sequence[Issue]) -> None:
        """Retain classified issues up to the budget.

        Parameters
        ----------
        issues : collections.abc.Sequence[Issue]
            Newly classified issues, in arrival order.
        """
        for issue in issues:
            if len(self._issues) < MAX_RETAINED_ISSUES:
                self._issues.append(issue)
            elif not self._exhausted:
                self._exhausted = True
                self._issues.append(
                    Issue(
                        IssueKind.STRUCTURAL,
                        "further parser issues were dropped after the first "
                        f"{MAX_RETAINED_ISSUES}",
                    )
                )


def retention_issue(subject: str) -> Issue:
    """Return the refusal for a retained collection that hit its budget."""
    return Issue(
        IssueKind.TRUNCATION,
        f"{subject} retention is truncated at {MAX_RETAINED_RECORDS}",
    )


class RecordBudget:
    """One attempt's shared budget for retained evidence records."""

    def __init__(self, limit: int = MAX_RETAINED_RECORDS) -> None:
        """Create a budget that admits `limit` records."""
        self._remaining = limit

    def take(self) -> bool:
        """Consume one record slot, reporting whether one was available."""
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True
