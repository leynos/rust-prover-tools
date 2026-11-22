"""Validate the versioned JSONL and report schemas strictly.

The readers reject an unknown or missing key, an unknown record kind or
schema version, and a measurement that is neither available nor explained.
The schema is documented in ``docs/kani-supervised-execution.md``.
"""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.supervision.models import (
    ExecutionStatus,
    ProofVerdict,
    StopReason,
)
from rust_prover_tools.supervision.results import RunOutcome
from rust_prover_tools.supervision.schema_fields import (
    JSONL_FIELDS,
    REPORT_FIELDS,
    REPORT_RESULT_FIELDS,
)
from rust_prover_tools.supervision.schema_values import (
    EvidenceFormatError,
    decode_measurement,
    require_bool,
    require_enum,
    require_exact,
    require_int,
    require_optional,
    require_optional_int,
    require_seconds,
    require_str,
    validate_cgroup,
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


#: The optional context a truncation record may carry.
_TRUNCATED_CONTEXT: typ.Final[frozenset[str]] = frozenset({
    "original_kind",
    "run_id",
})


def _require_schema(document: dict[str, object], subject: str) -> None:
    """Require an exact integer schema version, not a bool or float."""
    value = document.get("schema")
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{subject} has a non-integer schema {value!r}"
        raise EvidenceFormatError(msg)
    if value != _SCHEMA_VERSION:
        msg = f"{subject} has schema {value!r}"
        raise EvidenceFormatError(msg)


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
    _require_schema(document, f"record {number}")
    kind = document.get("record")
    if not isinstance(kind, str) or kind not in JSONL_FIELDS:
        msg = f"record {number} has unknown kind {kind!r}"
        raise EvidenceFormatError(msg)
    fields = JSONL_FIELDS[kind]
    if kind == "truncated":
        require_optional(document, fields | _TRUNCATED_CONTEXT, required=fields)
    else:
        require_exact(document, fields, subject=f"record {number}")
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
        validate_cgroup(document["cgroup"], subject=f"{subject}.cgroup")
        return
    if kind == "run-finish":
        _validate_accounting(document["accounting"])
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
    _require_schema(document, "the report")
    require_exact(document, REPORT_FIELDS, subject="the report")
    require_str(document["run_id"], subject="the report run_id")
    require_str(document["started_at"], subject="the report started_at")
    require_str(
        document["finished_at"], subject="the report finished_at", optional=True
    )
    require_enum(document["outcome"], RunOutcome, subject="the report outcome")
    for field in ("complete", "passed", "interrupted"):
        require_bool(document[field], subject=f"the report {field}")
    require_optional_int(
        document["interrupt_signal"], subject="the report interrupt_signal"
    )
    notes = document["notes"]
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        msg = "the report notes are not an array of strings"
        raise EvidenceFormatError(msg)
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
        require_exact(
            entry,
            frozenset({"package", "harness", "attempt"}),
            subject=f"expected entry {index}",
        )
        for field in ("package", "harness"):
            value = entry[field]
            if not isinstance(value, str) or not value:
                msg = f"expected entry {index}.{field} is not a non-empty string"
                raise EvidenceFormatError(msg)
        attempt = entry["attempt"]
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            msg = f"expected entry {index}.attempt is not an integer"
            raise EvidenceFormatError(msg)
        if attempt < 1:
            msg = f"expected entry {index}.attempt is not positive"
            raise EvidenceFormatError(msg)


def _validate_accounting(accounting: object) -> None:
    """Require the accounting object to carry the three drift lists."""
    if not isinstance(accounting, dict):
        msg = "the report accounting is not an object"
        raise EvidenceFormatError(msg)
    require_exact(
        accounting,
        frozenset({"missing", "duplicate", "unexpected"}),
        subject="the report accounting",
    )
    for field in ("missing", "duplicate", "unexpected"):
        if not isinstance(accounting[field], list):
            msg = f"the report accounting {field} is not an array"
            raise EvidenceFormatError(msg)


def _validate_results(results: object) -> None:
    """Require every result to carry the documented fields and measurements."""
    if not isinstance(results, list):
        msg = "the report results list is not an array"
        raise EvidenceFormatError(msg)
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            msg = f"result {index} is not an object"
            raise EvidenceFormatError(msg)
        require_exact(result, REPORT_RESULT_FIELDS, subject=f"result {index}")
        require_str(result["package"], subject=f"result {index}.package")
        require_str(result["harness"], subject=f"result {index}.harness")
        require_int(result["attempt"], subject=f"result {index}.attempt", minimum=1)
        require_bool(result["executed"], subject=f"result {index}.executed")
        require_enum(
            result["status"], ExecutionStatus, subject=f"result {index}.status"
        )
        stop_reason = result["stop_reason"]
        if stop_reason is not None:
            require_enum(
                stop_reason,
                StopReason,
                subject=f"result {index}.stop_reason",
            )
        require_optional_int(result["exit_code"], subject=f"result {index}.exit_code")
        require_optional_int(
            result["signal_number"], subject=f"result {index}.signal_number"
        )
        require_enum(
            result["proof_verdict"],
            ProofVerdict,
            subject=f"result {index}.proof_verdict",
        )
        require_str(
            result["started_at"],
            subject=f"result {index}.started_at",
            optional=True,
        )
        require_str(
            result["finished_at"],
            subject=f"result {index}.finished_at",
            optional=True,
        )
        require_seconds(
            result["duration_seconds"],
            subject=f"result {index}.duration_seconds",
        )
        for field in ("last_output", "last_progress", "detail"):
            require_str(result[field], subject=f"result {index}.{field}", optional=True)
        for field in ("peak_group_rss", "min_host_available", "host_total"):
            decode_measurement(result[field], subject=f"result {index}.{field}")
        validate_cgroup(result["cgroup"], subject=f"result {index}.cgroup")
        _validate_diagnostic(result["diagnostic"], subject=f"result {index}.diagnostic")


def _validate_diagnostic(document: object, *, subject: str) -> None:
    """Require a diagnostic reference to name a sink and an integer offset."""
    if document is None:
        return
    if not isinstance(document, dict):
        msg = f"{subject} is not an object"
        raise EvidenceFormatError(msg)
    require_exact(document, frozenset({"sink", "offset"}), subject=subject)
    if not isinstance(document["sink"], str):
        msg = f"{subject}.sink is not a string"
        raise EvidenceFormatError(msg)
    offset = document["offset"]
    if not isinstance(offset, int) or isinstance(offset, bool):
        msg = f"{subject}.offset is not an integer"
        raise EvidenceFormatError(msg)
    if offset < 0:
        msg = f"{subject}.offset is negative"
        raise EvidenceFormatError(msg)
