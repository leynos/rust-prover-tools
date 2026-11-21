"""The classified refusals a Kani log reader records.

A refusal has two parts: the sentence a log producer can act on, and the class
of evidence it concerns. Keeping the class separate from the sentence lets the
parser expose contradictions, summary errors, and truncation as distinct facts
while one ordered issue list preserves the order in which the log broke.
"""

from __future__ import annotations

import enum
import typing as typ

__all__ = (
    "Issue",
    "IssueKind",
    "late_harness_issue",
    "second_banner_issue",
    "structural_issue",
)


class IssueKind(enum.StrEnum):
    """The class of a refusal the reader records."""

    STRUCTURAL = "structural"
    CONTRADICTION = "contradiction"
    TRUNCATION = "truncation"
    SUMMARY = "summary"


class Issue(typ.NamedTuple):
    """One recorded refusal, classified by the evidence it concerns.

    Attributes
    ----------
    kind : IssueKind
        Whether the refusal concerns report structure, a contradictory count,
        or a truncated report.
    message : str
        Sentence a log producer can act on.
    """

    kind: IssueKind
    message: str


def structural_issue(message: str) -> Issue:
    """Return a structural refusal."""
    return Issue(IssueKind.STRUCTURAL, message)


def second_banner_issue(line_number: int) -> Issue:
    """Return the refusal for a second invocation in one capture."""
    return Issue(
        IssueKind.STRUCTURAL,
        f"line {line_number}: a second Kani banner in one capture",
    )


def late_harness_issue(line_number: int) -> Issue:
    """Return the refusal for a harness block after the final summary."""
    return Issue(
        IssueKind.STRUCTURAL,
        f"line {line_number}: a harness block after the final manual summary",
    )
