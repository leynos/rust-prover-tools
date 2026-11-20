"""Hold the parsed value types a Kani log produces.

:mod:`rust_prover_tools.kani_log.extract` walks the text and builds these; the
decision function and the tests read them.  They live apart from the walk for
two reasons: the walker is the only part that needs the grammar, and a value
type that names no regular expression is easier to construct in a test than one
that shares a module with a line cursor.

:class:`HarnessBlock` is frozen at the public boundary.  The mutable
accumulation happens in a private builder inside the reader; the finished block
is a tuple-only value the caller can compare and hash.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.syntax import FailedCheck, PropertyResult

__all__ = ("HarnessBlock", "KaniLog", "ManualSummary")


@dc.dataclass(frozen=True, slots=True)
class HarnessBlock:
    """Everything one ``Checking harness`` block reported.

    Attributes
    ----------
    name : str
        Harness name as Kani printed it.
    properties : tuple[PropertyResult, ...]
        Every check Kani listed for this block.
    failures : tuple[FailedCheck, ...]
        Failed-check explanations, each with its source location.
    failed_count : int | None
        Failure count from ``SUMMARY:``; ``None`` for a timed-out block.
    total_count : int | None
        Check count from ``SUMMARY:``; ``None`` for a timed-out block.
    other_counts : tuple[tuple[str, int], ...]
        Counts Kani printed in parentheses after the failure count, one pair
        per status, such as ``(("undetermined", 3),)``; empty when the block
        printed none.
    covered_satisfied : int | None
        Satisfied cover count, when the block declared covers.
    covered_total : int | None
        Total cover count, when the block declared covers.
    covered_unreachable : int | None
        Unreachable cover count, when Kani reported one.
    covered_undetermined : int | None
        Undetermined cover count, when Kani reported one.
    verdict : str | None
        ``SUCCESSFUL`` or ``FAILED`` from ``VERIFICATION:``.
    panics_as_expected : bool
        Whether Kani excused the failure because a panic was expected.
    solver_command : str | None
        Echoed ``cbmc`` command line, when the block printed one.
    seen_verification_time : bool
        Whether the block ended with its ``Verification Time:`` line.
    timed_out : bool
        Whether CBMC reported a timeout.
    unwinding_reported : bool
        Whether Kani reported an unwinding failure note.
    has_results : bool
        Whether the block carried a ``RESULTS:`` section.

    """

    name: str
    properties: tuple[PropertyResult, ...] = ()
    failures: tuple[FailedCheck, ...] = ()
    failed_count: int | None = None
    total_count: int | None = None
    other_counts: tuple[tuple[str, int], ...] = ()
    covered_satisfied: int | None = None
    covered_total: int | None = None
    covered_unreachable: int | None = None
    covered_undetermined: int | None = None
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
        success for the harness, so the note does not change the verdict.
        """
        return self.verdict == "SUCCESSFUL"

    @property
    def checks(self) -> tuple[PropertyResult, ...]:
        """The assertion and unwinding checks, excluding covers."""
        return tuple(item for item in self.properties if not item.is_cover)

    @property
    def covers(self) -> tuple[PropertyResult, ...]:
        """The declared cover properties."""
        return tuple(item for item in self.properties if item.is_cover)

    def owned(self, harness: str) -> tuple[PropertyResult, ...]:
        """Return the checks whose identifier belongs to ``harness``.

        Parameters
        ----------
        harness : str
            Harness name whose checks should be selected.

        Returns
        -------
        tuple[PropertyResult, ...]
            Checks attributed to the named harness, in log order.

        """
        return tuple(item for item in self.properties if item.owner == harness)


@dc.dataclass(frozen=True, slots=True)
class ManualSummary:
    """The counts Kani prints once after every harness block.

    Attributes
    ----------
    verified : int
        Harnesses whose verdict was ``SUCCESSFUL``.
    failures : int
        Harnesses whose verdict was ``FAILED``; it counts harnesses, not
        failed checks.
    total : int
        Harnesses the run reported.
    failed : tuple[str, ...]
        Names of the failed harnesses, in the order Kani listed them.

    """

    verified: int
    failures: int
    total: int
    failed: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class KaniLog:
    """The facts one log states, without interpretation.

    ``format`` is ``regular`` for a verification run and ``inventory`` for
    ``kani list`` output, which carries no harness verdicts.  A log that is
    neither cannot be analysed, and :func:`parse_kani_log` refuses it.

    Attributes
    ----------
    banner_version : str
        Kani release the log's banner named.
    format : str
        ``regular`` for a verification run, ``inventory`` for ``kani list``.
    blocks : tuple[HarnessBlock, ...]
        One block per harness Kani ran, in log order; empty for an inventory.
    manual : ManualSummary | None
        The run's closing counts, or ``None`` for an inventory, which has no
        manual summary.
    unsupported_constructs : tuple[str, ...]
        Constructs Kani named as unsupported before verification began.

    """

    banner_version: str
    format: str
    blocks: tuple[HarnessBlock, ...]
    manual: ManualSummary | None
    unsupported_constructs: tuple[str, ...]

    @property
    def harnesses(self) -> tuple[str, ...]:
        """Name every harness that produced a verification block, in log order."""
        return tuple(block.name for block in self.blocks)
