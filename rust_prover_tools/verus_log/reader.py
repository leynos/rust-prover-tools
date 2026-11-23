"""Read one captured Verus invocation into its reported facts.

The scan is deliberately order-independent: Verus writes its summary to standard
output and its diagnostics to standard error, so a captured log may show either
first.  Lines the grammar does not name - compiler chatter, progress notes,
``help``/``note`` asides - are skipped rather than refused, because Verus
delegates to rustc and the compiler's vocabulary is not this package's to pin.

What is refused is a log that contradicts itself: two summaries, or a clean
summary beside a failure that would have to have been counted.  rustc's closing
``error: aborting due to N previous errors`` trailer counts as that evidence
even though it is not itself a failure: a capture can lose the diagnostic and
keep the trailer, and reading the trailer as chatter would report the run as
clean.
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
    trailers: list[str] = []
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
        if header is None:
            continue
        if line.startswith(TRAILER_PREFIXES):
            trailers.append(header["message"])
        else:
            failures.append(_failure(header["message"], lines, index))
    _require_consistent(summary, failures, trailers)
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
    """Build one failure, taking the span printed beneath it.

    Verus writes a diagnostic and its span to standard error and its summary to
    standard output, so a capture that merges the streams can put a summary or
    a selection note between the two. Those named records are stepped over. The
    search ends at another diagnostic, or at a line the grammar does not name,
    because a span found beyond either of those belongs to somebody else.

    The tail is walked by position rather than sliced: a slice would copy the
    remainder of the capture once per diagnostic, and an iterator restarts
    from the first line to reach the offset.  Starting the walk at the
    diagnostic's own index keeps a transcript with a diagnostic on most lines
    linear in its length.
    """
    for position in range(index + 1, len(lines)):
        following = lines[position]
        if ERROR_HEADER.match(following) is not None:
            break
        span = LOCATION.match(following)
        if span is not None:
            return _located(message, span)
        if SUMMARY.match(following) is None and SELECTION_NOTE.match(following) is None:
            break
    return VerusFailure(message)


def _located(message: str, span: re.Match[str]) -> VerusFailure:
    """Build the failure a span locates, or an unlocated one it cannot."""
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
    trailers: list[str],
) -> None:
    """Refuse a clean summary beside evidence that contradicts it.

    A trailer is rustc's own count of the errors it printed.  It is not a
    failure, so it never enters the failure collection a negative control
    counts, but a clean summary beside one is a capture that lost the
    diagnostic the trailer says was printed.
    """
    if summary is None:
        return
    if summary.errors == 0 and trailers:
        msg = (
            f"the log records no verification errors but reports the trailer "
            f"{trailers[0]!r}"
        )
        raise VerusLogError(msg)
    if failures:
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
