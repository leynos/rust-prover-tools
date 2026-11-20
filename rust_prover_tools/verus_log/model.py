"""Hold the frozen values one parsed Verus log produces.

A log reduces to three facts: the summary counts Verus printed, the failures
it reported, and the note naming what was selected for verification.  Each is
a frozen record holding only text and integers, so a parsed log compares and
hashes like the text it came from.
"""

from __future__ import annotations

import dataclasses as dc

__all__ = ("VerusFailure", "VerusLog", "VerusSummary")


@dc.dataclass(frozen=True, slots=True)
class VerusFailure:
    """One ``error:`` diagnostic, with the span rustc printed beneath it.

    The span is optional because not every diagnostic has one: a usage error
    such as an unknown ``--verify-function`` name names no source location.
    """

    message: str
    path: str | None = None
    line: int | None = None
    column: int | None = None

    @property
    def is_located(self) -> bool:
        """Report whether the diagnostic names a source position."""
        return self.path is not None and self.line is not None

    @property
    def where(self) -> str:
        """Render the span as ``path:line:column``, or an empty string.

        Returns
        -------
        str
            The source position in the form rustc prints it, or an empty
            string when the diagnostic named none.

        """
        if self.path is None or self.line is None:
            return ""
        if self.column is None:
            return f"{self.path}:{self.line}"
        return f"{self.path}:{self.line}:{self.column}"


@dc.dataclass(frozen=True, slots=True)
class VerusSummary:
    """The counts from one ``verification results::`` line."""

    verified: int
    errors: int
    is_partial: bool = False

    @property
    def total(self) -> int:
        """How many functions the run verified or failed."""
        return self.verified + self.errors


@dc.dataclass(frozen=True, slots=True)
class VerusLog:
    """One captured Verus invocation, reduced to the facts it reported."""

    summary: VerusSummary | None = None
    failures: tuple[VerusFailure, ...] = ()
    selection: str | None = None
