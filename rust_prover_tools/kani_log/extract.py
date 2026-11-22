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

from rust_prover_tools.kani_log.model import HarnessBlock, KaniLog, ManualSummary
from rust_prover_tools.kani_log.reader import Reader
from rust_prover_tools.kani_log.syntax import (
    BANNER,
    HARNESS_START,
    LIST_WRITTEN,
    NO_UNWINDING_ASSERTIONS_FLAG,
    SOLVER_FRAME,
    SUPPORTED_KANI_VERSIONS,
    UNSUPPORTED_PRELUDE,
    VERIFICATION,
    KaniLogError,
)

__all__ = ("parse_kani_log", "unwinding_checks_enabled")


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
    reader = Reader(_physical_lines(text))
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
            unsupported.extend(reader.unsupported_constructs())
        elif line.startswith(LIST_WRITTEN):
            is_list = True
            reader.take()
        elif header := HARNESS_START.match(line):
            _refuse_harness_after_manual(reader, manual)
            blocks.append(reader.block(header))
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


def _refuse_orphan_report_line(reader: Reader, line: str) -> None:
    """Refuse a report marker that no harness block owns.

    Discarding one would accept a malformed capture and return an earlier
    block's facts as though the report were complete.
    """
    if line in {"RESULTS:", "SUMMARY:"} or VERIFICATION.match(line) is not None:
        msg = f"{reader.where()}: a report line outside a harness block"
        raise KaniLogError(msg)


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
    it appear as blocks but not in the counts.  The valid shape is therefore a
    summary with no successes whose total is exactly the failed set; any other
    disagreement is still refused.  The reader cannot see the driver flag in
    the transcript, so this shape is the only evidence of the mode.
    """
    verified, failures, total = counts
    return (
        verified == 0
        and bool(failed)
        and failures == len(failed)
        and total == len(failed)
    )


def unwinding_checks_enabled(block: HarnessBlock) -> bool | None:
    """Report whether the solver was asked to generate unwinding assertions.

    Parameters
    ----------
    block : HarnessBlock
        One parsed harness block.

    Returns
    -------
    bool | None
        ``False`` when the echoed command disables the assertions, ``True``
        otherwise, and ``None`` when no solver command was echoed or its
        arguments cannot be read.  An unreadable command may still carry the
        disabling flag, so it must not be read as evidence that the checks
        were generated.

    Notes
    -----
    The rule is Kani's, read from the pinned driver rather than guessed: the
    driver passes ``--no-unwinding-assertions`` when the checks are off and
    otherwise passes only ``--no-self-loops-to-assumptions``, relying on
    CBMC's own default.  So the echoed command decides with one flag, and a
    missing bound says nothing about whether the assertions were generated.

    """
    if block.solver_command is None:
        return None
    arguments = _solver_arguments(block.solver_command)
    if arguments is None:
        return None
    return NO_UNWINDING_ASSERTIONS_FLAG not in arguments


def _solver_arguments(command: str) -> frozenset[str] | None:
    """Split the echoed ``cbmc`` command into its arguments and their options.

    The echo is read at Kani's own boundaries rather than as shell text.  Kani
    wraps an argument in double quotes when, and only when, it holds a space,
    and escapes nothing: an apostrophe is an ordinary character.  Splitting it
    as shell words would let one apostrophe open a quoted region spanning the
    arguments that follow, which hides a flag such as
    ``--no-unwinding-assertions`` from the membership test and reports checks
    that were disabled as enabled.

    Each argument contributes itself and, when written ``--option=value``, the
    option alone; the flags read here are written as separate words, so
    matching whole arguments is enough.

    The echo's own framing is removed first.  A closing backtick would
    otherwise join the last argument and hide a trailing flag.

    A wrapped argument is only recoverable when it ends the echo.  Because
    Kani escapes nothing inside it, a quote followed by more text can close
    the argument or be a quote the value contains; no fact in the rendering
    says which.  Reading it as a boundary would let one rendering stand for
    two argument lists, and the flag the other reading contains would go
    unread.

    Returns
    -------
    frozenset[str] | None
        The arguments and their option names, or ``None`` when the rendering
        cannot be split.  An unterminated wrap, text after a closing quote, or
        a wrapped argument followed by more arguments means the boundaries
        cannot be recovered; none of those shapes may be read as evidence that
        the checks were generated.
    """
    if not command.startswith(SOLVER_FRAME) or not command.endswith("`"):
        # A capture that cut the echo mid-argument leaves a flag that is
        # not a whole word, so none of its options can be read as fact.
        return None
    framed = command[len(SOLVER_FRAME) : -1]
    words = _rendered_words(framed)
    if words is None:
        return None
    return frozenset(word for item in words for word in (item, item.partition("=")[0]))


def _rendered_words(rendered: str) -> list[str] | None:
    """Split a command echo at the boundaries Kani rendered it with.

    Parameters
    ----------
    rendered : str
        The echoed command without its framing backticks.

    Returns
    -------
    list[str] | None
        The words Kani passed, or ``None`` when a quote leaves them ambiguous.

    """
    words: list[str] = []
    index = 0
    end_of_rendering = len(rendered)
    while index < end_of_rendering:
        if rendered[index] == " ":
            index += 1
            continue
        if rendered[index] == '"':
            closing = rendered.find('"', index + 1)
            if closing == -1:
                return None
            words.append(rendered[index + 1 : closing])
            following = closing + 1
            while following < end_of_rendering and rendered[following] == " ":
                following += 1
            if following < end_of_rendering:
                # Kani escapes nothing inside a wrapped argument, so a quote
                # followed by more text is ambiguous: it can close the
                # argument, or it can be a quote the value itself contains
                # with the rest of the value continuing after the space.  No
                # fact in the rendering distinguishes the two reads, so the
                # boundary is unknown rather than assumed.
                return None
            index = following
            continue
        end = rendered.find(" ", index)
        if end == -1:
            end = end_of_rendering
        words.append(rendered[index:end])
        index = end
    return words
