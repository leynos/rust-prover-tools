"""Value checks for one JSONL record, apart from the report reader.

The key sets live in ``schema_fields``; this module says what a JSONL record's
identity, enumerated, and numeric values may be, so a correctly keyed record
cannot carry an invented status or a negative duration.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.supervision.models import (
    ExecutionStatus,
    ProofVerdict,
    StopReason,
)
from rust_prover_tools.supervision.results import RunOutcome
from rust_prover_tools.supervision.schema_values import (
    EvidenceFormatError,
    decode_measurement,
    require_bool,
    require_enum,
    require_int,
    require_optional_int,
    require_seconds,
    require_str,
    validate_accounting,
    validate_cgroup,
)

__all__ = ("validate_record", "validate_record_values")

#: The streams a record may name.
_STREAMS: typ.Final[frozenset[object]] = frozenset({"stdout", "stderr"})

#: Enumerated fields of a harness-finish record and the type they must name.
_FINISH_ENUMS: typ.Final[tuple[tuple[str, type[typ.Any]], ...]] = (
    ("status", ExecutionStatus),
    ("stop_reason", StopReason),
    ("proof_verdict", ProofVerdict),
)
#: Enumerated fields of a run-finish record.
_RUN_FINISH_ENUMS: typ.Final[tuple[tuple[str, type[typ.Any]], ...]] = (
    ("outcome", RunOutcome),
)


def validate_record(
    kind: str,
    document: dict[str, object],
    *,
    subject: str,
) -> None:
    """Validate one record's measurements and its own values."""
    _validate_record_measurements(kind, document, subject=subject)
    validate_record_values(kind, document, subject=subject)


def validate_record_values(
    kind: str,
    document: dict[str, object],
    *,
    subject: str,
) -> None:
    """Validate the identity, enumerated and numeric values of one record.

    The key sets say which fields exist; this says what they may contain, so a
    correctly keyed record with an invented status or a negative elapsed time
    cannot pass the reader.
    """
    if kind == "truncated":
        require_str(document["marker"], subject=f"{subject}.marker")
        for field in ("run_id", "original_kind"):
            if field in document:
                require_str(document[field], subject=f"{subject}.{field}")
        return
    require_str(document["run_id"], subject=f"{subject}.run_id")
    # A harness-finish record for a harness that never ran has no finish time,
    # which is the one record kind whose producer can emit a null timestamp.
    require_str(
        document["timestamp"],
        subject=f"{subject}.timestamp",
        optional=kind == "harness-finish",
    )
    if kind == "run-start":
        _validate_run_start(document, subject=subject)
    elif kind == "run-finish":
        _validate_run_finish(document, subject=subject)
    elif kind == "harness-finish":
        _validate_harness_identity(document, subject=subject)
        _validate_harness_finish(document, subject=subject)
    else:
        _validate_harness_identity(document, subject=subject)
        _validate_stream_record(kind, document, subject=subject)


def _validate_harness_identity(
    document: dict[str, object],
    *,
    subject: str,
) -> None:
    """Validate the package, harness and attempt shared by harness records."""
    for field in ("package", "harness"):
        require_str(document[field], subject=f"{subject}.{field}")
    require_int(document["attempt"], subject=f"{subject}.attempt", minimum=1)


def _validate_stream_record(
    kind: str,
    document: dict[str, object],
    *,
    subject: str,
) -> None:
    """Validate an output or heartbeat record's own values."""
    require_seconds(document["elapsed_seconds"], subject=f"{subject}.elapsed_seconds")
    if kind == "output":
        stream = document["stream"]
        if not isinstance(stream, str) or stream not in _STREAMS:
            msg = f"{subject}.stream is not a documented value: {stream!r}"
            raise EvidenceFormatError(msg)
        # The producer always writes the decoded line, which may be empty but
        # is never absent; null would make a consumer's join ambivalent.
        require_str(document["text"], subject=f"{subject}.text", emptiable=True)
    elif kind == "heartbeat":
        require_seconds(document["idle_seconds"], subject=f"{subject}.idle_seconds")
        require_str(
            document["last_progress"],
            subject=f"{subject}.last_progress",
            optional=True,
        )


def _validate_run_start(document: dict[str, object], *, subject: str) -> None:
    """Validate a run-start record's packages and intervals."""
    packages = document["packages"]
    if not isinstance(packages, list) or not all(
        isinstance(name, str) and name for name in packages
    ):
        msg = f"{subject}.packages is not an array of non-empty strings"
        raise EvidenceFormatError(msg)
    require_int(
        document["harness_count"], subject=f"{subject}.harness_count", minimum=0
    )
    for field in (
        "timeout_seconds",
        "sample_interval_seconds",
        "heartbeat_interval_seconds",
    ):
        require_seconds(document[field], subject=f"{subject}.{field}")
    require_optional_int(document["watchdog_mib"], subject=f"{subject}.watchdog_mib")


def _validate_run_finish(document: dict[str, object], *, subject: str) -> None:
    """Validate a run-finish record's enumerated and Boolean values."""
    for field, allowed in _RUN_FINISH_ENUMS:
        require_enum(document[field], allowed, subject=f"{subject}.{field}")
    for field in ("complete", "interrupted"):
        require_bool(document[field], subject=f"{subject}.{field}")
    require_optional_int(
        document["interrupt_signal"], subject=f"{subject}.interrupt_signal"
    )


def _validate_harness_finish(document: dict[str, object], *, subject: str) -> None:
    """Validate a harness-finish record's enumerated and optional values."""
    for field, allowed in _FINISH_ENUMS:
        value = document[field]
        if field == "stop_reason" and value is None:
            # A not-run result has no stop reason; the producer writes it.
            continue
        require_enum(value, allowed, subject=f"{subject}.{field}")
    for field in ("exit_code", "signal_number"):
        require_optional_int(document[field], subject=f"{subject}.{field}")
    require_seconds(document["duration_seconds"], subject=f"{subject}.duration_seconds")
    for field in ("last_output", "last_progress"):
        require_str(document[field], subject=f"{subject}.{field}", optional=True)
    require_str(document["detail"], subject=f"{subject}.detail", optional=True)
    if document["detail"] is None:
        msg = f"{subject}.detail is missing"
        raise EvidenceFormatError(msg)


def _validate_harness_identity(
    document: dict[str, object],
    *,
    subject: str,
) -> None:
    """Validate the package, harness and attempt shared by harness records."""
    for field in ("package", "harness"):
        require_str(document[field], subject=f"{subject}.{field}")
    require_int(document["attempt"], subject=f"{subject}.attempt", minimum=1)


def _validate_stream_record(
    kind: str,
    document: dict[str, object],
    *,
    subject: str,
) -> None:
    """Validate an output or heartbeat record's own values."""
    require_seconds(document["elapsed_seconds"], subject=f"{subject}.elapsed_seconds")
    if kind == "output":
        stream = document["stream"]
        if not isinstance(stream, str) or stream not in _STREAMS:
            msg = f"{subject}.stream is not a documented value: {stream!r}"
            raise EvidenceFormatError(msg)
        # The producer always writes the decoded line, which may be empty but
        # is never absent; null would make a consumer's join ambivalent.
        require_str(document["text"], subject=f"{subject}.text", emptiable=True)
    elif kind == "heartbeat":
        require_seconds(document["idle_seconds"], subject=f"{subject}.idle_seconds")
        require_str(
            document["last_progress"],
            subject=f"{subject}.last_progress",
            optional=True,
        )


def _validate_run_start(document: dict[str, object], *, subject: str) -> None:
    """Validate a run-start record's packages and intervals."""
    packages = document["packages"]
    if not isinstance(packages, list) or not all(
        isinstance(name, str) and name for name in packages
    ):
        msg = f"{subject}.packages is not an array of non-empty strings"
        raise EvidenceFormatError(msg)
    require_int(
        document["harness_count"], subject=f"{subject}.harness_count", minimum=0
    )
    for field in (
        "timeout_seconds",
        "sample_interval_seconds",
        "heartbeat_interval_seconds",
    ):
        require_seconds(document[field], subject=f"{subject}.{field}")
    require_optional_int(document["watchdog_mib"], subject=f"{subject}.watchdog_mib")


def _validate_run_finish(document: dict[str, object], *, subject: str) -> None:
    """Validate a run-finish record's enumerated and Boolean values."""
    for field, allowed in _RUN_FINISH_ENUMS:
        require_enum(document[field], allowed, subject=f"{subject}.{field}")
    for field in ("complete", "interrupted"):
        require_bool(document[field], subject=f"{subject}.{field}")
    require_optional_int(
        document["interrupt_signal"], subject=f"{subject}.interrupt_signal"
    )


def _validate_harness_finish(document: dict[str, object], *, subject: str) -> None:
    """Validate a harness-finish record's enumerated and optional values."""
    for field, allowed in _FINISH_ENUMS:
        value = document[field]
        if field == "stop_reason" and value is None:
            # A not-run result has no stop reason; the producer writes it.
            continue
        require_enum(value, allowed, subject=f"{subject}.{field}")
    for field in ("exit_code", "signal_number"):
        require_optional_int(document[field], subject=f"{subject}.{field}")
    require_seconds(document["duration_seconds"], subject=f"{subject}.duration_seconds")
    for field in ("last_output", "last_progress"):
        require_str(document[field], subject=f"{subject}.{field}", optional=True)
    require_str(document["detail"], subject=f"{subject}.detail", optional=True)
    if document["detail"] is None:
        msg = f"{subject}.detail is missing"
        raise EvidenceFormatError(msg)


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
        validate_accounting(document["accounting"], subject=f"{subject}.accounting")
        return
    for field in ("group_rss", "host_available", "host_total"):
        if field in document:
            decode_measurement(document[field], subject=f"{subject}.{field}")
