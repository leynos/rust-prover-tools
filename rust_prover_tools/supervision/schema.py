"""Validate the versioned JSONL and report schemas strictly.

The readers reject an unknown or missing key, an unknown record kind or
schema version, and a measurement that is neither available nor explained.
The schema is documented in ``docs/kani-supervised-execution.md``.
"""

from __future__ import annotations

import json
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

from rust_prover_tools.supervision.schema_fields import (
    JSONL_FIELDS,
    REPORT_FIELDS,
    REPORT_RESULT_FIELDS,
)

__all__ = (
    "JSONL_FIELDS",
    "REPORT_FIELDS",
    "EvidenceFormatError",
    "decode_jsonl",
    "decode_measurement",
    "validate_report_document",
)

#: Schema version the readers accept.
_SCHEMA_VERSION = 1


class EvidenceFormatError(ValueError):
    """A record or report that does not match the documented schema."""


def _require_exact(
    document: cabc.Mapping[str, object],
    fields: frozenset[str],
    *,
    subject: str,
) -> None:
    """Require exactly `fields`, naming the missing and unknown keys."""
    missing = sorted(fields - set(document))
    unknown = sorted(set(document) - fields)
    if missing or unknown:
        msg = (
            f"{subject} does not match the schema: missing={missing}, unknown={unknown}"
        )
        raise EvidenceFormatError(msg)


def _measurement_keys(document: object, *, subject: str) -> None:
    """Require a measurement to be exactly one available or absent form."""
    if not isinstance(document, dict):
        msg = f"{subject} is not a measurement object"
        raise EvidenceFormatError(msg)
    keys = set(document)
    if keys == {"value"}:
        if not isinstance(document["value"], int) or isinstance(
            document["value"], bool
        ):
            msg = f"{subject}.value is not an integer"
            raise EvidenceFormatError(msg)
        return
    if keys == {"unavailable_reason"}:
        if not isinstance(document["unavailable_reason"], str):
            msg = f"{subject}.unavailable_reason is not a string"
            raise EvidenceFormatError(msg)
        return
    msg = f"{subject} is neither available nor unavailable: {sorted(keys)}"
    raise EvidenceFormatError(msg)


def decode_measurement(document: object, *, subject: str = "measurement") -> None:
    """Validate one measurement object.

    Parameters
    ----------
    document : object
        Parsed JSON value.
    subject : str
        Name used in the refusal.

    Raises
    ------
    EvidenceFormatError
        Raised when the value is neither ``{"value": N}`` nor
        ``{"unavailable_reason": "..."}``.
    """
    _measurement_keys(document, subject=subject)


def _validate_cgroup(document: object, *, subject: str) -> None:
    """Validate the optional cgroup object of a report result."""
    if document is None:
        return
    if not isinstance(document, dict):
        msg = f"{subject} is not an object"
        raise EvidenceFormatError(msg)
    _require_exact(
        document,
        frozenset({
            "path",
            "start",
            "finish",
            "oom_kill_delta",
            "unavailable_reason",
        }),
        subject=subject,
    )
    for side in ("start", "finish"):
        memory = document[side]
        if memory is None:
            continue
        if not isinstance(memory, dict):
            msg = f"{subject}.{side} is not an object"
            raise EvidenceFormatError(msg)
        _require_exact(
            memory,
            frozenset({"current", "peak", "oom_kill"}),
            subject=f"{subject}.{side}",
        )
        for field in ("current", "peak", "oom_kill"):
            decode_measurement(memory[field], subject=f"{subject}.{side}.{field}")


def decode_jsonl(text: str) -> tuple[dict[str, object], ...]:
    """Validate and return every record in one JSONL document.

    Parameters
    ----------
    text : str
        JSONL text, one record per line.

    Returns
    -------
    tuple[dict[str, object], ...]
        Parsed records, in order.

    Raises
    ------
    EvidenceFormatError
        Raised for malformed JSON, an unknown version or record kind, or a
        record whose keys differ from the documented set.
    """
    records: list[dict[str, object]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if line.strip():
            records.append(_decode_record(line, number))
    return tuple(records)


def _decode_record(line: str, number: int) -> dict[str, object]:
    """Validate one JSONL line and return its parsed record."""
    try:
        document = json.loads(line)
    except ValueError as error:
        msg = f"record {number} is not JSON: {error}"
        raise EvidenceFormatError(msg) from error
    if not isinstance(document, dict):
        msg = f"record {number} is not an object"
        raise EvidenceFormatError(msg)
    if document.get("schema") != _SCHEMA_VERSION:
        msg = f"record {number} has schema {document.get('schema')!r}"
        raise EvidenceFormatError(msg)
    kind = document.get("record")
    if not isinstance(kind, str) or kind not in JSONL_FIELDS:
        msg = f"record {number} has unknown kind {kind!r}"
        raise EvidenceFormatError(msg)
    _require_exact(document, JSONL_FIELDS[kind], subject=f"record {number}")
    _validate_record_measurements(kind, document, subject=f"record {number}")
    return typ.cast("dict[str, object]", document)


def _validate_record_measurements(
    kind: str,
    document: dict[str, object],
    *,
    subject: str,
) -> None:
    """Validate the measurements and cgroup evidence one record kind carries."""
    if kind == "harness-finish":
        for field in ("peak_group_rss", "min_host_available", "host_total"):
            decode_measurement(document[field], subject=f"{subject}.{field}")
        _validate_cgroup(document["cgroup"], subject=f"{subject}.cgroup")
        return
    for field in ("group_rss", "host_available", "host_total"):
        if field in document:
            decode_measurement(document[field], subject=f"{subject}.{field}")


def validate_report_document(document: object) -> None:
    """Validate one final report document against the version-1 schema.

    Parameters
    ----------
    document : object
        Parsed JSON value.

    Raises
    ------
    EvidenceFormatError
        Raised when the document, an expected entry, a result, or a nested
        measurement does not match the schema.
    """
    if not isinstance(document, dict):
        msg = "the report is not an object"
        raise EvidenceFormatError(msg)
    if document.get("schema") != _SCHEMA_VERSION:
        msg = f"the report has schema {document.get('schema')!r}"
        raise EvidenceFormatError(msg)
    _require_exact(document, REPORT_FIELDS, subject="the report")
    _validate_expected(document["expected"])
    _validate_accounting(document["accounting"])
    _validate_results(document["results"])


def _validate_expected(expected: object) -> None:
    """Require every expected entry to name a package, harness, and attempt."""
    if not isinstance(expected, list):
        msg = "the report expected list is not an array"
        raise EvidenceFormatError(msg)
    for index, entry in enumerate(expected):
        if not isinstance(entry, dict):
            msg = f"expected entry {index} is not an object"
            raise EvidenceFormatError(msg)
        _require_exact(
            entry,
            frozenset({"package", "harness", "attempt"}),
            subject=f"expected entry {index}",
        )


def _validate_accounting(accounting: object) -> None:
    """Require the accounting object to carry the three drift lists."""
    if not isinstance(accounting, dict):
        msg = "the report accounting is not an object"
        raise EvidenceFormatError(msg)
    _require_exact(
        accounting,
        frozenset({"missing", "duplicate", "unexpected"}),
        subject="the report accounting",
    )


def _validate_results(results: object) -> None:
    """Require every result to carry the documented fields and measurements."""
    if not isinstance(results, list):
        msg = "the report results list is not an array"
        raise EvidenceFormatError(msg)
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            msg = f"result {index} is not an object"
            raise EvidenceFormatError(msg)
        _require_exact(result, REPORT_RESULT_FIELDS, subject=f"result {index}")
        for field in ("peak_group_rss", "min_host_available", "host_total"):
            decode_measurement(result[field], subject=f"result {index}.{field}")
        _validate_cgroup(result["cgroup"], subject=f"result {index}.cgroup")
