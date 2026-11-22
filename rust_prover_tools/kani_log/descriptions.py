"""Decide where a check entry's quoted description ends.

Kani frames a description in double quotes and escapes nothing inside it, so a
description holding a newline is printed over several lines and an embedded
quote can end a printed line.  The frame therefore closes at the report's own
boundary rather than at the first line ending in a quote, and the boundary
itself is validated: a value may print ``RESULTS:``, ``Location:`` or
``SUMMARY:`` as content.
"""

from __future__ import annotations

import re
import typing as typ

from rust_prover_tools.kani_log.syntax import (
    CHECK,
    COMPLETE,
    COVER,
    FAILED_CHECK,
    FAILED_FOR,
    FAILED_LOCATION,
    FIELD,
    HARNESS_START,
    TOTAL,
    UNSUPPORTED_ENTRY,
    VERIFICATION,
    VERIFICATION_TIME,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_log.reader import Reader

__all__ = ("ends_entry",)

#: The shortest description that can carry a pair of framing quotes.
_FRAMED_LENGTH = 2
#: A ``Check`` header with an ordinal but not the rest of the report grammar.
_MALFORMED_CHECK = re.compile(r"^Check \d+: ")


def _is_open_quotation(
    reader: Reader,
    value: str,
    seen: cabc.Container[str],
) -> bool:
    """Report whether a rendered description still waits for its closing quote.

    Kani prints the value between one pair of quotes without escaping anything
    inside it, so a line ending with a quote does not by itself close the
    frame: an embedded quote may end a printed line.  The frame closes on the
    line the entry ends on, and the report's own boundary is what says where
    that is.  A line that closes no quote is unfinished whatever follows it.
    """
    if not value.startswith('"'):
        return False
    if not (len(value) >= _FRAMED_LENGTH and value.endswith('"')):
        return True
    return not ends_entry(reader, seen)


#: Report constructs that can follow a check entry.  A blank line only closes
#: the entry when what follows it is one of these: a description holding a
#: newline can print a blank line as content, and the next continuation line
#: then looks like ordinary text.
_ENTRY_HEADS = (
    CHECK,
    FIELD,
    FAILED_CHECK,
    FAILED_LOCATION,
    COVER,
    TOTAL,
    COMPLETE,
    UNSUPPORTED_ENTRY,
    VERIFICATION,
    VERIFICATION_TIME,
    HARNESS_START,
)


def ends_entry(reader: Reader, seen: cabc.Container[str]) -> bool:
    """Report whether the quote just read ends the check's description.

    A description is followed by another field such as ``Location``, by the
    blank line that closes the entry, or by nothing when the log ends.  All
    three shapes mean the frame has closed; anything else is a continuation
    line waiting to be read.

    A blank line is not by itself the entry boundary: a description holding a
    newline can print one as content.  The boundary is the next line that is
    not blank, so a blank line closes the entry only when a report construct
    follows it.  Without that lookahead, a description containing
    ``first"`` and an empty line before ``second`` would be cut short at the
    embedded quote, losing the continuation and leaving it to be refused as an
    unrecognised line.
    """
    if reader.exhausted:
        return True
    following = reader.peek()
    if not following.strip():
        return _blank_closes_entry(reader, seen)
    return FIELD.match(following) is not None and _field_closes_entry(
        reader,
        seen,
    )


def _field_closes_entry(reader: Reader, seen: cabc.Container[str]) -> bool:
    """Report whether the field-shaped line at the cursor is a real boundary.

    A description is arbitrary text, so one of its continuation lines may
    itself look like ``Location:``.  A real field run is followed by another
    field, a report construct, a fresh check entry or the end of the log; a
    description continuation is followed by ordinary text.  Scanning past the
    run is what keeps ``first"`` + a field-shaped line + ``last"`` inside one
    description.
    """
    first = FIELD.match(reader.peek())
    if first is not None and first["key"] in seen:
        return False
    offset = 0
    while (following := reader.peek_at(offset)) is not None:
        if not following.strip() or FIELD.match(following) is not None:
            offset += 1
            continue
        return _marker_closes_entry(reader, offset, seen)
    return True


def _is_report_line(line: str, seen: cabc.Container[str]) -> bool:
    """Report whether ``line`` can follow a check entry's field list.

    A malformed ``Check`` header counts too.  It is not a report construct the
    block can read, but it is also not description continuation: keeping the
    description open would swallow the line and hide the structural fault the
    outer parser reports.
    """
    if _MALFORMED_CHECK.match(line) is not None:
        return True
    field = FIELD.match(line)
    if field is not None:
        return field["key"] not in seen
    return any(pattern.match(line) is not None for pattern in _ENTRY_HEADS)


def _blank_closes_entry(reader: Reader, seen: cabc.Container[str]) -> bool:
    """Report whether the blank line at the cursor ends the check entry.

    ``RESULTS:`` is the one report marker a description can also contain:
    Kani escapes nothing, so a value may print that word on its own line.
    A real results section is followed by a ``Check N:`` entry, so the marker
    closes the description only when that entry is actually there; otherwise
    it is description text and the frame stays open.
    """
    offset = 1
    while (following := reader.peek_at(offset)) is not None and not following.strip():
        offset += 1
    if following is None:
        return True
    if FIELD.match(following) is not None:
        return _field_run_closes_entry(reader, offset, seen)
    return _marker_closes_entry(reader, offset, seen)


def _field_run_closes_entry(
    reader: Reader,
    offset: int,
    seen: cabc.Container[str],
) -> bool:
    """Report whether a field run after a blank line is a real boundary.

    A description may print a blank line and then a field-shaped line as
    content, exactly as it may print one without the blank line.  The run is
    read the same way here: it is a boundary only when what follows it is
    another field, a report construct or the end of the log.
    """
    while (following := reader.peek_at(offset)) is not None:
        if not following.strip() or FIELD.match(following) is not None:
            offset += 1
            continue
        return _marker_closes_entry(reader, offset, seen)
    return True


def _starts_results(reader: Reader, offset: int) -> bool:
    """Report whether a real results section follows the ``RESULTS:`` marker."""
    while (following := reader.peek_at(offset)) is not None and not following.strip():
        offset += 1
    return following is not None and CHECK.match(following) is not None


def _starts_summary(reader: Reader, offset: int) -> bool:
    """Report whether a real summary section follows the ``SUMMARY:`` marker.

    Kani prints its counts as `` ** …`` lines and then the verdict, so a
    marker followed by anything else is description content that happens to
    spell the word.
    """
    while (following := reader.peek_at(offset)) is not None and not following.strip():
        offset += 1
    if following is None:
        return True
    return following.startswith(" **") or VERIFICATION.match(following) is not None


def _starts_manual_summary(reader: Reader, offset: int) -> bool:
    """Report whether a real manual summary follows its heading.

    Kani prints a ``Verification failed for`` line when a harness failed and
    always ends the section with its ``Complete - …`` counts, so a heading that
    neither follows is description content that happens to spell the words.
    """
    while (following := reader.peek_at(offset)) is not None and not following.strip():
        offset += 1
    if following is None:
        return True
    return (
        FAILED_FOR.match(following) is not None or COMPLETE.match(following) is not None
    )


def _marker_closes_entry(
    reader: Reader,
    offset: int,
    seen: cabc.Container[str],
) -> bool:
    """Report whether the line at ``offset`` is a real entry boundary.

    A report marker is also description text, so each is accepted only when
    the structure that follows a real one is present.
    """
    following = reader.peek_at(offset)
    if following is None:
        return True
    if following == "RESULTS:":
        return _starts_results(reader, offset + 1)
    if following == "SUMMARY:":
        return _starts_summary(reader, offset + 1)
    if following == "Manual Harness Summary:":
        return _starts_manual_summary(reader, offset + 1)
    return _is_report_line(following, seen)
