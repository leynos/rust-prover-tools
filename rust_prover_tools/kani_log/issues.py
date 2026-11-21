"""The classified refusals a Kani log reader records.

A refusal has two parts: the sentence a log producer can act on, and the class
of evidence it concerns. Keeping the class separate from the sentence lets the
parser expose contradictions, summary errors, and truncation as distinct facts
while one ordered issue list preserves the order in which the log broke.
"""

from __future__ import annotations

import enum
import typing as typ

__all__ = ("Issue", "IssueKind")


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
