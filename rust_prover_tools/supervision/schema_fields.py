"""The required key sets of the versioned evidence schemas.

These tables are the schema. They live apart from the validation logic so a
producer and a reader can compare one definition, and so the reader stays a
sequence of checks rather than a wall of constants. Every key is required and
no unknown key is permitted for its record kind.
"""

from __future__ import annotations

import typing as typ

__all__ = (
    "JSONL_FIELDS",
    "REPORT_FIELDS",
    "REPORT_RESULT_FIELDS",
)


#: Required and permitted keys for one JSONL record of each kind.
JSONL_FIELDS: typ.Final[dict[str, frozenset[str]]] = {
    "run-start": frozenset({
        "schema",
        "record",
        "run_id",
        "timestamp",
        "packages",
        "harness_count",
        "timeout_seconds",
        "sample_interval_seconds",
        "heartbeat_interval_seconds",
        "watchdog_mib",
    }),
    "harness-start": frozenset({
        "schema",
        "record",
        "run_id",
        "package",
        "harness",
        "attempt",
        "timestamp",
        "elapsed_seconds",
    }),
    "output": frozenset({
        "schema",
        "record",
        "run_id",
        "package",
        "harness",
        "attempt",
        "timestamp",
        "elapsed_seconds",
        "stream",
        "text",
    }),
    "heartbeat": frozenset({
        "schema",
        "record",
        "run_id",
        "package",
        "harness",
        "attempt",
        "timestamp",
        "elapsed_seconds",
        "idle_seconds",
        "group_rss",
        "host_available",
        "last_progress",
    }),
    "sample": frozenset({
        "schema",
        "record",
        "run_id",
        "package",
        "harness",
        "attempt",
        "timestamp",
        "elapsed_seconds",
        "group_rss",
        "host_available",
        "host_total",
    }),
    "harness-finish": frozenset({
        "schema",
        "record",
        "run_id",
        "package",
        "harness",
        "attempt",
        "timestamp",
        "status",
        "stop_reason",
        "exit_code",
        "signal_number",
        "proof_verdict",
        "duration_seconds",
        "peak_group_rss",
        "min_host_available",
        "host_total",
        "cgroup",
        "last_output",
        "last_progress",
        "detail",
    }),
    # The minimal truncation record may omit its optional context fields.
    "truncated": frozenset({"schema", "record", "marker"}),
    "run-finish": frozenset({
        "schema",
        "record",
        "run_id",
        "timestamp",
        "outcome",
        "complete",
        "interrupted",
        "interrupt_signal",
        "accounting",
    }),
}

REPORT_RESULT_FIELDS: typ.Final[frozenset[str]] = frozenset({
    "package",
    "harness",
    "attempt",
    "executed",
    "status",
    "stop_reason",
    "exit_code",
    "signal_number",
    "proof_verdict",
    "started_at",
    "finished_at",
    "duration_seconds",
    "peak_group_rss",
    "min_host_available",
    "host_total",
    "cgroup",
    "last_output",
    "last_progress",
    "diagnostic",
    "detail",
})

#: Required and permitted top-level keys of the final report.
REPORT_FIELDS: typ.Final[frozenset[str]] = frozenset({
    "schema",
    "run_id",
    "started_at",
    "finished_at",
    "outcome",
    "complete",
    "passed",
    "interrupted",
    "interrupt_signal",
    "expected",
    "accounting",
    "results",
    "notes",
})
