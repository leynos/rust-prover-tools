"""Consume the report sections that appear inside a Kani harness block."""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.model import ManualSummary
from rust_prover_tools.kani_log.syntax import (
    CHECK,
    CHECK_FIELDS,
    CHECK_STATUSES,
    COMPLETE,
    COVER,
    COVER_STATUSES,
    FAILED_CHECK,
    FAILED_FOR,
    FAILED_LOCATION,
    FIELD,
    NOISE_PREFIXES,
    TOTAL,
    UNSUPPORTED_ENTRY,
    UNSUPPORTED_REACHABLE,
    VERIFICATION,
    FailedCheck,
    KaniLogError,
    PropertyResult,
)

if typ.TYPE_CHECKING:
    import re

    from rust_prover_tools.kani_log.reader import Reader, _BlockBuilder

__all__ = (
    "failed_check",
    "is_noise",
    "manual",
    "results",
    "summary",
    "unsupported_constructs",
    "verification",
)

#: The shortest description that can carry a pair of framing quotes.
_FRAMED_LENGTH = 2


def results(reader: Reader, builder: _BlockBuilder) -> None:
    """Consume the ``RESULTS:`` section into ``builder``."""
    builder.has_results = True
    while not reader.exhausted:
        line = reader.peek()
        entry = CHECK.match(line)
        if entry is not None:
            builder.properties.append(check(reader, entry))
        elif not line.strip():
            reader.take()
        else:
            return


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


def _report_int(reader: Reader, value: str) -> int:
    """Convert one rendered integer, refusing a value too long to be a count.

    CPython limits integer string conversion, and a digit run past that limit
    raises :class:`ValueError` rather than producing a number.  The parser must
    turn that into a structural error so callers see the same ``tool-error``
    result as any other malformed report line.
    """
    try:
        return int(value)
    except ValueError:
        msg = (
            f"{reader.where()}: the report's integer {value[:16]!r} is not a "
            "count this parser can read"
        )
        raise KaniLogError(msg) from None


def summary(reader: Reader, builder: _BlockBuilder) -> None:
    """Consume the ``SUMMARY:`` section, including failed-check explanations."""
    while not reader.exhausted:
        line = reader.peek()
        total = TOTAL.match(line)
        cover = COVER.match(line)
        if total is not None:
            _record_total(reader, builder, total)
        elif cover is not None:
            _record_cover(reader, builder, cover)
        elif message := FAILED_CHECK.match(line):
            reader.take()
            builder.failures.append(failed_check(reader, message))
        elif VERIFICATION.match(line) or line == UNSUPPORTED_REACHABLE:
            return
        elif not line.strip():
            reader.take()
        else:
            msg = (
                f"{reader.where()}: unrecognised summary line for "
                f"{builder.name!r}: {line!r}"
            )
            raise KaniLogError(msg)
    msg = f"the log is truncated inside the summary for {builder.name!r}"
    raise KaniLogError(msg)


def _record_total(
    reader: Reader,
    builder: _BlockBuilder,
    match: re.Match[str],
) -> None:
    """Record one ``N of M failed`` line."""
    if builder.failed_count is not None:
        msg = f"the block for {builder.name!r} has two failure counts"
        raise KaniLogError(msg)
    reader.take()
    builder.failed_count = _report_int(reader, match["failed"])
    builder.total_count = _report_int(reader, match["total"])
    builder.other_count = _report_int(reader, match["other"] or "0")
    builder.other_kind = match["other_kind"]


def _record_cover(
    reader: Reader,
    builder: _BlockBuilder,
    match: re.Match[str],
) -> None:
    """Record one ``N of M cover properties satisfied`` line."""
    if builder.covered_total is not None:
        msg = f"the block for {builder.name!r} has two cover counts"
        raise KaniLogError(msg)
    reader.take()
    builder.covered_satisfied = _report_int(reader, match["satisfied"])
    builder.covered_total = _report_int(reader, match["total"])
    counts = _cover_counts(reader, match["counts"])
    builder.covered_unreachable = counts.get("unreachable")
    builder.covered_undetermined = counts.get("undetermined")


def _cover_counts(reader: Reader, rendered: str | None) -> dict[str, int]:
    """Parse the parenthesised per-status counts on a cover line.

    Kani prints one count per status, comma separated when more than one
    applies, such as ``(1 unreachable, 1 undetermined)``.
    """
    if rendered is None:
        return {}
    counts: dict[str, int] = {}
    for entry in rendered.split(", "):
        number, _, word = entry.partition(" ")
        if word not in COVER_STATUSES:
            msg = f"{reader.where()}: a cover count names the unknown status {word!r}"
            raise KaniLogError(msg)
        counts[word] = _report_int(reader, number)
    return counts


def failed_check(reader: Reader, match: re.Match[str]) -> FailedCheck:
    """Consume the optional location line after a failed-check message."""
    path: str | None = None
    line: int | None = None
    if not reader.exhausted:
        location = FAILED_LOCATION.match(reader.peek())
        if location is not None:
            reader.take()
            path, line = location["path"], _report_int(reader, location["line"])
    return FailedCheck(message=match["message"], path=path, line=line)


def verification(
    reader: Reader,
    builder: _BlockBuilder,
    match: re.Match[str],
) -> None:
    """Record the verdict from one already-matched ``VERIFICATION:`` line."""
    reader.take()
    if builder.verdict is not None:
        msg = f"the block for {builder.name!r} reports two verification results"
        raise KaniLogError(msg)
    builder.verdict = match["verdict"]
    builder.panics_as_expected = (
        match["note"] == "(encountered one or more panics as expected)"
    )


def manual(reader: Reader) -> ManualSummary:
    """Consume the ``Manual Harness Summary`` that ends a complete run."""
    reader.take()
    failed: list[str] = []
    while not reader.exhausted:
        line = reader.peek()
        failure = FAILED_FOR.match(line)
        complete = COMPLETE.match(line)
        if failure is not None:
            reader.take()
            failed.append(failure["name"])
        elif complete is not None:
            reader.take()
            return ManualSummary(
                _report_int(reader, complete["verified"]),
                _report_int(reader, complete["failures"]),
                _report_int(reader, complete["total"]),
                tuple(failed),
            )
        elif not line.strip():
            reader.take()
        else:
            msg = f"{reader.where()}: unrecognised manual summary line: {line!r}"
            raise KaniLogError(msg)
    msg = "the log is truncated: the manual harness summary has no counts"
    raise KaniLogError(msg)


def unsupported_constructs(reader: Reader) -> list[str]:
    """Consume Kani's pre-flight list of constructs it cannot model."""
    reader.take()
    names: list[str] = []
    while not reader.exhausted:
        entry = UNSUPPORTED_ENTRY.match(reader.peek())
        if entry is None:
            if reader.peek().strip():
                return names
            reader.take()
            continue
        names.append(entry["name"])
        reader.take()
    return names


def is_noise(line: str) -> bool:
    """Report whether a line is build or driver chatter, not report content."""
    return not line.strip() or line.startswith(NOISE_PREFIXES)
