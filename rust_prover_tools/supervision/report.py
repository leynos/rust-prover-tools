"""Render a supervised run as a versioned report and a short table.

The table is for a human reading a job log after the fact. It deliberately
carries the identity, status, duration, sampled peak group RSS, minimum host
available memory, exit status, and a short detail, because those are the facts
that let a reader tell a proof failure from a resource stop. The JSON report is
for a machine and carries every field the typed result holds.

The notes are part of the report, not an afterthought. A sampled peak can miss a
spike, summed RSS can count a shared page once per process, the samples include
compilation, and the cgroup values are shared with the rest of the job. A
reader who mistakes any of those for an exclusive per-harness measurement would
draw a stronger conclusion than the evidence supports.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import typing as typ
from pathlib import Path

from rust_prover_tools.supervision.events import (
    cgroup_document,
    measurement_document,
)
from rust_prover_tools.supervision.models import ExecutionStatus, Measurement
from rust_prover_tools.supervision.sinks import utc_text

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.results import HarnessResult, RunReport

__all__ = (
    "REPORT_NOTES",
    "REPORT_SCHEMA_VERSION",
    "render_table",
    "report_document",
    "write_report",
)

#: Version of the final JSON report schema.
REPORT_SCHEMA_VERSION: typ.Final[int] = 1

#: Limitations every report states so its measurements are not over-read.
REPORT_NOTES: typ.Final[tuple[str, ...]] = (
    (
        "Peak group RSS is the largest periodic sample, so a spike between "
        "samples can be missed."
    ),
    (
        "Summed RSS counts a shared page once per process that maps it, so it "
        "is an upper bound on unique resident memory."
    ),
    "Samples bracket the whole invocation and therefore include compilation.",
    (
        "Process-group sampling covers only processes that stayed in the group "
        "the supervisor owns."
    ),
    (
        "Cgroup values belong to the cgroup the supervisor is in and are "
        "normally shared with the rest of the job, so they are not exclusive "
        "per-harness measurements and their lifetime peaks are not harness "
        "peaks."
    ),
)


def _mib(value: Measurement) -> str:
    """Render a byte measurement as MiB, or name why it is unavailable."""
    if value.value is None:
        return f"unavailable({value.unavailable_reason})"
    return f"{value.value / (1024 * 1024):.1f} MiB"


def _exit_text(exit_code: int | None, signal_number: int | None) -> str:
    """Render a raw status, naming a signal when one ended the child."""
    if signal_number is not None:
        return f"signal {signal_number}"
    if exit_code is None:
        return "-"
    return str(exit_code)


def _row(result: HarnessResult) -> tuple[str, ...]:
    """Render one result as a table row.

    The harness column keeps Kani's fully qualified pretty name. Two modules in
    one package may each define a harness with the same leaf name, so trimming
    to the leaf would make two rows indistinguishable.
    """
    return (
        result.spec.package,
        result.spec.harness,
        result.status.value,
        f"{result.duration_seconds:.1f}s",
        _mib(result.peak_group_rss),
        _mib(result.min_host_available),
        _exit_text(result.exit_code, result.signal_number),
        result.detail.replace("\n", " ").replace("\r", " "),
    )


_HEADER: typ.Final[tuple[str, ...]] = (
    "PACKAGE",
    "HARNESS",
    "STATUS",
    "DURATION",
    "PEAK-RSS",
    "MIN-AVAIL",
    "EXIT",
    "DETAIL",
)


def render_table(report: RunReport) -> tuple[str, ...]:
    """Render the deterministic summary table for one report.

    Parameters
    ----------
    report : RunReport
        Report to render.

    Returns
    -------
    tuple[str, ...]
        Header, separator, one row per result, a totals line, and the
        measurement notes. No line wraps: a long detail widens its column.
    """
    rows = [_HEADER, *(_row(result) for result in report.results)]
    widths = [max(len(row[column]) for row in rows) for column in range(len(_HEADER))]
    lines = [
        "  ".join(
            cell.ljust(widths[column]) for column, cell in enumerate(row)
        ).rstrip()
        for row in rows
    ]
    lines.insert(1, "  ".join("-" * width for width in widths))
    lines.append(_totals(report))
    lines.extend(f"note: {note}" for note in REPORT_NOTES)
    return tuple(lines)


def _totals(report: RunReport) -> str:
    """Count results by documented status."""
    counts = {
        status: sum(1 for result in report.results if result.status is status)
        for status in ExecutionStatus
    }
    rendered = ", ".join(
        f"{count} {status.value}" for status, count in counts.items() if count
    )
    return (
        f"total: {len(report.results)} harness(es): {rendered}; "
        f"outcome={report.outcome.value}"
    )


def _result_document(result: HarnessResult) -> dict[str, object]:
    """Render one typed result as report JSON."""
    return {
        "package": result.spec.package,
        "harness": result.spec.harness,
        "attempt": result.spec.attempt,
        "executed": result.executed,
        "status": result.status,
        "stop_reason": result.stop_reason,
        "exit_code": result.exit_code,
        "signal_number": result.signal_number,
        "proof_verdict": result.proof_verdict,
        "started_at": (
            utc_text(result.started_at) if result.started_at is not None else None
        ),
        "finished_at": (
            utc_text(result.finished_at) if result.finished_at is not None else None
        ),
        "duration_seconds": round(result.duration_seconds, 6),
        "peak_group_rss": measurement_document(result.peak_group_rss),
        "min_host_available": measurement_document(result.min_host_available),
        "host_total": measurement_document(result.host_total),
        "cgroup": cgroup_document(result.cgroup),
        "last_output": result.last_output,
        "last_progress": result.last_progress,
        "diagnostic": (
            None
            if result.diagnostic is None
            else {"sink": result.diagnostic.sink, "offset": result.diagnostic.offset}
        ),
        "detail": result.detail,
    }


def report_document(report: RunReport) -> dict[str, object]:
    """Render one typed report as the versioned JSON document.

    Parameters
    ----------
    report : RunReport
        Report to render.

    Returns
    -------
    dict[str, object]
        A JSON-compatible mapping with no live objects.
    """
    return {
        "schema": REPORT_SCHEMA_VERSION,
        "run_id": report.run_id,
        "started_at": utc_text(report.started_at),
        "finished_at": (
            utc_text(report.finished_at) if report.finished_at is not None else None
        ),
        "outcome": report.outcome,
        "complete": report.is_complete,
        "passed": report.passed,
        "interrupted": report.interrupted,
        "interrupt_signal": report.interrupt_signal,
        "expected": [
            {
                "package": spec.package,
                "harness": spec.harness,
                "attempt": spec.attempt,
            }
            for spec in report.expected
        ],
        "accounting": {
            "missing": list(report.accounting.missing),
            "duplicate": list(report.accounting.duplicate),
            "unexpected": list(report.accounting.unexpected),
        },
        "results": [_result_document(result) for result in report.results],
        "notes": list(REPORT_NOTES),
    }


def write_report(path: Path, report: RunReport) -> None:
    """Write the JSON report atomically.

    Parameters
    ----------
    path : Path
        Destination path.
    report : RunReport
        Report to write.

    Notes
    -----
    The document is written to a sibling temporary file, flushed, synced, and
    renamed over the destination. An interrupted write therefore leaves the
    previous report intact rather than a half-written one.
    """
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                report_document(report),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(destination)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(temporary).unlink()
        raise
