"""Name the reader's position and derive the facts that depend on it.

The reader's state is a small closed set, and several answers — whether the log
completed, what format it declared, why it is incomplete — are pure functions
of that position and a few accumulated facts. Keeping them here lets the
reader stay a line dispatcher and makes each answer testable without a log.

Examples
--------
>>> is_complete(State.BETWEEN, banner="0.67.0", has_manual=True, is_list=False)
True
>>> truncation_reason((), banner="0.67.0", complete=False)
'the log has not reported a complete result'
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

from rust_prover_tools.kani_log.blocks import Issue, IssueKind
from rust_prover_tools.kani_log.model import (
    HarnessBlock,
    KaniLogFacts,
    ManualSummary,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "ReaderSnapshot",
    "State",
    "format_name",
    "incomplete_issue",
    "is_complete",
    "truncation_reason",
)


class State(enum.Enum):
    """Where the cursor is in the report's nested structure."""

    BEFORE_BANNER = enum.auto()
    BETWEEN = enum.auto()
    BLOCK = enum.auto()
    RESULTS = enum.auto()
    SUMMARY = enum.auto()
    MANUAL = enum.auto()
    UNSUPPORTED = enum.auto()
    DONE = enum.auto()


def format_name(
    state: State,
    *,
    has_blocks: bool,
    has_manual: bool,
    is_list: bool,
) -> str | None:
    """Name the log's format once the log has stated it."""
    if is_list:
        return "inventory"
    if has_blocks or has_manual:
        return "regular"
    return None


def is_complete(
    state: State,
    *,
    banner: str | None,
    has_manual: bool,
    is_list: bool,
) -> bool:
    """Report whether the report reached a final summary or a list marker."""
    if banner is None:
        return False
    if state is State.DONE:
        return True
    return state is State.BETWEEN and (has_manual or is_list)


def truncation_reason(
    issues: cabc.Sequence[Issue],
    *,
    banner: str | None,
    complete: bool,
) -> str | None:
    """Return why the log is incomplete, when it is."""
    recorded = next(
        (issue.message for issue in issues if issue.kind is IssueKind.TRUNCATION),
        None,
    )
    if recorded is not None:
        return recorded
    if banner is not None and not complete:
        return "the log has not reported a complete result"
    return None


def incomplete_issue(state: State, block_name: str) -> Issue | None:
    """Return why an unfinished report stopped, or `None` at a boundary.

    Parameters
    ----------
    state : State
        Reader position when the log ended.
    block_name : str
        Name of the block being read, used by the message.

    Returns
    -------
    Issue | None
        A truncation issue for a report cut inside a block or the manual
        summary; `None` when the log ended between reports, where the summary
        reconciliation supplies the reason instead.
    """
    if state in {State.BLOCK, State.RESULTS, State.SUMMARY}:
        return Issue(
            IssueKind.TRUNCATION,
            f"the log is truncated inside the block for {block_name!r}",
        )
    if state is State.MANUAL:
        return Issue(
            IssueKind.TRUNCATION,
            "the log is truncated: the manual harness summary has no counts",
        )
    return None


@dc.dataclass(frozen=True, slots=True)
class ReaderSnapshot:
    """The reader's accumulated state, rendered as partial facts.

    Attributes
    ----------
    state : State
        Cursor position when the snapshot was taken.
    banner : str | None
        Release named by the banner, when one was seen.
    blocks : tuple[HarnessBlock, ...]
        Harness blocks started so far.
    manual : ManualSummary | None
        Final manual summary, when one was read.
    unsupported : tuple[str, ...]
        Constructs Kani named in its pre-flight warning.
    is_list : bool
        Whether the log declared itself an inventory.
    issues : tuple[Issue, ...]
        Recorded refusals, in arrival order.
    last_progress : str | None
        Last structurally recognised report line.
    exit_code : int | None
        Raw child status, once `finish` has been called.
    """

    state: State
    banner: str | None
    blocks: tuple[HarnessBlock, ...]
    manual: ManualSummary | None
    unsupported: tuple[str, ...]
    is_list: bool
    issues: tuple[Issue, ...]
    last_progress: str | None
    exit_code: int | None

    def facts(self) -> KaniLogFacts:
        """Render the snapshot as the public partial-facts value."""
        complete = is_complete(
            self.state,
            banner=self.banner,
            has_manual=self.manual is not None,
            is_list=self.is_list,
        )
        return KaniLogFacts(
            banner_version=self.banner,
            format=format_name(
                self.state,
                has_blocks=bool(self.blocks),
                has_manual=self.manual is not None,
                is_list=self.is_list,
            ),
            selection_records=tuple(block.name for block in self.blocks),
            verdicts=tuple(
                (block.name, block.verdict)
                for block in self.blocks
                if block.verdict is not None
            ),
            contradictions=tuple(
                issue.message
                for issue in self.issues
                if issue.kind is IssueKind.CONTRADICTION
            ),
            summary_errors=tuple(
                issue.message
                for issue in self.issues
                if issue.kind is IssueKind.SUMMARY
            ),
            truncation_reason=truncation_reason(
                self.issues,
                banner=self.banner,
                complete=complete,
            ),
            unsupported_constructs=self.unsupported,
            completed=complete,
            exit_code=self.exit_code,
            last_progress=self.last_progress,
        )
