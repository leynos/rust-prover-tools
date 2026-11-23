"""Extract per-harness facts from a Kani log, or refuse the log.

Unknown chatter *between* reports is accepted, so a Kani release that adds a
post-processing message does not break the parser.  Refusal is for structure
the parser cannot account for inside a report: a missing banner or ``Manual
Harness Summary``, a truncated or duplicated harness block, a block that ends
without its verdict, summary counts that disagree with the checks listed, or an
unrecognised line inside a report block.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log.model import HarnessBlock, KaniLog, ManualSummary
from rust_prover_tools.kani_log.reader import Reader, StreamingLines
from rust_prover_tools.kani_log.syntax import (
    BANNER,
    CHECK,
    FIELD,
    HARNESS_START,
    LIST_WRITTEN,
    SUPPORTED_KANI_VERSIONS,
    UNSUPPORTED_PRELUDE,
    VERIFICATION,
    KaniLogError,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import re

from rust_prover_tools.kani_log.unwinding import unwinding_checks_enabled

#: Largest number of harness blocks or unsupported constructs one log retains.
MAX_PARSER_RECORDS: typ.Final[int] = 10_000

__all__ = ("parse_kani_log", "parse_kani_log_lines", "unwinding_checks_enabled")


def parse_kani_log(text: str) -> KaniLog:
    """Read one Kani log into per-harness facts, or refuse it.

    Parameters
    ----------
    text : str
        The complete captured output of one Kani invocation, with standard
        output followed by standard error.

    Returns
    -------
    KaniLog
        The banner, output format, every harness block, the manual summary and
        the constructs Kani named as unsupported.

    Raises
    ------
    KaniLogError
        If the log is truncated, duplicated, interleaved, count-inconsistent,
        from an unsupported version, or otherwise not a complete Kani report.

    Examples
    --------
    A log without a banner cannot be attributed to a Kani release, so it is
    refused with ``structural error: the log does not begin with a Kani
    banner``.

    """
    return parse_kani_log_lines(_physical_lines(text))


def parse_kani_log_lines(lines: cabc.Iterable[str]) -> KaniLog:
    """Read a log supplied one line at a time, without holding it all.

    The parser still needs a small lookahead, so the source keeps a window
    rather than the whole log. A supervisor that spools a child's output can
    therefore parse an arbitrarily long log in bounded memory.

    Parameters
    ----------
    lines : cabc.Iterable[str]
        The log's physical lines, without terminators.

    Returns
    -------
    KaniLog
        The banner, output format, harness blocks, manual summary, and the
        unsupported constructs Kani named.

    Raises
    ------
    KaniLogError
        If the log is truncated, duplicated, interleaved, count-inconsistent,
        from an unsupported version, or otherwise not a complete Kani report.
    """
    reader = Reader(StreamingLines(lines))
    version = _require_supported_banner(reader)
    return _reconcile(version, _scan(reader))


def _physical_lines(text: str) -> list[str]:
    """Split a transcript only at physical line endings.

    ``str.splitlines`` also breaks on U+2028, U+0085 and the other Unicode line
    separators.  Kani prints such a character inside a quoted description as
    content, and splitting there would rejoin it as a newline the author never
    wrote, so a declared cover could stop matching its report.  A trailing
    newline yields no final empty line, as the reader expects.
    """
    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _require_supported_banner(reader: Reader) -> str:
    """Return the banner's release, refusing a log from another release."""
    banner = reader.banner()
    if banner is None:
        msg = "the log does not begin with a Kani banner"
        raise KaniLogError(msg)
    version = banner[0]
    if version not in SUPPORTED_KANI_VERSIONS:
        msg = (
            f"the log is from Kani {version}, but this parser reads "
            f"{list(SUPPORTED_KANI_VERSIONS)}"
        )
        raise KaniLogError(msg)
    return version


@dc.dataclass(frozen=True, slots=True)
class _ObservedLog:
    """The parts a scan collected before they are reconciled."""

    blocks: tuple[HarnessBlock, ...]
    manual: ManualSummary | None
    is_list: bool
    unsupported: tuple[str, ...]


def _scan(reader: Reader) -> _ObservedLog:
    """Read report markers until the log is exhausted."""
    blocks: list[HarnessBlock] = []
    manual: ManualSummary | None = None
    unsupported: list[str] = []
    is_list = False
    while not reader.exhausted:
        line = reader.peek()
        if line == "Manual Harness Summary:":
            if manual is not None:
                msg = f"{reader.where()}: a second manual harness summary"
                raise KaniLogError(msg)
            manual = reader.manual()
        elif line == UNSUPPORTED_PRELUDE:
            _extend_unsupported(reader, unsupported)
        elif line.startswith(LIST_WRITTEN):
            is_list = True
            reader.take()
        elif header := HARNESS_START.match(line):
            _append_block(reader, blocks, manual, header)
        elif BANNER.match(line) is not None:
            # A second banner means the capture is two invocations
            # concatenated.  The first run's facts describe only itself, so
            # returning them would silently drop whatever the second run
            # reported - including a failure after its banner.
            msg = (
                f"{reader.where()}: a second Kani banner, so the log holds "
                "more than one invocation"
            )
            raise KaniLogError(msg)
        else:
            _refuse_orphan_report_line(reader, line)
            reader.take()
    return _ObservedLog(
        tuple(blocks),
        manual,
        is_list,
        tuple(unsupported),
    )


def _append_block(
    reader: Reader,
    blocks: list[HarnessBlock],
    manual: ManualSummary | None,
    header: re.Match[str],
) -> None:
    """Append one block, refusing a log beyond the block budget."""
    if len(blocks) >= MAX_PARSER_RECORDS:
        msg = (
            f"{reader.where()}: the log reports more than "
            f"{MAX_PARSER_RECORDS} harness blocks"
        )
        raise KaniLogError(msg)
    _refuse_harness_after_manual(reader, manual)
    blocks.append(reader.block(header))


def _extend_unsupported(reader: Reader, unsupported: list[str]) -> None:
    """Append one unsupported list, refusing a log beyond the budget."""
    unsupported.extend(reader.unsupported_constructs())
    if len(unsupported) > MAX_PARSER_RECORDS:
        msg = (
            f"{reader.where()}: the log names more than "
            f"{MAX_PARSER_RECORDS} unsupported constructs"
        )
        raise KaniLogError(msg)


def _refuse_orphan_report_line(reader: Reader, line: str) -> None:
    """Refuse a report marker that no harness block owns.

    Discarding one would accept a malformed capture and return an earlier
    block's facts as though the report were complete.  A detached ``Check N:``
    row matters as much as a section marker: the block it was written into had
    already ended, so the failure it reports is never reconciled against a
    verdict, and the earlier clean block would be returned as the run's result.
    """
    if _is_report_line(line):
        msg = f"{reader.where()}: a report line outside a harness block"
        raise KaniLogError(msg)


def _is_report_line(line: str) -> bool:
    """Report whether ``line`` belongs to a harness report."""
    if line in {"RESULTS:", "SUMMARY:"}:
        return True
    return any(
        pattern.match(line) is not None
        for pattern in (
            VERIFICATION,
            CHECK,
            FIELD,
        )
    )


def _refuse_harness_after_manual(
    reader: Reader,
    manual: ManualSummary | None,
) -> None:
    """Refuse a harness block that follows the manual harness summary.

    The manual summary closes the report, so a block after it belongs to a
    later part of the log that the summary cannot describe.
    """
    if manual is not None:
        msg = f"{reader.where()}: a harness block follows the manual harness summary"
        raise KaniLogError(msg)


def _reconcile(version: str, seen: _ObservedLog) -> KaniLog:
    """Reconcile the log's parts, refusing any combination that cannot be real."""
    if seen.is_list:
        if seen.blocks or seen.manual is not None:
            msg = "the log claims to be both an inventory and a verification run"
            raise KaniLogError(msg)
        return KaniLog(version, "inventory", (), None, seen.unsupported)
    if not seen.blocks:
        msg = "the log reports no harness results"
        raise KaniLogError(msg)
    if seen.manual is None:
        msg = "the log is truncated: it has no manual harness summary"
        raise KaniLogError(msg)
    _require_unique_harnesses(seen.blocks)
    _require_summary_counts(seen.blocks, seen.manual)
    return KaniLog(version, "regular", seen.blocks, seen.manual, seen.unsupported)


def _require_unique_harnesses(blocks: tuple[HarnessBlock, ...]) -> None:
    """Refuse a run that reports the same harness twice."""
    names = tuple(block.name for block in blocks)
    if len(set(names)) != len(names):
        msg = "a harness was reported twice in the same run"
        raise KaniLogError(msg)


def _require_summary_counts(
    blocks: tuple[HarnessBlock, ...],
    manual: ManualSummary,
) -> None:
    """Refuse a manual summary that disagrees with the blocks."""
    succeeded = tuple(block.name for block in blocks if block.succeeded)
    failed = tuple(block.name for block in blocks if block.verdict == "FAILED")
    counts = (manual.verified, manual.failures, manual.total)
    observed = (len(succeeded), len(failed), len(blocks))
    if counts != observed and not _fail_fast_counts(counts, failed):
        msg = (
            f"the manual summary claims {manual.verified} verified and "
            f"{manual.failures} failed of {manual.total}, but the blocks "
            f"report {len(succeeded)} verified and {len(failed)} failed of "
            f"{len(blocks)}"
        )
        raise KaniLogError(msg)
    if manual.failed != failed:
        msg = (
            f"the manual summary names failures {list(manual.failed)}, but the "
            f"blocks report {list(failed)}"
        )
        raise KaniLogError(msg)


def _fail_fast_counts(
    counts: tuple[int, int, int],
    failed: tuple[str, ...],
) -> bool:
    """Report whether a summary counted only the failing harnesses.

    ``--fail-fast`` stops the run at the first failing harness and passes only
    that result to Kani's final-summary routine, so successes printed before
    it appear as blocks but not in the counts.  The driver summarises exactly
    one failed result in that mode, so the only accepted shape is a summary
    that counts one failure and no successes.  A summary naming several failed
    blocks is a run Kani 0.67.0 cannot have produced under either mode, and is
    still refused.  The reader cannot see the driver flag in the transcript, so
    this shape is the only evidence of the mode.
    """
    verified, failures, total = counts
    return verified == 0 and len(failed) == 1 and failures == 1 and total == 1
