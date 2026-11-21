"""Accumulate one harness block and reconcile its counts.

A ``Checking harness`` block is the unit Kani reports on: its header,
``RESULTS:`` list, ``SUMMARY:`` counts and verdict. This module owns one
block's accumulation so the reader stays a line dispatcher and the
reconciliation rules stay readable. The rules derive from Combobulate's reader
at commit ``9dd6e3c`` under the ISC licence in ``ATTRIBUTION.md``.

Examples
--------
>>> cursor = BlockCursor()
>>> cursor.start("harnesses::x")
>>> cursor.block.name
'harnesses::x'
"""

from __future__ import annotations

import re
import typing as typ

from rust_prover_tools.kani_log.issues import Issue, IssueKind
from rust_prover_tools.kani_log.model import HarnessBlock
from rust_prover_tools.kani_log.syntax import (
    CHECK_FIELDS,
    CHECK_STATUSES,
    FAILED_LOCATION,
    OTHER_STATUS,
    FailedCheck,
    PropertyResult,
)

__all__ = ("BlockCursor", "Issue", "IssueKind")


class BlockCursor:
    """Accumulate and reconcile the facts of one ``Checking harness`` block.

    Attributes
    ----------
    block : HarnessBlock
        The block being filled. Mutable by design: counts are only
        reconcilable once the whole block has been read.
    """

    def __init__(self) -> None:
        """Create a cursor with no block attached."""
        self.block = HarnessBlock(name="")
        self._check_id: str | None = None
        self._check_values: dict[str, str] = {}
        self._awaiting_failure: str | None = None

    @property
    def started(self) -> bool:
        """Report whether a block has been started."""
        return bool(self.block.name)

    def start(self, name: str) -> None:
        """Begin a block for one harness name."""
        self.block = HarnessBlock(name=name)
        self._check_id = None
        self._check_values = {}
        self._awaiting_failure = None

    @property
    def needs_summary(self) -> bool:
        """Report whether the block has yet to report any counts.

        The solver's own transcript sits between the header and the report.
        Until the block has a verdict, a results list, or a summary count,
        unexplained lines belong to CBMC rather than to Kani.
        """
        return (
            self.block.verdict is None
            and not self.block.has_results
            and self.block.failed_count is None
        )

    def begin_check(self, identifier: str) -> None:
        """Begin a ``Check N:`` entry for ``identifier``."""
        self._check_id = identifier
        self._check_values = {}

    def add_check_field(self, key: str, value: str) -> str | None:
        """Record one field of the pending check, or name the refusal."""
        if self._check_id is None:
            return "field outside a check"
        if key in self._check_values:
            return f"check {self._check_id!r} repeats field {key!r}"
        self._check_values[key] = value
        return None

    def flush_check(self) -> list[Issue]:
        """Finish the pending check, returning any structural reasons."""
        if self._check_id is None:
            return []
        identifier, values = self._check_id, self._check_values
        self._check_id, self._check_values = None, {}
        missing = [key for key in CHECK_FIELDS if key not in values]
        if missing:
            return [
                Issue(
                    IssueKind.STRUCTURAL,
                    f"check {identifier!r} is missing {', '.join(missing)}",
                )
            ]
        if values["Status"] not in CHECK_STATUSES:
            return [
                Issue(
                    IssueKind.STRUCTURAL,
                    f"check {identifier!r} has unknown status {values['Status']!r}",
                )
            ]
        self.block.properties.append(
            PropertyResult(
                identifier=identifier,
                status=values["Status"],
                description=values["Description"].strip('"'),
            )
        )
        return []

    @property
    def awaiting_failure(self) -> bool:
        """Report whether a ``Failed Checks:`` message awaits its location."""
        return self._awaiting_failure is not None

    def add_failure(self, message: str) -> None:
        """Record a failure message whose location line may follow."""
        self._awaiting_failure = message

    def try_failure_location(self, text: str) -> bool:
        """Attach a location line to the awaiting failure, or close it unlocated.

        Returns
        -------
        bool
            `True` when ``text`` was the location line and has been consumed;
            `False` when it was not, so the caller continues normally.
        """
        message = typ.cast("str", self._awaiting_failure)
        match = FAILED_LOCATION.match(text)
        self._awaiting_failure = None
        if match is None:
            self.block.failures.append(FailedCheck(message, None, None))
            return False
        self.block.failures.append(
            FailedCheck(message, match["path"], int(match["line"]))
        )
        return True

    def add_total(self, match: re.Match[str]) -> list[Issue]:
        """Record the failure-count line, refusing a second one."""
        if self.block.failed_count is not None:
            return [
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {self.block.name!r} has two failure counts",
                )
            ]
        self.block.failed_count = int(match["failed"])
        self.block.total_count = int(match["total"])
        try:
            pairs = _other_pairs(match["others"])
        except ValueError:
            return [
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {self.block.name!r} has a malformed "
                    "other-result list",
                )
            ]
        self.block.other_counts = pairs
        self.block.other_count, self.block.other_kind = pairs[0] if pairs else (0, None)
        return []

    def add_cover(self, match: re.Match[str]) -> list[Issue]:
        """Record the cover-count line, refusing a second one."""
        if self.block.covered_total is not None:
            return [
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {self.block.name!r} has two cover counts",
                )
            ]
        self.block.covered_satisfied = int(match["satisfied"])
        self.block.covered_total = int(match["total"])
        if match["unreachable"] is not None:
            self.block.covered_unreachable = int(match["unreachable"])
        return []

    def add_verdict(self, verdict: str, note: str | None) -> list[Issue]:
        """Record the verdict, refusing a block that states two."""
        if self.block.verdict is not None:
            return [
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {self.block.name!r} reports two verification "
                    "results",
                )
            ]
        self.block.verdict = verdict
        self.block.panics_as_expected = note == (
            "(encountered one or more panics as expected)"
        )
        return []

    def finish(self) -> list[Issue]:
        """Reconcile the block and return every reason it cannot be believed."""
        reasons = self.flush_check()
        if self.block.verdict is None:
            reasons.append(
                Issue(
                    IssueKind.STRUCTURAL,
                    f"the block for {self.block.name!r} ends without a "
                    "verification result",
                )
            )
            return reasons
        if self.block.timed_out and not self.block.has_results:
            # A timeout aborts CBMC before it reports any check, so the block
            # has no results, no summary, and no verification time. Its verdict
            # alone is the fact.
            return reasons
        return [*reasons, *self._reconcile()]

    def _reconcile(self) -> list[Issue]:
        """Check the block's counts against the checks and failures it lists."""
        if not self.block.seen_verification_time:
            return [
                Issue(
                    IssueKind.TRUNCATION,
                    f"the log is truncated: the block for {self.block.name!r} has "
                    "no verification time",
                )
            ]
        if self.block.failed_count is None or self.block.total_count is None:
            return [
                Issue(
                    IssueKind.STRUCTURAL,
                    f"the block for {self.block.name!r} has no summary",
                )
            ]
        return [
            *self._reconcile_checks(),
            *self._reconcile_failures(),
            *self._reconcile_covers(),
            *self._reconcile_other(),
        ]

    def _reconcile_checks(self) -> list[Issue]:
        """Check the failure count against the listed checks."""
        name = self.block.name
        checks = self.block.checks
        reasons: list[Issue] = []
        if len(checks) != self.block.total_count:
            reasons.append(
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {name!r} counts {self.block.total_count} "
                    f"checks but lists {len(checks)}",
                )
            )
        failed = sum(1 for item in checks if item.status == "FAILURE")
        if failed != self.block.failed_count:
            reasons.append(
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {name!r} counts {self.block.failed_count} "
                    f"failures but lists {failed}",
                )
            )
        if self._unexpected_failure(failed):
            reasons.append(
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {name!r} reports SUCCESSFUL but lists "
                    f"{failed} failed check(s)",
                )
            )
        return reasons

    def _unexpected_failure(self, failed: int) -> bool:
        """Whether a successful verdict contradicts a non-panic failure."""
        if self.block.verdict != "SUCCESSFUL":
            return False
        return bool(failed) and not self.block.panics_as_expected

    def _reconcile_failures(self) -> list[Issue]:
        """Check the failure explanations against the failure count.

        Every failed check is explained once, and the explanation carries the
        source location Kani attributes it to. Requiring both makes deleting a
        ``Failed Checks:`` line, or the ``File:`` line beneath it, a structural
        error rather than a quiet loss of the one fact a record could not
        invent.
        """
        name = self.block.name
        reasons: list[Issue] = []
        if len(self.block.failures) != self.block.failed_count:
            reasons.append(
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {name!r} counts {self.block.failed_count} "
                    f"failures but explains {len(self.block.failures)}",
                )
            )
        return reasons

    def _reconcile_covers(self) -> list[Issue]:
        """Check the cover counts against the listed witnesses."""
        name = self.block.name
        covers = self.block.covers
        reasons: list[Issue] = []
        if self.block.covered_total is None and covers:
            reasons.append(
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {name!r} lists {len(covers)} covers but has "
                    "no cover count",
                )
            )
        if (
            self.block.covered_total is not None
            and len(covers) != self.block.covered_total
        ):
            reasons.append(
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {name!r} counts {self.block.covered_total} "
                    f"covers but lists {len(covers)}",
                )
            )
        satisfied = sum(1 for item in covers if item.status == "SATISFIED")
        if (
            self.block.covered_satisfied is not None
            and satisfied != self.block.covered_satisfied
        ):
            reasons.append(
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {name!r} counts "
                    f"{self.block.covered_satisfied} satisfied covers but lists "
                    f"{satisfied}",
                )
            )
        unreachable = sum(1 for item in covers if item.status == "UNREACHABLE")
        if (
            self.block.covered_unreachable is not None
            and self.block.covered_unreachable != unreachable
        ):
            reasons.append(
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {name!r} counts "
                    f"{self.block.covered_unreachable} unreachable covers but "
                    f"lists {unreachable}",
                )
            )
        return reasons

    def _reconcile_other(self) -> list[Issue]:
        """Check every parenthesized other-result pair against the checks."""
        pairs = self.block.other_counts or (
            ((self.block.other_count, self.block.other_kind),)
            if self.block.other_kind is not None
            else ()
        )
        reasons: list[Issue] = []
        for count, kind in pairs:
            status = OTHER_STATUS.get(kind, "")
            observed = sum(1 for item in self.block.checks if item.status == status)
            if count != observed:
                reasons.append(
                    Issue(
                        IssueKind.CONTRADICTION,
                        f"the block for {self.block.name!r} counts {count} "
                        f"{kind} results but lists {observed}",
                    )
                )
        return reasons


def _other_pairs(text: str | None) -> tuple[tuple[int, str], ...]:
    """Parse a comma-separated list of ``N kind`` other-result pairs."""
    if not text:
        return ()
    pairs: list[tuple[int, str]] = []
    for part in text.split(","):
        match = re.fullmatch(r"\s*(\d+)\s+([a-z]+)\s*", part)
        if match is None:
            msg = f"malformed other-result pair {part!r}"
            raise ValueError(msg)
        pairs.append((int(match[1]), match[2]))
    return tuple(pairs)
