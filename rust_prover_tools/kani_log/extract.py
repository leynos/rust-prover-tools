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
import shlex

from rust_prover_tools.kani_log.model import HarnessBlock, KaniLog, ManualSummary
from rust_prover_tools.kani_log.reader import Reader
from rust_prover_tools.kani_log.syntax import (
    HARNESS_START,
    LIST_WRITTEN,
    NO_UNWINDING_ASSERTIONS_FLAG,
    SOLVER_FRAME,
    SUPPORTED_KANI_VERSIONS,
    UNSUPPORTED_PRELUDE,
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
    reader = Reader(text.splitlines())
    version = _require_supported_banner(reader)
    return _reconcile(version, _scan(reader))


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
            if manual is not None:
                msg = (
                    f"{reader.where()}: a harness block follows the manual "
                    "harness summary"
                )
                raise KaniLogError(msg)
            blocks.append(reader.block(header))
        else:
            reader.take()
    return _ObservedLog(
        tuple(blocks),
        manual,
        is_list,
        tuple(unsupported),
    )


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
    if (len(succeeded), len(failed), len(blocks)) != (
        manual.verified,
        manual.failures,
        manual.total,
    ):
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

    Kani quotes a path holding spaces as one argument, so ``--unwind`` there is
    part of a path, not an option.  Each argument contributes itself and, when
    written ``--option=value``, the option alone; the flags read here are
    written as separate words, so matching whole arguments is enough.

    The echo's own framing is removed first.  A closing backtick is an ordinary
    character to ``shlex``, so leaving it on would join it to the last argument
    and hide a trailing flag such as ``--no-unwinding-assertions`` from the
    membership test.

    Returns
    -------
    frozenset[str] | None
        The arguments and their option names, or ``None`` when the command
        cannot be split, because an unbalanced quote leaves no argument
        boundary to trust.
    """
    framed = command.removeprefix(SOLVER_FRAME).removesuffix("`")
    try:
        words = shlex.split(framed)
    except ValueError:
        return None
    return frozenset(word for item in words for word in (item, item.partition("=")[0]))
