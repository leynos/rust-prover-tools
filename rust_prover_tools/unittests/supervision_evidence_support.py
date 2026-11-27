"""Shared record and report builders for the evidence tests."""

from __future__ import annotations

from rust_prover_tools.supervision import (
    ExecutionStatus,
    HarnessResult,
    HarnessSpec,
    InventoryDrift,
    Measurement,
    ProofVerdict,
    RunOutcome,
    RunReport,
    StopReason,
)
from rust_prover_tools.unittests.supervision_doubles import VirtualClock

_UNAVAILABLE = {"unavailable_reason": "not sampled"}


def _record(**fields: object) -> dict[str, object]:
    """Return a minimal run-start record with the given overrides."""
    base: dict[str, object] = {
        "record": "run-start",
        "run_id": "run-1",
        "timestamp": "2026-11-21T12:00:00.000Z",
        "packages": ["example"],
        "harness_count": 1,
        "timeout_seconds": 1.0,
        "sample_interval_seconds": 1.0,
        "heartbeat_interval_seconds": 1.0,
        "watchdog_mib": 1024,
    }
    base.update(fields)
    return base


def _result() -> HarnessResult:
    """Return one complete passing result."""
    return HarnessResult(
        spec=HarnessSpec("example", "harnesses::verified"),
        status=ExecutionStatus.PASSED,
        stop_reason=StopReason.EXITED,
        exit_code=0,
        signal_number=None,
        proof_verdict=ProofVerdict.PASSED,
        started_at=VirtualClock().now(),
        finished_at=VirtualClock().now(),
        duration_seconds=1.25,
        peak_group_rss=Measurement.available(128 * 1024 * 1024),
        min_host_available=Measurement.available(8 * 1024 * 1024 * 1024),
        host_total=Measurement.available(16 * 1024 * 1024 * 1024),
        cgroup=None,
        last_output="VERIFICATION:- SUCCESSFUL",
        last_progress="Verification Time: 1.2s",
        diagnostic=None,
        detail="",
    )


def _report() -> RunReport:
    """Return one complete passing report."""
    clock = VirtualClock()
    return RunReport(
        schema_version=1,
        run_id="run-1",
        started_at=clock.now(),
        finished_at=clock.now(),
        expected=(HarnessSpec("example", "harnesses::verified"),),
        results=(_result(),),
        accounting=InventoryDrift(),
        outcome=RunOutcome.PASSED,
    )


def _valid_record(kind: str) -> dict[str, object]:
    """Return one minimal valid record of `kind`."""
    base: dict[str, object] = {
        "schema": 1,
        "record": kind,
        "run_id": "run-1",
        "timestamp": "2026-11-21T12:00:00.000Z",
        "package": "alpha",
        "harness": "a",
        "attempt": 1,
    }
    if kind != "harness-finish":
        base["elapsed_seconds"] = 0.5
    if kind == "output":
        base.update({"stream": "stdout", "text": "line"})
    elif kind == "heartbeat":
        base.update({
            "idle_seconds": 0.5,
            "group_rss": _UNAVAILABLE,
            "host_available": _UNAVAILABLE,
            "last_progress": None,
        })
    elif kind == "harness-finish":
        base.update({
            "status": "passed",
            "stop_reason": "exited",
            "exit_code": 0,
            "signal_number": None,
            "proof_verdict": "passed",
            "duration_seconds": 1.0,
            "peak_group_rss": _UNAVAILABLE,
            "min_host_available": _UNAVAILABLE,
            "host_total": _UNAVAILABLE,
            "cgroup": {
                "path": None,
                "start": None,
                "finish": None,
                "oom_kill_delta": None,
                "unavailable_reason": "not mounted",
            },
            "last_output": None,
            "last_progress": None,
            "detail": "",
        })
    else:
        raise ValueError(kind)
    return base
