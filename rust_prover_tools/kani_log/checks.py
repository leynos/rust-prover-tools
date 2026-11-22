"""Read one ``Check N:`` entry of a Kani results section.

Kani frames a check's description in a pair of double quotes and escapes
nothing inside it, so a description holding a newline is printed over several
lines.  Reading only the labelled line would leave the rest to be refused as
unrecognised input, so this module follows the frame to the line the entry
ends on.  The report's own boundary - the next field, the blank line that
closes the entry, or the end of the log - is what says where that is; an
embedded quote ending a printed line says nothing.

The field names, statuses and rendered shapes come from
:mod:`rust_prover_tools.kani_log.syntax`, which records them from the pinned
release.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.syntax import (
    CHECK,
    CHECK_FIELDS,
    CHECK_STATUSES,
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
    KaniLogError,
    PropertyResult,
)

if typ.TYPE_CHECKING:
    import re

    from rust_prover_tools.kani_log.reader import Reader, _BlockBuilder

__all__ = ("check",)

#: The shortest description that can carry a pair of framing quotes.
_FRAMED_LENGTH = 2
#: Statuses Kani applies only to cover properties.
_COVER_ONLY_STATUSES = frozenset({"SATISFIED", "UNSATISFIABLE"})


def check(reader: Reader, match: re.Match[str]) -> PropertyResult:
    """Consume the three fields of one ``Check N:`` entry.

    Parameters
    ----------
    reader : Reader
        The log cursor.
    match : re.Match[str]
        The already-matched ``Check`` line.

    Returns
    -------
    PropertyResult
        The check described by its fields.

    Raises
    ------
    KaniLogError
        If a field is missing or the status is unknown.

    """
    reader.take()
    values: dict[str, str] = {}
    while not reader.exhausted:
        field_match = FIELD.match(reader.peek())
        if field_match is None:
            break
        reader.take()
        key = field_match["key"]
        if key in values:
            msg = (
                f"{reader.where()}: check {match['identifier']!r} repeats its "
                f"{key} field"
            )
            raise KaniLogError(msg)
        values[key] = field_match["value"]
        if key == "Description":
            values[key] = _joined_description(reader, values[key])
    missing = [key for key in CHECK_FIELDS if key not in values]
    if missing:
        msg = (
            f"{reader.where()}: check {match['identifier']!r} is missing "
            f"{', '.join(missing)}"
        )
        raise KaniLogError(msg)
    if values["Status"] not in CHECK_STATUSES:
        msg = (
            f"{reader.where()}: check {match['identifier']!r} has unknown "
            f"status {values['Status']!r}"
        )
        raise KaniLogError(msg)
    result = PropertyResult(
        identifier=match["identifier"],
        status=values["Status"],
        description=_unquote(values["Description"]),
    )
    if not result.is_cover and result.status in _COVER_ONLY_STATUSES:
        msg = (
            f"{reader.where()}: check {result.identifier!r} is not a cover but "
            f"carries the cover status {result.status!r}"
        )
        raise KaniLogError(msg)
    return result


def _joined_description(reader: Reader, value: str) -> str:
    """Join the lines a newline in a description splits it across.

    Kani frames a description in quotes and escapes nothing inside it, so a
    description holding a newline is printed over several lines: the opening
    quote ends the ``Description:`` line and the closing quote ends the last
    continuation line.  Reading only the first line would leave the rest to be
    rejected by the block reader as an unrecognized line, refusing an otherwise
    complete log.  A description that opens no quote is returned unchanged.

    The frame closes where the check entry ends, not at the first line that
    happens to end with a quote: an embedded quote can end a printed line just
    as the closing one does.

    Parameters
    ----------
    reader : Reader
        The log cursor, positioned after the ``Description`` line.
    value : str
        The rendered value from that line.

    Returns
    -------
    str
        The complete rendered value, continuation lines joined by newlines.

    Raises
    ------
    KaniLogError
        If the log ends before the description's closing quote.

    """
    while _is_open_quotation(reader, value):
        if reader.exhausted:
            msg = f"{reader.where()}: the log ends inside a check description"
            raise KaniLogError(msg)
        value = f"{value}\n{reader.take()}"
    return value


def _is_open_quotation(reader: Reader, value: str) -> bool:
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
    return not _ends_entry(reader)


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


def _ends_entry(reader: Reader) -> bool:
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
        return _blank_closes_entry(reader)
    return FIELD.match(following) is not None and _field_closes_entry(reader)


def _field_closes_entry(reader: Reader) -> bool:
    """Report whether the field-shaped line at the cursor is a real boundary.

    A description is arbitrary text, so one of its continuation lines may
    itself look like ``Location:``.  A real field run is followed by another
    field, a report construct, a fresh check entry or the end of the log; a
    description continuation is followed by ordinary text.  Scanning past the
    run is what keeps ``first"`` + a field-shaped line + ``last"`` inside one
    description.
    """
    offset = 0
    while (following := reader.peek_at(offset)) is not None:
        if not following.strip() or FIELD.match(following) is not None:
            offset += 1
            continue
        return _is_report_line(following)
    return True


def _is_report_line(line: str) -> bool:
    """Report whether ``line`` can follow a check entry's field list.

    A malformed ``Check`` header counts too.  It is not a report construct the
    block can read, but it is also not description continuation: keeping the
    description open would swallow the line and hide the structural fault the
    outer parser reports.
    """
    if line in {"RESULTS:", "SUMMARY:", "Manual Harness Summary:"}:
        return True
    if line.startswith("Check "):
        return True
    return any(pattern.match(line) is not None for pattern in _ENTRY_HEADS)


def _blank_closes_entry(reader: Reader) -> bool:
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
    if following == "RESULTS:":
        return _starts_results(reader, offset + 1)
    if following == "SUMMARY:":
        return _starts_summary(reader, offset + 1)
    if following == "Manual Harness Summary:":
        return _starts_manual_summary(reader, offset + 1)
    if FIELD.match(following) is not None:
        return _field_run_closes_entry(reader, offset)
    return any(pattern.match(following) is not None for pattern in _ENTRY_HEADS)


def _field_run_closes_entry(reader: Reader, offset: int) -> bool:
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
        return _is_report_line(following)
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


def _unquote(value: str) -> str:
    """Remove one framing pair of quotes from a rendered description.

    Kani wraps the description in quotes but does not escape any it finds
    inside, so a cover written as ``"ready"`` is printed ``""ready""``.
    Stripping every quote would lose the text the manifest declares.
    """
    if len(value) < _FRAMED_LENGTH:
        return value
    if value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def explained_message(
    reader: Reader,
    builder: _BlockBuilder,
    first_line: str,
) -> str:
    """Read the whole explanation of the failure being explained.

    Kani prints the failed property's description again after ``Failed
    Checks:``, and escapes nothing, so a description holding a newline arrives
    over several lines with only the first on the labelled line.  The results
    section has already been read, and Kani renders one explanation per failed
    check in the order it listed them, so the description says exactly how many
    lines the explanation occupies and what they must say.  Reading by the
    text that happens to follow would stop at the wrong line whenever the next
    report line looked like prose.

    A summary that explains something the results did not list contradicts
    itself, so a first line or continuation that disagrees is refused rather
    than attributed to the check.
    """
    described = [
        item.description for item in builder.properties if item.status == "FAILURE"
    ]
    index = len(builder.failures)
    if index >= len(described):
        # More explanations than failures is a contradiction the block's count
        # reconciliation refuses once the block ends; until then there is no
        # description to read the continuation from.
        return first_line
    expected = described[index].split("\n")
    if expected[0] != first_line:
        msg = (
            f"{reader.where()}: the failure explanation {first_line!r} "
            "disagrees with the check it explains"
        )
        raise KaniLogError(msg)
    pieces = [first_line]
    for piece in expected[1:]:
        if reader.exhausted or reader.peek() != piece:
            msg = (
                f"{reader.where()}: the failure explanation for "
                f"{first_line!r} disagrees with the check it explains"
            )
            raise KaniLogError(msg)
        pieces.append(reader.take())
    return "\n".join(pieces)
