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

import re
import typing as typ

from rust_prover_tools.kani_log.descriptions import ends_entry
from rust_prover_tools.kani_log.syntax import (
    CHECK_FIELDS,
    CHECK_STATUSES,
    FIELD,
    KaniLogError,
    PropertyResult,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_log.reader import Reader, _BlockBuilder

__all__ = ("check",)

_MALFORMED_CHECK = re.compile(r"^Check \d+: ")

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
            values[key] = _joined_description(reader, values[key], values)
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


def _joined_description(
    reader: Reader,
    value: str,
    seen: cabc.Container[str],
) -> str:
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
    while _is_open_quotation(reader, value, seen):
        if reader.exhausted:
            msg = f"{reader.where()}: the log ends inside a check description"
            raise KaniLogError(msg)
        value = f"{value}\n{reader.take()}"
    return value


def _is_open_quotation(
    reader: Reader,
    value: str,
    seen: cabc.Container[str],
) -> bool:
    """Report whether a rendered description still waits for its closing quote.

    A line ending with a quote does not by itself close the frame, because an
    embedded quote can end a printed line.  The report's own boundary decides.
    """
    if not value.startswith('"'):
        return False
    if not (len(value) >= _FRAMED_LENGTH and value.endswith('"')):
        return True
    return not ends_entry(reader, seen)


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
    if builder.failed_descriptions is None:
        builder.failed_descriptions = tuple(
            item.description for item in builder.properties if item.status == "FAILURE"
        )
    described = builder.failed_descriptions
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
