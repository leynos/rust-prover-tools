"""Consume the report sections that appear inside a Kani harness block."""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.checks import check, explained_message
from rust_prover_tools.kani_log.model import ManualSummary
from rust_prover_tools.kani_log.syntax import (
    CHECK,
    COMPLETE,
    COVER,
    COVER_STATUSES,
    FAILED_CHECK,
    FAILED_FOR,
    FAILED_LOCATION,
    NOISE_PREFIXES,
    TOTAL,
    UNSUPPORTED_ENTRY,
    UNSUPPORTED_REACHABLE,
    VERIFICATION,
    FailedCheck,
    KaniLogError,
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


def results(reader: Reader, builder: _BlockBuilder) -> None:
    """Consume the ``RESULTS:`` section into ``builder``.

    Parameters
    ----------
    reader : Reader
        The log cursor, positioned immediately after the caller consumed the
        ``RESULTS:`` heading line.
    builder : _BlockBuilder
        The block the listed checks are recorded on.  ``has_results`` is set
        before the first check is read.

    Returns
    -------
    None
        The cursor stops at the first line that is neither a ``Check N:``
        entry nor blank, which is the section boundary the block reader
        continues from.

    Raises
    ------
    KaniLogError
        If a check entry is malformed or the log ends inside one.
    """
    builder.has_results = True
    while not reader.exhausted:
        line = reader.peek()
        entry = CHECK.match(line)
        if entry is not None:
            _record_check(reader, builder, entry)
        elif not line.strip():
            reader.take()
        else:
            return


def _record_check(
    reader: Reader,
    builder: _BlockBuilder,
    entry: re.Match[str],
) -> None:
    """Append one check, refusing an identifier the block already reported.

    Two checks sharing an identifier would describe one property twice, and a
    reader that sums or compares per-check facts would then count it twice.
    Kani prints each check once, so a repeat is a malformed report rather than
    a fact to reconcile.  The same identifier in another block is ordinary,
    because identifiers are unique only within a harness.
    """
    item = check(reader, entry)
    if item.identifier in builder.check_ids:
        msg = f"the block for {builder.name!r} reports check {item.identifier!r} twice"
        raise KaniLogError(msg)
    builder.check_ids.add(item.identifier)
    builder.properties.append(item)


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
    """Consume the ``SUMMARY:`` section, including failed-check explanations.

    Parameters
    ----------
    reader : Reader
        The log cursor, positioned immediately after the caller consumed the
        ``SUMMARY:`` heading line.
    builder : _BlockBuilder
        The block the counts, covers and failure explanations are recorded on.

    Returns
    -------
    None
        The cursor stops before the verdict line that ends the section, which
        the block reader consumes next.

    Raises
    ------
    KaniLogError
        If a summary line is unrecognised or the log is truncated before the
        verdict.
    """
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
            builder.failures.append(failed_check(reader, builder, message))
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
    builder.other_counts = _status_counts(reader, match["counts"])


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
    counts = _status_counts(reader, match["counts"])
    builder.covered_unreachable = counts.get("unreachable")
    builder.covered_undetermined = counts.get("undetermined")


def _status_counts(reader: Reader, rendered: str | None) -> dict[str, int]:
    """Parse a parenthesised list of per-status counts.

    Kani prints one count per status, joined with a comma when more than one
    applies, such as ``(1 undetermined,2 unreachable)``.
    """
    if rendered is None:
        return {}
    counts: dict[str, int] = {}
    for entry in rendered.split(","):
        number, _, word = entry.strip().partition(" ")
        if word not in COVER_STATUSES:
            msg = f"{reader.where()}: a cover count names the unknown status {word!r}"
            raise KaniLogError(msg)
        if word in counts:
            # One status printed twice means the two counts contradict each
            # other; keeping the later one would hide the earlier number.
            msg = f"{reader.where()}: a cover count names {word!r} twice"
            raise KaniLogError(msg)
        counts[word] = _report_int(reader, number)
    return counts


def failed_check(
    reader: Reader,
    builder: _BlockBuilder,
    match: re.Match[str],
) -> FailedCheck:
    """Consume one failed-check explanation and its optional location.

    Parameters
    ----------
    reader : Reader
        The log cursor, positioned after the ``Failed Checks:`` line.
    builder : _BlockBuilder
        The block being read; its results are already parsed.
    match : re.Match[str]
        The already-matched ``Failed Checks:`` line.

    Returns
    -------
    FailedCheck
        The complete explanation and the location Kani attributed it to.

    Raises
    ------
    KaniLogError
        If the explanation disagrees with the check it repeats.

    """
    message = explained_message(reader, builder, match["message"])
    path: str | None = None
    line: int | None = None
    if not reader.exhausted:
        location = FAILED_LOCATION.match(reader.peek())
        if location is not None:
            reader.take()
            path, line = location["path"], _report_int(reader, location["line"])
    return FailedCheck(message=message, path=path, line=line)


def verification(
    reader: Reader,
    builder: _BlockBuilder,
    match: re.Match[str],
) -> None:
    """Record the verdict from one already-matched ``VERIFICATION:`` line.

    Parameters
    ----------
    reader : Reader
        The log cursor, positioned at the verdict line.
    builder : _BlockBuilder
        The block the verdict and expected-panic note are recorded on.
    match : re.Match[str]
        The already-matched verdict line.

    Returns
    -------
    None
        The verdict line is consumed.

    Raises
    ------
    KaniLogError
        If the block already carries a verdict, because one block is one
        verification result.
    """
    reader.take()
    if builder.verdict is not None:
        msg = f"the block for {builder.name!r} reports two verification results"
        raise KaniLogError(msg)
    builder.verdict = match["verdict"]
    builder.panics_as_expected = (
        match["note"] == "(encountered one or more panics as expected)"
    )


def manual(reader: Reader) -> ManualSummary:
    """Consume the ``Manual Harness Summary`` that ends a complete run.

    Parameters
    ----------
    reader : Reader
        The log cursor, positioned at the ``Manual Harness Summary:`` heading.

    Returns
    -------
    ManualSummary
        The counts and failed harness names the summary printed.  The heading
        and every consumed summary line are behind the cursor.

    Raises
    ------
    KaniLogError
        If a summary line is unrecognised or the counts never arrive.
    """
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
    """Consume Kani's pre-flight list of constructs it cannot model.

    Parameters
    ----------
    reader : Reader
        The log cursor, positioned at the list's heading line.

    Returns
    -------
    list[str]
        One name per listed construct, in source order.  The heading and the
        entries are behind the cursor; the first non-entry line is left for
        the caller.

    Raises
    ------
    KaniLogError
        Never directly; a truncated list simply ends at the last entry.
    """
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
    """Report whether a line is build or driver chatter, not report content.

    Parameters
    ----------
    line : str
        One rendered log line.

    Returns
    -------
    bool
        ``True`` for a blank line or one beginning with a known chatter
        prefix.
    """
    return not line.strip() or line.startswith(NOISE_PREFIXES)
