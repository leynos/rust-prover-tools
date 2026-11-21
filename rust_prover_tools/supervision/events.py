"""Build the versioned evidence records a run writes.

Every record names its kind in ``record`` and carries the run identifier plus
the package-qualified harness identity. That is what lets a reader join a
heartbeat, a sample, the last output line, and a termination reason to the
harness they describe after a runner loss, without reading the whole log.

The builders are pure: they take typed values and return a mapping. The sinks
own serialization and flushing, and the runner owns the order in which records
are emitted.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.sinks import utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from rust_prover_tools.supervision.config import SupervisionConfig
    from rust_prover_tools.supervision.models import (
        CgroupMemory,
        CgroupObservation,
        HarnessSpec,
        Measurement,
        OutputLine,
        ResourceSample,
    )
    from rust_prover_tools.supervision.results import HarnessResult, RunReport

__all__ = (
    "RunStart",
    "cgroup_document",
    "harness_finish_record",
    "harness_start_record",
    "heartbeat_record",
    "measurement_document",
    "output_record",
    "run_finish_record",
    "run_start_record",
    "sample_record",
)


def measurement_document(measurement: Measurement) -> dict[str, object]:
    """Render one measurement as a tagged JSON object.

    Parameters
    ----------
    measurement : Measurement
        Value to render.

    Returns
    -------
    dict[str, object]
        ``{"value": N}`` when available, otherwise
        ``{"unavailable_reason": "..."}``.
    """
    if measurement.is_available:
        return {"value": measurement.value}
    return {"unavailable_reason": measurement.unavailable_reason}


def cgroup_document(observation: CgroupObservation | None) -> dict[str, object] | None:
    """Render one cgroup observation for a report or record.

    Parameters
    ----------
    observation : CgroupObservation | None
        Observation to render.

    Returns
    -------
    dict[str, object] | None
        A mapping with the path, start, finish, and ``oom_kill`` delta, or
        `None` when no observation was taken.
    """
    if observation is None:
        return None
    return {
        "path": observation.path,
        "start": _cgroup_memory_document(observation.start),
        "finish": _cgroup_memory_document(observation.finish),
        "oom_kill_delta": observation.oom_kill_delta,
        "unavailable_reason": observation.unavailable_reason,
    }


def _cgroup_memory_document(memory: CgroupMemory | None) -> dict[str, object] | None:
    """Render one instant of cgroup memory values."""
    if memory is None:
        return None
    return {
        "current": measurement_document(memory.current),
        "peak": measurement_document(memory.peak),
        "oom_kill": measurement_document(memory.oom_kill),
    }


@dc.dataclass(frozen=True, slots=True)
class RunStart:
    """The run-level facts a start record states.

    Attributes
    ----------
    run_id : str
        Unique run identifier.
    timestamp : datetime.datetime
        Run start time.
    packages : cabc.Sequence[str]
        Selected packages, in selection order.
    harness_count : int
        Number of harnesses the run expects.
    config : SupervisionConfig
        Policy the run applies.
    """

    run_id: str
    timestamp: dt.datetime
    packages: cabc.Sequence[str]
    harness_count: int
    config: SupervisionConfig


def run_start_record(start: RunStart) -> dict[str, object]:
    """Build the flushed record emitted before the first harness."""
    config = start.config
    watchdog_mib = (
        None
        if config.watchdog_threshold_bytes is None
        else config.watchdog_threshold_bytes // (1024 * 1024)
    )
    return {
        "record": "run-start",
        "run_id": start.run_id,
        "timestamp": utc_text(start.timestamp),
        "packages": list(start.packages),
        "harness_count": start.harness_count,
        "timeout_seconds": config.timeout_seconds,
        "sample_interval_seconds": config.sample_interval_seconds,
        "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
        "watchdog_mib": watchdog_mib,
    }


def harness_start_record(
    *,
    run_id: str,
    spec: HarnessSpec,
    timestamp: dt.datetime,
) -> dict[str, object]:
    """Build the flushed start record for one invocation."""
    return {
        "record": "harness-start",
        "run_id": run_id,
        "package": spec.package,
        "harness": spec.harness,
        "attempt": spec.attempt,
        "timestamp": utc_text(timestamp),
        "elapsed_seconds": 0.0,
    }


def output_record(
    *, run_id: str, spec: HarnessSpec, line: OutputLine
) -> dict[str, object]:
    """Build one streamed child-output record."""
    return {
        "record": "output",
        "run_id": run_id,
        "package": spec.package,
        "harness": spec.harness,
        "attempt": spec.attempt,
        "timestamp": utc_text(line.timestamp_utc),
        "elapsed_seconds": round(line.elapsed_seconds, 6),
        "stream": line.stream,
        "text": line.text,
    }


def sample_record(*, run_id: str, sample: ResourceSample) -> dict[str, object]:
    """Build one resource-sample record."""
    return {
        "record": "sample",
        "run_id": run_id,
        "package": sample.package,
        "harness": sample.harness,
        "attempt": sample.attempt,
        "timestamp": utc_text(sample.timestamp_utc),
        "elapsed_seconds": round(sample.elapsed_seconds, 6),
        "group_rss": measurement_document(sample.group_rss),
        "host_available": measurement_document(sample.host_available),
        "host_total": measurement_document(sample.host_total),
    }


def heartbeat_record(
    *,
    run_id: str,
    sample: ResourceSample,
    idle_seconds: float,
    last_progress: str | None,
) -> dict[str, object]:
    """Build one periodic heartbeat, emitted even when the prover is silent."""
    return {
        "record": "heartbeat",
        "run_id": run_id,
        "package": sample.package,
        "harness": sample.harness,
        "attempt": sample.attempt,
        "timestamp": utc_text(sample.timestamp_utc),
        "elapsed_seconds": round(sample.elapsed_seconds, 6),
        "idle_seconds": round(max(0.0, idle_seconds), 6),
        "group_rss": measurement_document(sample.group_rss),
        "host_available": measurement_document(sample.host_available),
        "last_progress": last_progress,
    }


def harness_finish_record(
    *,
    run_id: str,
    result: HarnessResult,
) -> dict[str, object]:
    """Build the record that closes one harness's evidence."""
    return {
        "record": "harness-finish",
        "run_id": run_id,
        "package": result.spec.package,
        "harness": result.spec.harness,
        "attempt": result.spec.attempt,
        "timestamp": (
            utc_text(result.finished_at) if result.finished_at is not None else None
        ),
        "status": result.status,
        "stop_reason": result.stop_reason,
        "exit_code": result.exit_code,
        "signal_number": result.signal_number,
        "proof_verdict": result.proof_verdict,
        "duration_seconds": round(result.duration_seconds, 6),
        "peak_group_rss": measurement_document(result.peak_group_rss),
        "min_host_available": measurement_document(result.min_host_available),
        "host_total": measurement_document(result.host_total),
        "cgroup": cgroup_document(result.cgroup),
        "last_output": result.last_output,
        "last_progress": result.last_progress,
        "detail": result.detail,
    }


def run_finish_record(
    *,
    run_id: str,
    timestamp: dt.datetime,
    report: RunReport,
) -> dict[str, object]:
    """Build the record that closes a run's evidence."""
    return {
        "record": "run-finish",
        "run_id": run_id,
        "timestamp": utc_text(timestamp),
        "outcome": report.outcome,
        "complete": report.is_complete,
        "interrupted": report.interrupted,
        "interrupt_signal": report.interrupt_signal,
        "accounting": {
            "missing": list(report.accounting.missing),
            "duplicate": list(report.accounting.duplicate),
            "unexpected": list(report.accounting.unexpected),
        },
    }
