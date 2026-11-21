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

import typing as typ

from rust_prover_tools.kani_log.budget import RecordBudget, retention_issue
from rust_prover_tools.kani_log.issues import Issue, IssueKind
from rust_prover_tools.kani_log.model import HarnessBlock
from rust_prover_tools.kani_log.other_counts import parse_other_pairs
from rust_prover_tools.kani_log.reconciliation import reconcile
from rust_prover_tools.kani_log.syntax import (
    CHECK_FIELDS,
    CHECK_STATUSES,
    FAILED_LOCATION,
    FailedCheck,
    PropertyResult,
)

if typ.TYPE_CHECKING:
    import re

__all__ = ("BlockCursor", "Issue", "IssueKind")


class BlockCursor:
    """Accumulate and reconcile the facts of one ``Checking harness`` block.

    Attributes
    ----------
    block : HarnessBlock
        The block being filled. Mutable by design: counts are only
        reconcilable once the whole block has been read.
    """

    def __init__(self, budget: RecordBudget | None = None) -> None:
        """Create a cursor with no block attached."""
        self._budget = budget or RecordBudget()
        self.block = HarnessBlock(name="")
        self._check_id: str | None = None
        self._check_values: dict[str, str] = {}
        self._awaiting_failure: str | None = None
        self._identifiers: set[str] = set()
        self._checks_truncated = False
        self._failures_truncated = False

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
        self._identifiers = set()
        self._checks_truncated = False
        self._failures_truncated = False

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
        refusal = self._check_refusal(identifier, values, missing)
        if refusal is not None:
            return [refusal]
        return self._retain_property(identifier, values)

    def _check_refusal(
        self,
        identifier: str,
        values: dict[str, str],
        missing: list[str],
    ) -> Issue | None:
        """Return why a completed check cannot be retained, if it cannot."""
        if missing:
            return Issue(
                IssueKind.STRUCTURAL,
                f"check {identifier!r} is missing {', '.join(missing)}",
            )
        if values["Status"] not in CHECK_STATUSES:
            return Issue(
                IssueKind.STRUCTURAL,
                f"check {identifier!r} has unknown status {values['Status']!r}",
            )
        if identifier in self._identifiers:
            return Issue(
                IssueKind.STRUCTURAL,
                f"check {identifier!r} is reported more than once",
            )
        return None

    def _retain_property(
        self,
        identifier: str,
        values: dict[str, str],
    ) -> list[Issue]:
        """Retain one unique property within the block's budget."""
        if not self._budget.take():
            if self._checks_truncated:
                return []
            self._checks_truncated = True
            return [retention_issue("check")]
        self._identifiers.add(identifier)
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
            self._retain_failure(FailedCheck(message, None, None))
            return False
        self._retain_failure(FailedCheck(message, match["path"], int(match["line"])))
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
            pairs = parse_other_pairs(match["others"])
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
        try:
            pairs = parse_other_pairs(match["others"])
        except ValueError:
            return [
                Issue(
                    IssueKind.CONTRADICTION,
                    f"the block for {self.block.name!r} has a malformed "
                    "cover-result list",
                )
            ]
        self.block.covered_other_counts = pairs
        counts = {kind: count for count, kind in pairs}
        self.block.covered_unreachable = counts.get("unreachable")
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
        if self._awaiting_failure is not None:
            # The stream ended before a location line arrived. Keep the
            # message as an unlocated failure rather than dropping the last
            # explanation the verifier produced.
            message = self._awaiting_failure
            self._awaiting_failure = None
            self._retain_failure(FailedCheck(message, None, None))
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
        if self._failures_truncated:
            reasons.append(retention_issue("failure"))
        return [*reasons, *reconcile(self.block)]

    def _retain_failure(self, failure: FailedCheck) -> None:
        """Retain one failure within the block's budget."""
        if self._budget.take():
            self.block.failures.append(failure)
        else:
            self._failures_truncated = True
