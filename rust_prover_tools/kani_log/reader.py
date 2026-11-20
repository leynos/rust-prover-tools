"""Walk one Kani log line by line with the grammar from :mod:`.syntax`.

This is the cursor half of the extractor: it consumes one report construct at a
time and raises :class:`KaniLogError` wherever the next line is not one the
grammar accounts for.  The report readers live in :mod:`.reports`, which keeps
each module within the repository's size limit while leaving this one as the
part a new Kani release is compared against.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log import reports
from rust_prover_tools.kani_log.model import HarnessBlock, ManualSummary
from rust_prover_tools.kani_log.syntax import (
    BANNER,
    CBMC_FAILED,
    HARNESS_START,
    OTHER_STATUS,
    SOLVER_COMMAND,
    TIMED_OUT,
    UNSUPPORTED_REACHABLE,
    UNWINDING_FAILED,
    VERIFICATION,
    VERIFICATION_TIME,
    FailedCheck,
    KaniLogError,
    PropertyResult,
)

if typ.TYPE_CHECKING:
    import re

__all__ = ("Reader",)


@dc.dataclass(slots=True)
class _BlockBuilder:
    """Accumulate one harness block before it becomes a frozen record."""

    name: str
    properties: list[PropertyResult] = dc.field(default_factory=list)
    failures: list[FailedCheck] = dc.field(default_factory=list)
    failed_count: int | None = None
    total_count: int | None = None
    other_count: int = 0
    other_kind: str | None = None
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
    def checks(self) -> tuple[PropertyResult, ...]:
        """The accumulated checks that are not covers."""
        return tuple(item for item in self.properties if not item.is_cover)

    @property
    def covers(self) -> tuple[PropertyResult, ...]:
        """The accumulated cover properties."""
        return tuple(item for item in self.properties if item.is_cover)

    def build(self) -> HarnessBlock:
        """Freeze the accumulated fields into their public record."""
        return HarnessBlock(
            name=self.name,
            properties=tuple(self.properties),
            failures=tuple(self.failures),
            failed_count=self.failed_count,
            total_count=self.total_count,
            other_count=self.other_count,
            other_kind=self.other_kind,
            covered_satisfied=self.covered_satisfied,
            covered_total=self.covered_total,
            covered_unreachable=self.covered_unreachable,
            verdict=self.verdict,
            panics_as_expected=self.panics_as_expected,
            solver_command=self.solver_command,
            seen_verification_time=self.seen_verification_time,
            timed_out=self.timed_out,
            unwinding_reported=self.unwinding_reported,
            has_results=self.has_results,
        )


class Reader:
    """Walk the log once, keeping the cursor position for error messages."""

    def __init__(self, lines: list[str]) -> None:
        """Start at the first line of a log already split into lines."""
        self._lines = lines
        self._index = 0

    @property
    def exhausted(self) -> bool:
        """Report whether every line has been consumed."""
        return self._index >= len(self._lines)

    def where(self) -> str:
        """Describe the current position as a one-based line number."""
        return f"line {self._index + 1}"

    def peek(self) -> str:
        """Return the current line without consuming it."""
        return self._lines[self._index]

    def take(self) -> str:
        """Consume and return the current line, or refuse a truncated log.

        Returns
        -------
        str
            The consumed line.

        Raises
        ------
        KaniLogError
            If the cursor is already past the end of the log.

        """
        if self.exhausted:
            msg = f"{self.where()}: the log ends where more output was required"
            raise KaniLogError(msg)
        line = self._lines[self._index]
        self._index += 1
        return line

    def banner(self) -> tuple[str, str] | None:
        """Consume the version banner if the log opens with one."""
        if self.exhausted:
            return None
        match = BANNER.match(self._lines[0])
        if match is None:
            return None
        self.take()
        return match["version"], match["kind"]

    def results(self, builder: _BlockBuilder) -> None:
        """Consume the ``RESULTS:`` section into ``builder``."""
        reports.results(self, builder)

    def summary(self, builder: _BlockBuilder) -> None:
        """Consume the ``SUMMARY:`` section into ``builder``."""
        reports.summary(self, builder)

    def verification(
        self,
        builder: _BlockBuilder,
        match: re.Match[str],
    ) -> None:
        """Record one already-matched ``VERIFICATION:`` line on ``builder``."""
        reports.verification(self, builder, match)

    def manual(self) -> ManualSummary:
        """Consume the ``Manual Harness Summary`` that ends a complete run."""
        return reports.manual(self)

    def unsupported_constructs(self) -> list[str]:
        """Consume Kani's pre-flight list of constructs it cannot model."""
        return reports.unsupported_constructs(self)

    def block(self, match: re.Match[str]) -> HarnessBlock:
        """Consume one ``Checking harness`` block through its verification time.

        Parameters
        ----------
        match : re.Match[str]
            The already-matched harness header.

        Returns
        -------
        HarnessBlock
            The completed block.

        Raises
        ------
        KaniLogError
            If the block is truncated or contains an unrecognised report line.

        """
        builder = _BlockBuilder(name=match["name"])
        self.take()
        while not self.exhausted:
            line = self.peek()
            verdict = VERIFICATION.match(line)
            if line == "RESULTS:":
                self.take()
                self.results(builder)
            elif line == "SUMMARY:":
                self.take()
                self.summary(builder)
            elif verdict is not None:
                self.verification(builder, verdict)
            elif (ended := self._end_block(builder, line)) is not None:
                return ended
            elif self._note_marker(builder, line):
                continue
            elif _is_solver_transcript(builder):
                # The solver's own transcript sits between the harness header
                # and the report.  Its wording belongs to CBMC, not to Kani, so
                # it is skipped by position rather than by matching every line.
                self.take()
            else:
                msg = (
                    f"{self.where()}: unrecognised line in the block for "
                    f"{builder.name!r}: {line!r}"
                )
                raise KaniLogError(msg)
        msg = f"the log is truncated inside the block for {builder.name!r}"
        raise KaniLogError(msg)

    def _end_block(
        self,
        builder: _BlockBuilder,
        line: str,
    ) -> HarnessBlock | None:
        """Finish the block if ``line`` ends it or begins the next one.

        A second harness header ends the current block without being consumed:
        the outer loop reads it as the next block.  Only a timed-out block can
        end that way, because every block that ran has a verification time.
        """
        if VERIFICATION_TIME.match(line) is not None:
            self.take()
            builder.seen_verification_time = True
        elif line != "Manual Harness Summary:" and HARNESS_START.match(line) is None:
            return None
        _require_complete(builder)
        return builder.build()

    def _note_marker(self, builder: _BlockBuilder, line: str) -> bool:
        """Record a one-line block marker in ``builder`` and consume it.

        Returns
        -------
        bool
            Whether ``line`` was one of the markers this method knows.

        """
        if line == UNWINDING_FAILED:
            builder.unwinding_reported = True
        elif line.startswith(TIMED_OUT):
            builder.timed_out = True
        elif line.startswith(SOLVER_COMMAND):
            builder.solver_command = line
        elif line in {CBMC_FAILED, UNSUPPORTED_REACHABLE} or reports.is_noise(line):
            # Build and driver chatter carries no fact the analysis reads.
            pass
        else:
            return False
        self.take()
        return True


def _require_complete(builder: _BlockBuilder) -> None:
    """Reconcile one block's counts against the checks it listed.

    Parameters
    ----------
    builder : _BlockBuilder
        The block that is about to be finished.

    Raises
    ------
    KaniLogError
        If the block is missing a verdict, summary or location, or if any count
        disagrees with the listed checks.

    """
    if builder.verdict is None:
        msg = f"the block for {builder.name!r} ends without a verification result"
        raise KaniLogError(msg)
    if builder.timed_out and not builder.has_results:
        # A timeout aborts CBMC before it reports any check, so the block has no
        # results, summary or verification time.  Its verdict alone is the fact.
        return
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


def _is_solver_transcript(builder: _BlockBuilder) -> bool:
    """Report whether the block has not yet reached its own report."""
    if builder.verdict is not None:
        return False
    if builder.has_results:
        return False
    return builder.failed_count is None


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
    unlocated = [
        item for item in builder.failures if item.path is None or item.line is None
    ]
    if unlocated:
        msg = (
            f"the block for {builder.name!r} explains a failure without a "
            f"source location: {unlocated[0].message!r}"
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


def _require_other(
    builder: _BlockBuilder,
    checks: tuple[PropertyResult, ...],
) -> None:
    """Require the parenthesised "other" count to reconcile."""
    expected = OTHER_STATUS.get(builder.other_kind or "", "")
    observed_other = sum(1 for item in checks if item.status == expected)
    if builder.other_count not in {0, observed_other}:
        msg = (
            f"the block for {builder.name!r} counts {builder.other_count} "
            f"{builder.other_kind} results but lists {observed_other}"
        )
        raise KaniLogError(msg)
