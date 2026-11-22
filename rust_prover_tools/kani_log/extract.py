"""Read a whole Kani log, or report whether it asked for unwinding checks.

This is the whole-text convenience over :class:`KaniLogReader`. It exists so a
caller holding a complete capture — a fixture, an archived proof log, or a log
for a process that has already exited — needs no loop of its own. It is not a
second implementation: it feeds the same reader and calls the same `finish`, so
the live and offline paths cannot drift.

Examples
--------
>>> parse("not a log")  # doctest: +IGNORE_EXCEPTION_DETAIL
Traceback (most recent call last):
KaniLogError: structural error: the log does not begin with a Kani banner
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.reader import KaniLogReader
from rust_prover_tools.kani_log.syntax import (
    NO_UNWINDING_ASSERTIONS_FLAG,
    UNWINDING_BOUND_FLAGS,
    LineStreamName,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_log.model import HarnessBlock, KaniLog

__all__ = ("parse", "parse_lines", "unwinding_checks_enabled")


def parse(text: str, *, exit_code: int = 0) -> KaniLog:
    """Read one complete Kani log into per-harness facts, or refuse it.

    Parameters
    ----------
    text : str
        Complete captured output of one Kani invocation, with standard output
        and standard error in that order.
    exit_code : int
        Raw child status to record on the parsed log.

    Returns
    -------
    KaniLog
        The banner, output format, every harness block, the manual summary,
        the constructs Kani named as unsupported, and the exit status.

    Raises
    ------
    KaniLogError
        Raised if the log is truncated, duplicated, interleaved,
        count-inconsistent, or otherwise not a complete Kani report.
    UnsupportedKaniGrammarError
        Raised if the banner names an unregistered release.
    """
    return parse_lines(
        (line.removesuffix("\r") for line in text.split("\n")),
        exit_code=exit_code,
    )


def parse_lines(
    lines: cabc.Iterable[str],
    *,
    exit_code: int = 0,
    stream: LineStreamName = "stdout",
) -> KaniLog:
    """Read a log supplied as an iterable of lines.

    Parameters
    ----------
    lines : Iterable[str]
        Lines without terminators, in arrival order.
    exit_code : int
        Raw child status to record on the parsed log.
    stream : LineStreamName
        Stream to attribute to every line. The default matches a capture that
        concatenated standard output before standard error.

    Returns
    -------
    KaniLog
        The reconciled facts.

    Raises
    ------
    KaniLogError
        Raised for the first structural, contradiction, summary, or truncation
        reason.
    UnsupportedKaniGrammarError
        Raised if the banner names an unregistered release.
    """
    reader = KaniLogReader()
    for line in lines:
        reader.feed_line(stream, line)
    return reader.finish(exit_code)


def unwinding_checks_enabled(block: HarnessBlock) -> bool | None:
    """Report whether the solver was asked to generate unwinding assertions.

    Parameters
    ----------
    block : HarnessBlock
        Block whose echoed solver command is inspected.

    Returns
    -------
    bool | None
        `True` when the command carries an unwinding bound and does not
        disable the assertions, `False` when it carries no bound or disables
        them, and `None` when the block echoed no solver command.

    Notes
    -----
    The rule is CBMC's: a bound is what makes it emit the assertion in the
    first place, which is why Kani's driver never passes
    ``--unwinding-assertions`` and instead disables them with
    ``--no-unwinding-assertions``.
    """
    if block.solver_command is None:
        return None
    if NO_UNWINDING_ASSERTIONS_FLAG in block.solver_command:
        return False
    return any(flag in block.solver_command for flag in UNWINDING_BOUND_FLAGS)
