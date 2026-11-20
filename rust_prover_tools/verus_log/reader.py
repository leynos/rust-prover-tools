"""Read one captured Verus invocation into its reported facts.

The scan is deliberately order-independent: Verus writes its summary to standard
output and its diagnostics to standard error, so a captured log may show either
first.  Lines the grammar does not name - compiler chatter, progress notes,
``help``/``note`` asides - are skipped rather than refused, because Verus
delegates to rustc and the compiler's vocabulary is not this package's to pin.

What is refused is a log that contradicts itself: two summaries, or a clean
summary beside a failure that would have to have been counted.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.verus_log.model import VerusFailure, VerusLog, VerusSummary
from rust_prover_tools.verus_log.syntax import (
    ERROR_HEADER,
    LOCATION,
    SELECTION_NOTE,
    SUMMARY,
    TRAILER_PREFIXES,
    VerusLogError,
)

if typ.TYPE_CHECKING:
    import re

__all__ = ("parse_verus_log",)


def parse_verus_log(text: str) -> VerusLog:
    """Read one captured Verus invocation.

    Parameters
    ----------
    text : str
        Complete captured output of one Verus invocation, with both streams
        merged in any order.

    Returns
    -------
    VerusLog
        The summary counts, every failure Verus reported, and the note naming
        what was selected for verification.

    Raises
    ------
    VerusLogError
        If the log carries two verification summaries, or a clean summary
        beside a failure.

    Examples
    --------
    >>> parse_verus_log("verification results:: 2 verified, 0 errors").summary
    VerusSummary(verified=2, errors=0, is_partial=False)
    """
    lines = text.splitlines()
    summary: VerusSummary | None = None
    selection: str | None = None
    failures: list[VerusFailure] = []
    for index, line in enumerate(lines):
        found = SUMMARY.match(line)
        if found is not None:
            summary = _summary(summary, found)
            continue
        note = SELECTION_NOTE.match(line)
        if note is not None:
            selection = selection or note["subject"]
            continue
        header = ERROR_HEADER.match(line)
        if header is not None and not line.startswith(TRAILER_PREFIXES):
            failures.append(_failure(header["message"], lines, index))
    _require_consistent(summary, failures)
    return VerusLog(summary, tuple(failures), selection)


def _summary(
    previous: VerusSummary | None,
    match: re.Match[str],
) -> VerusSummary:
    """Read one summary line, refusing a second one."""
    if previous is not None:
        msg = "the log carries two verification results summaries"
        raise VerusLogError(msg)
    return VerusSummary(
        _count(match["verified"]),
        _count(match["errors"]),
        is_partial=match["partial"] is not None,
    )


def _count(value: str) -> int:
    """Convert one rendered count, refusing a digit run too long to be one.

    CPython limits integer string conversion, and a digit run past that limit
    raises :class:`ValueError`.  Turning it into a structural error keeps the
    caller from seeing a traceback where every other malformed log produces a
    ``tool-error`` result.
    """
    try:
        return int(value)
    except ValueError:
        msg = f"the report's integer {value[:16]!r} is not a count this parser can read"
        raise VerusLogError(msg) from None


def _failure(
    message: str,
    lines: list[str],
    index: int,
) -> VerusFailure:
    """Build one failure, taking the span printed on the following line."""
    if index + 1 >= len(lines):
        return VerusFailure(message)
    span = LOCATION.match(lines[index + 1])
    if span is None:
        return VerusFailure(message)
    try:
        line = int(span["line"])
        column = int(span["column"])
    except ValueError:
        # A span whose numbers cannot be read locates nothing; keep the
        # diagnostic rather than losing the fact that the function failed.
        return VerusFailure(message)
    return VerusFailure(message, span["path"], line, column)


def _require_consistent(
    summary: VerusSummary | None,
    failures: list[VerusFailure],
) -> None:
    """Refuse a clean summary beside a failure that contradicts it."""
    if summary is None or not failures:
        return
    _require_counted(summary, failures[0])


def _require_counted(summary: VerusSummary, failure: VerusFailure) -> None:
    """Refuse one failure the summary claims not to have counted."""
    if summary.errors != 0:
        return
    where = failure.where
    located = f" at {where}" if where else ""
    msg = (
        f"the log records no verification errors but reports "
        f"{failure.message!r}{located}"
    )
    raise VerusLogError(msg)
