"""Hold the parsed value types a Kani log produces.

:mod:`rust_prover_tools.kani_log.reader` walks the text and builds these; the
classification adapter and the tests read them. They live apart from the walk
so a value type names no regular expression and a test can construct facts
without a log.

:class:`HarnessBlock` is deliberately mutable. It is filled field by field as
one block is read, and the counts are only reconcilable once the whole block
has been seen; the frozen types beside it are the finished facts.

The value types are derived from Combobulate's ``docs_validation.kani_log``
package at commit ``9dd6e3c`` and are used under the ISC licence recorded in
``ATTRIBUTION.md`` beside this module.

Examples
--------
>>> HarnessBlock(name="h::x", verdict="SUCCESSFUL").succeeded
True
>>> KaniLog("0.67.0", "regular", (), None, (), 0).harnesses
()
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.syntax import FailedCheck, PropertyResult

__all__ = (
    "HarnessBlock",
    "KaniLog",
    "KaniLogFacts",
    "ManualSummary",
)


@dc.dataclass(slots=True)
class HarnessBlock:
    """Everything one ``Checking harness`` block reported.

    Attributes
    ----------
    name : str
        Harness name Kani selected.
    properties : list[PropertyResult]
        Checks listed under ``RESULTS:``.
    failures : list[FailedCheck]
        Failure explanations listed under ``SUMMARY:``.
    failed_count : int | None
        Count from the ``N of M failed`` line, when printed.
    total_count : int | None
        Total from the same line, when printed.
    other_count : int
        Count of the first ``(N undetermined)`` or similar result pair.
    other_kind : str | None
        Word Kani used for the first ``other_count``.
    other_counts : tuple[tuple[int, str], ...]
        Every ``(count, kind)`` pair in the summary, in log order.
    covered_satisfied : int | None
        Satisfied cover count, when a cover count was printed.
    covered_total : int | None
        Total cover count, when a cover count was printed.
    covered_unreachable : int | None
        Unreachable cover count, when Kani printed one.
    verdict : str | None
        ``SUCCESSFUL`` or ``FAILED`` from ``VERIFICATION:``.
    panics_as_expected : bool
        Whether the verdict carried Kani's expected-panic note.
    solver_command : str | None
        Echoed CBMC command, when the log carried one.
    seen_verification_time : bool
        Whether the block ended with its ``Verification Time`` line.
    timed_out : bool
        Whether CBMC reported its own timeout.
    unwinding_reported : bool
        Whether Kani reported unwinding failures.
    has_results : bool
        Whether the block printed a ``RESULTS:`` section.
    """

    name: str
    properties: list[PropertyResult] = dc.field(default_factory=list)
    failures: list[FailedCheck] = dc.field(default_factory=list)
    failed_count: int | None = None
    total_count: int | None = None
    other_count: int = 0
    other_kind: str | None = None
    other_counts: tuple[tuple[int, str], ...] = ()
    covered_satisfied: int | None = None
    covered_total: int | None = None
    covered_unreachable: int | None = None
    verdict: str | None = None
    panics_as_expected: bool = False
    solver_command: str | None = None
    seen_verification_time: bool = False
    timed_out: bool = False
    unwinding_reported: bool = False
    has_results: bool = False

    @property
    def succeeded(self) -> bool:
        """Report whether Kani declared this block successful.

        ``SUCCESSFUL (encountered one or more panics as expected)`` is still a
        success for the harness, which is what a ``#[kani::should_panic]``
        claim relies on, so the note does not change the verdict.
        """
        return self.verdict == "SUCCESSFUL"

    @property
    def checks(self) -> tuple[PropertyResult, ...]:
        """The assertion and unwinding checks, excluding covers."""
        return tuple(item for item in self.properties if not item.is_cover)

    @property
    def covers(self) -> tuple[PropertyResult, ...]:
        """The declared success witnesses."""
        return tuple(item for item in self.properties if item.is_cover)

    def owned(self, harness: str) -> tuple[PropertyResult, ...]:
        """Return the checks whose identifier belongs to ``harness``."""
        return tuple(item for item in self.properties if item.owner == harness)


@dc.dataclass(frozen=True, slots=True)
class ManualSummary:
    """The counts Kani prints once after every harness block.

    Attributes
    ----------
    verified : int
        Number of successfully verified harnesses.
    failures : int
        Number of failed harnesses.
    total : int
        Total harnesses in the run.
    failed : tuple[str, ...]
        Names listed beneath ``Verification failed for``.
    """

    verified: int
    failures: int
    total: int
    failed: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class KaniLog:
    """The facts one complete log states, without interpretation.

    Attributes
    ----------
    banner_version : str
        Release named by the banner.
    format : str
        ``regular`` for a verification run or ``inventory`` for ``kani list``
        output, which carries no harness verdicts.
    blocks : tuple[HarnessBlock, ...]
        Harness blocks in log order.
    manual : ManualSummary | None
        Final manual summary, absent for an inventory run.
    unsupported_constructs : tuple[str, ...]
        Constructs Kani named in its pre-flight warning.
    exit_code : int | None
        Raw child status supplied to `finish`, when one was available.
    """

    banner_version: str
    format: str
    blocks: tuple[HarnessBlock, ...]
    manual: ManualSummary | None
    unsupported_constructs: tuple[str, ...]
    exit_code: int | None = None

    @property
    def harnesses(self) -> tuple[str, ...]:
        """Name every harness that produced a verification block, in log order."""
        return tuple(block.name for block in self.blocks)


@dc.dataclass(frozen=True, slots=True)
class KaniLogFacts:
    """Facts a live reader can report before, or instead of, a complete log.

    The supervision layer needs to associate the last available evidence with a
    harness even when the log was cut short, so these facts never raise for an
    incomplete log. ``truncated`` and the error tuples say what is missing.

    Attributes
    ----------
    banner_version : str | None
        Release named by the banner, when one was seen.
    format : str | None
        ``regular`` or ``inventory`` once the log has said which it is.
    selection_records : tuple[str, ...]
        Harness names from ``Checking harness`` lines, in arrival order.
    verdicts : tuple[tuple[str, str], ...]
        ``(harness, verdict)`` pairs from ``VERIFICATION:`` lines.
    contradictions : tuple[str, ...]
        Block counts that disagree with the checks the block listed.
    summary_errors : tuple[str, ...]
        Manual-summary counts or failure names that disagree with the blocks.
    truncation_reason : str | None
        Why the log is incomplete, when it is.
    unsupported_constructs : tuple[str, ...]
        Constructs Kani named in its pre-flight warning.
    completed : bool
        Whether a manual summary or an inventory marker completed the log.
    exit_code : int | None
        Raw child status, when `finish` has been called.
    last_progress : str | None
        Last structurally recognised report line, bounded by the caller.
    """

    banner_version: str | None
    format: str | None
    selection_records: tuple[str, ...]
    verdicts: tuple[tuple[str, str], ...]
    contradictions: tuple[str, ...]
    summary_errors: tuple[str, ...]
    truncation_reason: str | None
    unsupported_constructs: tuple[str, ...]
    completed: bool
    exit_code: int | None
    last_progress: str | None

    @property
    def truncated(self) -> bool:
        """Report whether the log ended before its report completed."""
        return self.truncation_reason is not None
