"""Walk one Kani log line by line with the grammar in ``syntax``.

The reader records a ``KaniLogError`` reason wherever a line is not one the
grammar accounts for, so facts stay available when a proof is stopped
mid-report while `finish` still fails closed. The state machine derives from
Combobulate's reader at commit ``9dd6e3c`` under the ISC licence.

Examples
--------
>>> reader = KaniLogReader()
>>> reader.feed_line("stdout", "Kani Rust Verifier 0.67.0 (cargo plugin)")
>>> reader.facts.banner_version
'0.67.0'
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.blocks import BlockCursor, Issue
from rust_prover_tools.kani_log.budget import (
    MAX_RETAINED_RECORDS,
    IssueBudget,
    RecordBudget,
    retention_issue,
)
from rust_prover_tools.kani_log.issues import second_banner_issue, structural_issue
from rust_prover_tools.kani_log.model import (
    HarnessBlock,
    KaniLog,
    KaniLogFacts,
    ManualSummary,
)
from rust_prover_tools.kani_log.registry import grammar_for
from rust_prover_tools.kani_log.state import (
    ReaderSnapshot,
    State,
    done_line,
    incomplete_issue,
)
from rust_prover_tools.kani_log.summary import reconcile_report
from rust_prover_tools.kani_log.syntax import (
    BANNER,
    CBMC_FAILED,
    CHECK,
    COMPLETE,
    COVER,
    FAILED_CHECK,
    FAILED_FOR,
    FIELD,
    HARNESS_START,
    LIST_WRITTEN,
    SOLVER_COMMAND,
    TIMED_OUT,
    TOTAL,
    UNSUPPORTED_ENTRY,
    UNSUPPORTED_PRELUDE,
    UNSUPPORTED_REACHABLE,
    UNWINDING_FAILED,
    VERIFICATION,
    VERIFICATION_TIME,
    KaniLogError,
    LineStreamName,
    is_noise,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("KaniLogReader",)


class KaniLogReader:
    """An incremental reader for one Kani 0.67.0 log.

    Feed decoded lines, then call `finish`. `facts` never raises for an
    incomplete log, so a supervisor can attribute a stopped harness's evidence.
    """

    def __init__(self) -> None:
        """Start before the banner with no accumulated report."""
        self._state = State.BEFORE_BANNER
        self._line_number = 0
        self._banner: str | None = None
        self._blocks: list[HarnessBlock] = []
        self._blocks_exhausted = False
        self._manual_exhausted = False
        self._unsupported_exhausted = False
        self._cursor = BlockCursor(RecordBudget())
        self._manual: ManualSummary | None = None
        self._manual_failed: list[str] = []
        self._unsupported: list[str] = []
        self._is_list = False
        self._budget = IssueBudget()
        self._last_progress: str | None = None
        self._exit_code: int | None = None
        self._handlers: dict[State, cabc.Callable[[str], None]] = {
            State.BETWEEN: self._between_line,
            State.BLOCK: self._block_line,
            State.RESULTS: self._results_line,
            State.SUMMARY: self._summary_line,
            State.MANUAL: self._manual_line,
            State.UNSUPPORTED: self._unsupported_line,
            State.DONE: self._done_line,
        }

    def feed_line(self, stream: LineStreamName, text: str) -> None:
        """Consume one decoded output line.

        A standard-error line before the banner is Cargo preamble, not a
        malformed log, because Cuprum interleaves the streams. An unregistered
        banner raises `UnsupportedKaniGrammarError` immediately.
        """
        self._line_number += 1
        if self._state is State.BEFORE_BANNER:
            self._banner_line(stream, text)
            return
        if self._cursor.awaiting_failure and self._cursor.try_failure_location(text):
            return
        self._handlers[self._state](text)

    def finish(self, exit_code: int) -> KaniLog:
        """Complete the log and return its reconciled facts.

        Raises
        ------
        KaniLogError
            The first structural, contradiction, summary, or truncation reason.
        """
        self._exit_code = exit_code
        if self._banner is None:
            self._budget.record((
                structural_issue("the log does not begin with a Kani banner"),
            ))
            raise KaniLogError(self._budget.issues[0].message)
        if self._state not in {State.BLOCK, State.RESULTS, State.SUMMARY}:
            self._close_block()
        self._finish_incomplete()
        if self._budget.issues:
            raise KaniLogError(self._budget.issues[0].message)
        self._budget.record(
            reconcile_report(
                self._blocks,
                self._manual,
                is_inventory=self._is_list,
            )
        )
        if self._budget.issues:
            raise KaniLogError(self._budget.issues[0].message)
        return KaniLog(
            self._banner,
            "inventory" if self._is_list else "regular",
            tuple(self._blocks),
            self._manual,
            tuple(self._unsupported),
            exit_code,
        )

    @property
    def retained_issues(self) -> tuple[Issue, ...]:
        """The recorded refusals, bounded by `MAX_RETAINED_ISSUES`."""
        return self._budget.issues

    @property
    def facts(self) -> KaniLogFacts:
        """The facts available so far, without raising for truncation."""
        return ReaderSnapshot(
            state=self._state,
            banner=self._banner,
            blocks=tuple(self._blocks),
            manual=self._manual,
            unsupported=tuple(self._unsupported),
            is_list=self._is_list,
            issues=self._budget.issues,
            last_progress=self._last_progress,
            exit_code=self._exit_code,
        ).facts()

    def _banner_line(self, stream: LineStreamName, text: str) -> None:
        """Consume the opening banner, or tolerate standard-error preamble."""
        match = BANNER.match(text)
        if match is not None:
            grammar_for(match["version"])
            self._banner = match["version"]
            self._state = State.BETWEEN
            self._last_progress = text
            return
        if stream == "stderr" or not text.strip():
            return
        self._budget.record((
            structural_issue("the log does not begin with a Kani banner"),
        ))
        self._state = State.DONE

    def _between_line(self, text: str) -> None:
        """Consume a line outside any harness block."""
        if text == "Manual Harness Summary:":
            self._begin_manual()
        elif text == UNSUPPORTED_PRELUDE:
            self._state = State.UNSUPPORTED
        elif text.startswith(LIST_WRITTEN):
            self._is_list = True
            self._last_progress = text
        elif (match := HARNESS_START.match(text)) is not None:
            self._begin_block(match["name"])
        elif BANNER.match(text) is not None:
            self._budget.record((second_banner_issue(self._line_number),))

    def _block_line(self, text: str) -> None:
        """Consume a line inside a harness block."""
        if self._block_section(text) or self._block_verdict(text):
            return
        if self._block_marker(text):
            return
        if (match := HARNESS_START.match(text)) is not None:
            self._close_block()
            self._begin_block(match["name"])
            return
        if BANNER.match(text) is not None:
            self._budget.record((second_banner_issue(self._line_number),))
            return
        if self._cursor.needs_summary:
            # The solver's transcript sits between the header and the report.
            return
        self._budget.record((
            structural_issue(
                f"line {self._line_number}: unrecognised line in the block for "
                f"{self._cursor.block.name!r}: {text!r}"
            ),
        ))

    def _block_section(self, text: str) -> bool:
        """Handle a block's section delimiters and its closing time line."""
        if text == "RESULTS:":
            self._cursor.block.has_results = True
            self._state = State.RESULTS
            self._last_progress = text
            return True
        if text == "SUMMARY:":
            self._state = State.SUMMARY
            self._last_progress = text
            return True
        if VERIFICATION_TIME.match(text):
            self._cursor.block.seen_verification_time = True
            self._close_block()
            self._state = State.BETWEEN
            self._last_progress = text
            return True
        if text == "Manual Harness Summary:":
            self._close_block()
            self._begin_manual()
            return True
        return False

    def _block_verdict(self, text: str) -> bool:
        """Record one ``VERIFICATION:`` verdict, refusing a second."""
        match = VERIFICATION.match(text)
        if match is None:
            return False
        self._budget.record(self._cursor.add_verdict(match["verdict"], match["note"]))
        self._last_progress = text
        return True

    def _block_marker(self, text: str) -> bool:
        """Handle a block marker or build chatter."""
        block = self._cursor.block
        if text == UNWINDING_FAILED:
            block.unwinding_reported = True
            return True
        if text.startswith(TIMED_OUT):
            block.timed_out = True
            return True
        if text.startswith(SOLVER_COMMAND):
            block.solver_command = text
            return True
        return text in {CBMC_FAILED, UNSUPPORTED_REACHABLE} or is_noise(text)

    def _results_line(self, text: str) -> None:
        """Consume one line of the ``RESULTS:`` section."""
        if (match := CHECK.match(text)) is not None:
            self._budget.record(self._cursor.flush_check())
            self._cursor.begin_check(match["identifier"])
            self._last_progress = text
            return
        if (field := FIELD.match(text)) is not None:
            refusal = self._cursor.add_check_field(field["key"], field["value"])
            if refusal is not None:
                self._budget.record((
                    structural_issue(f"line {self._line_number}: {refusal}: {text!r}"),
                ))
            return
        if not text.strip():
            return
        self._budget.record(self._cursor.flush_check())
        self._state = State.BLOCK
        self._block_line(text)

    def _summary_line(self, text: str) -> None:
        """Consume one line of the ``SUMMARY:`` section."""
        if (total := TOTAL.match(text)) is not None:
            self._budget.record(self._cursor.add_total(total))
            self._last_progress = text
        elif (cover := COVER.match(text)) is not None:
            self._budget.record(self._cursor.add_cover(cover))
            self._last_progress = text
        elif (message := FAILED_CHECK.match(text)) is not None:
            self._cursor.add_failure(message["message"])
            self._last_progress = text
        elif VERIFICATION.match(text) or text == UNSUPPORTED_REACHABLE:
            self._state = State.BLOCK
            self._block_line(text)
        elif text.strip():
            self._budget.record((
                structural_issue(
                    f"line {self._line_number}: unrecognised summary line for "
                    f"{self._cursor.block.name!r}: {text!r}"
                ),
            ))

    def _manual_line(self, text: str) -> None:
        """Consume one line of the final manual harness summary."""
        if (failure := FAILED_FOR.match(text)) is not None:
            if len(self._manual_failed) < MAX_RETAINED_RECORDS:
                self._manual_failed.append(failure["name"])
            elif not self._manual_exhausted:
                self._manual_exhausted = True
                self._budget.record((retention_issue("manual failure name"),))
            self._last_progress = text
            return
        if (complete := COMPLETE.match(text)) is not None:
            self._manual = ManualSummary(
                int(complete["verified"]),
                int(complete["failures"]),
                int(complete["total"]),
                tuple(self._manual_failed),
            )
            self._state = State.DONE
            self._last_progress = text
            return
        if text.strip():
            self._budget.record((
                structural_issue(
                    f"line {self._line_number}: unrecognised manual summary "
                    f"line: {text!r}"
                ),
            ))

    def _unsupported_line(self, text: str) -> None:
        """Consume one entry of the pre-flight unsupported-construct list."""
        if (entry := UNSUPPORTED_ENTRY.match(text)) is not None:
            if len(self._unsupported) < MAX_RETAINED_RECORDS:
                self._unsupported.append(entry["name"])
            elif not self._unsupported_exhausted:
                self._unsupported_exhausted = True
                self._budget.record((retention_issue("unsupported construct"),))
            return
        if text.strip():
            self._state = State.BETWEEN
            self._between_line(text)

    def _done_line(self, text: str) -> None:
        """Ignore chatter after the summary, but refuse a second report."""
        done_line(text, self._line_number, self._budget.record)

    def _begin_block(self, name: str) -> None:
        """Start a new harness block at ``name``."""
        self._cursor.start(name)
        if len(self._blocks) < MAX_RETAINED_RECORDS:
            self._blocks.append(self._cursor.block)
        elif not self._blocks_exhausted:
            self._blocks_exhausted = True
            self._budget.record((retention_issue("harness block"),))
        self._state = State.BLOCK
        self._last_progress = f"Checking harness {name}..."

    def _begin_manual(self) -> None:
        """Start the final manual harness summary."""
        self._manual_failed = []
        self._manual_exhausted = False
        self._state = State.MANUAL
        self._last_progress = "Manual Harness Summary:"

    def _close_block(self) -> None:
        """Reconcile the current block and detach it."""
        if not self._cursor.started:
            return
        self._budget.record(self._cursor.finish())
        self._cursor.start("")

    def _finish_incomplete(self) -> None:
        """Record why the report did not reach its final summary."""
        if self._state is State.UNSUPPORTED:
            self._state = State.BETWEEN
        issue = incomplete_issue(self._state, self._cursor.block.name)
        if issue is not None:
            self._budget.record((issue,))

    def _progress(self, text: str) -> None:
        """Remember the last structurally recognised report line."""
        self._last_progress = text
