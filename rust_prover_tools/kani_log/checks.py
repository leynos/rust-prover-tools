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
    CHECK_FIELDS,
    CHECK_STATUSES,
    FIELD,
    KaniLogError,
    PropertyResult,
)

if typ.TYPE_CHECKING:
    import re

    from rust_prover_tools.kani_log.reader import Reader

__all__ = ("check",)

#: The shortest description that can carry a pair of framing quotes.
_FRAMED_LENGTH = 2


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
    return PropertyResult(
        identifier=match["identifier"],
        status=values["Status"],
        description=_unquote(values["Description"]),
    )


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


def _ends_entry(reader: Reader) -> bool:
    """Report whether the quote just read ends the check's description.

    A description is followed by another field such as ``Location``, by the
    blank line that closes the entry, or by nothing when the log ends.  All
    three shapes mean the frame has closed; anything else is a continuation
    line waiting to be read.
    """
    if reader.exhausted:
        return True
    following = reader.peek()
    if not following.strip():
        return True
    return FIELD.match(following) is not None


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
