"""Hold the supervised run to continuation, accounting, and classification."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    HarnessSpec,
    Measurement,
    MemorySink,
    ResourceSample,
    RunOutcome,
    StopReason,
    run_supervised,
)
from rust_prover_tools.supervision.run_finalize import ReportInput, assemble_report
from rust_prover_tools.unittests.supervision_doubles import (
    Script,
    ScriptedLauncher,
    ScriptedProbe,
    StepInterrupts,
    VirtualClock,
    VirtualSleeper,
    spec,
)

if typ.TYPE_CHECKING:
    import datetime as dt

from rust_prover_tools.unittests.supervision_runner_support import (
    async_test as _async_test,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    config as _config,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    environment as _environment,
)


@_async_test
async def test_a_silent_passing_child_still_produces_a_heartbeat() -> None:
    """A harness that prints nothing must still report that it is alive."""
    clock = VirtualClock()
    jsonl = MemorySink()
    launcher = ScriptedLauncher(script=Script(exit_code=0, block=True))
    environment = _environment(launcher, clock, sinks=Diagnostics(jsonl=jsonl))

    report = await run_supervised(
        (spec(),),
        _config(
            timeout_seconds=1.0,
            sample_interval_seconds=0.5,
            heartbeat_interval_seconds=0.5,
        ),
        environment,
    )

    kinds = [record["record"] for record in jsonl.records]
    assert "run-start" in kinds
    assert "harness-start" in kinds
    assert "heartbeat" in kinds
    assert "harness-finish" in kinds
    assert "run-finish" in kinds
    heartbeats = [record for record in jsonl.records if record["record"] == "heartbeat"]
    idle = typ.cast("float", heartbeats[0]["idle_seconds"])
    assert idle > 0
    assert report.results[0].stop_reason is StopReason.TIMEOUT


@_async_test
async def test_a_timeout_stops_the_owned_process_group() -> None:
    """A harness that outlives its budget is stopped, not merely observed."""
    clock = VirtualClock()
    launcher = ScriptedLauncher(script=Script(block=True))
    environment = _environment(
        launcher,
        clock,
        statuses={spec().qualified: ExecutionStatus.TIMED_OUT},
    )

    report = await run_supervised(
        (spec(),),
        _config(timeout_seconds=1.0, sample_interval_seconds=0.5),
        environment,
    )

    assert launcher.stops == [StopReason.TIMEOUT]
    assert report.results[0].status is ExecutionStatus.TIMED_OUT


@_async_test
async def test_the_watchdog_stop_is_attributable() -> None:
    """Falling below the host floor stops the harness for that reason."""
    clock = VirtualClock()
    launcher = ScriptedLauncher(script=Script(block=True))
    probe = ScriptedProbe(available_values=(Measurement.available(64 * 1024 * 1024),))
    environment = _environment(
        launcher,
        clock,
        probe=probe,
        statuses={spec().qualified: ExecutionStatus.MEMORY_EXHAUSTED},
    )

    report = await run_supervised(
        (spec(),),
        _config(timeout_seconds=60.0, sample_interval_seconds=1.0),
        environment,
    )

    assert launcher.stops == [StopReason.MEMORY_WATCHDOG]
    assert report.results[0].status is ExecutionStatus.MEMORY_EXHAUSTED
    assert report.results[0].min_host_available.value == 64 * 1024 * 1024


@_async_test
async def test_the_first_stop_reason_wins() -> None:
    """A timeout that fires first is not replaced by a later watchdog stop."""
    clock = VirtualClock()
    launcher = ScriptedLauncher(script=Script(block=True))
    probe = ScriptedProbe(available_values=(Measurement.available(1),))
    environment = _environment(launcher, clock, probe=probe)

    report = await run_supervised(
        (spec(),),
        _config(timeout_seconds=1.0, sample_interval_seconds=1.0),
        environment,
    )

    assert launcher.stops == [StopReason.TIMEOUT]
    assert report.results[0].stop_reason is StopReason.TIMEOUT


@_async_test
async def test_a_failure_does_not_stop_later_harnesses() -> None:
    """One failing proof must not hide the evidence about the others."""
    clock = VirtualClock()
    launcher = ScriptedLauncher()
    first, second = spec("harnesses::a"), spec("harnesses::b")
    environment = _environment(
        launcher,
        clock,
        statuses={
            first.qualified: ExecutionStatus.FAILED,
            second.qualified: ExecutionStatus.PASSED,
        },
    )

    report = await run_supervised((first, second), _config(), environment)

    assert [result.status for result in report.results] == [
        ExecutionStatus.FAILED,
        ExecutionStatus.PASSED,
    ]
    assert report.outcome is RunOutcome.FAILED
    assert report.is_complete


def test_a_missing_execution_makes_the_run_incomplete() -> None:
    """An expected identity that never ran is a multiset difference."""
    expected = (spec("harnesses::a"), spec("harnesses::b"))
    report = assemble_report(
        ReportInput(
            expected=expected,
            results=(),
            executed=("example::harnesses::a",),
            interrupted=False,
            interrupt_signal=None,
            run_id="f" * 16,
            started_at=VirtualClock().now(),
            finished_at=VirtualClock().now(),
        )
    )

    assert "example::harnesses::b" in report.accounting.missing
    assert report.outcome is RunOutcome.INCOMPLETE
    assert not report.passed


@_async_test
async def test_a_repeated_expectation_matched_exactly_is_complete() -> None:
    """An exactly matched repeated expectation is not drift."""
    clock = VirtualClock()
    launcher = ScriptedLauncher()
    environment = _environment(launcher, clock)
    duplicate = spec()

    report = await run_supervised(
        (duplicate, duplicate),
        _config(),
        environment,
    )

    assert report.accounting.duplicate == ()
    assert report.is_complete
    assert report.outcome is RunOutcome.PASSED


@_async_test
async def test_an_interrupted_run_reports_the_rest_as_not_run() -> None:
    """An interruption finalizes the report and accounts for unexecuted work."""
    clock = VirtualClock()
    interrupts = StepInterrupts()
    launcher = ScriptedLauncher(script=Script(block=True))
    first, second = spec("harnesses::a"), spec("harnesses::b")
    environment = _environment(
        launcher,
        clock,
        interrupts=interrupts,
        statuses={first.qualified: ExecutionStatus.INTERRUPTED},
    )
    task = asyncio.create_task(
        run_supervised(
            (first, second),
            _config(timeout_seconds=600.0),
            environment,
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
    interrupts.fire(15)
    report = await task

    assert report.interrupted
    assert report.interrupt_signal == 15
    assert report.outcome is RunOutcome.INTERRUPTED
    assert report.results[0].status is ExecutionStatus.INTERRUPTED
    assert report.results[1].status is ExecutionStatus.NOT_RUN
    assert not report.results[1].executed


@_async_test
async def test_a_launch_failure_is_an_error_not_a_proof_failure() -> None:
    """A child that never started cannot have produced a counterexample."""
    clock = VirtualClock()
    launcher = ScriptedLauncher(script=Script(launch_error="driver missing"))
    environment = _environment(
        launcher,
        clock,
        statuses={spec().qualified: ExecutionStatus.ERROR},
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert report.results[0].stop_reason is StopReason.LAUNCH_FAILED
    assert report.results[0].status is ExecutionStatus.ERROR


class _ProbeFailedError(RuntimeError):
    """The test probe failed while sampling."""

    def __init__(self) -> None:
        """Record the failure."""
        super().__init__("probe failed")


class _ExplodingProbe:
    """A probe whose first sample fails, to prove the monitor surfaces it."""

    def sample(
        self,
        spec: HarnessSpec,
        *,
        group_id: int | None,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> ResourceSample:
        """Raise instead of measuring."""
        del spec, group_id, started_at, started_monotonic
        raise _ProbeFailedError


@_async_test
async def test_the_monitor_samples_at_the_configured_interval() -> None:
    """Sample and heartbeat thresholds are elapsed times, not sleep durations."""
    clock = VirtualClock()
    sleeper = VirtualSleeper(clock)
    probe = ScriptedProbe()
    environment = _environment(
        ScriptedLauncher(script=Script(block=True), clock=clock),
        clock,
        probe=probe,
        sleeper=sleeper,
    )

    await run_supervised(
        (spec(),),
        _config(
            timeout_seconds=6.0,
            sample_interval_seconds=2.0,
            heartbeat_interval_seconds=2.0,
        ),
        environment,
    )

    assert sleeper.calls[:3] == [2.0, 2.0, 2.0]
    assert len(probe.samples) >= 3


@_async_test
async def test_a_monitor_failure_is_not_hidden_by_cancellation() -> None:
    """A failing sample raises instead of returning a silent cancellation."""
    environment = _environment(
        ScriptedLauncher(script=Script(block=True)),
        VirtualClock(),
        probe=_ExplodingProbe(),
    )

    with pytest.raises(RuntimeError, match="probe failed"):
        await run_supervised(
            (spec(),),
            _config(timeout_seconds=60.0, sample_interval_seconds=1.0),
            environment,
        )


@_async_test
async def test_a_launch_failure_is_not_counted_as_executed() -> None:
    """A child that never started is missing work, not a completed harness."""
    environment = _environment(
        ScriptedLauncher(script=Script(launch_error="missing")),
        VirtualClock(),
        statuses={spec().qualified: ExecutionStatus.ERROR},
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert not report.results[0].executed
    assert not report.is_complete
    assert spec().qualified in report.accounting.missing


class _InterruptingSink:
    """A sink that fires an interrupt when the harness finishes."""

    def __init__(self, interrupts: StepInterrupts) -> None:
        """Wrap an interrupt source and a memory sink."""
        self._interrupts = interrupts
        self._records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        """Record the payload and fire once the harness finishes."""
        self._records.append(record)
        if record.get("record") == "harness-finish":
            self._interrupts.fire(15)

    def flush(self) -> None:
        """Do nothing: the sink is in memory."""

    @property
    def offset(self) -> int:
        """A monotonic record count as a stand-in offset."""
        return len(self._records)


@_async_test
async def test_a_late_interrupt_is_refreshed_before_finalization() -> None:
    """A signal that arrives during the final finish record is not lost."""
    clock = VirtualClock()
    interrupts = StepInterrupts()
    environment = _environment(
        ScriptedLauncher(clock=clock),
        clock,
        interrupts=interrupts,
        sinks=Diagnostics(jsonl=_InterruptingSink(interrupts)),
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert report.interrupted
    assert report.interrupt_signal == 15
    assert report.outcome is RunOutcome.INTERRUPTED
