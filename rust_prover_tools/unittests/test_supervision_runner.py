"""Hold the supervised run to continuation, accounting, and classification."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    HarnessClassification,
    HarnessSpec,
    Measurement,
    MemorySink,
    OutputLine,
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
    from rust_prover_tools.supervision import AttemptEvidence

from rust_prover_tools.unittests.supervision_runner_support import (
    async_test as _async_test,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    classify as _classify,
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
    ready = asyncio.Event()
    launcher = ScriptedLauncher(script=Script(block=True), ready=ready)
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
    await asyncio.wait_for(ready.wait(), timeout=1)
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


@_async_test
async def test_a_watchdog_stop_retains_its_triggering_sample() -> None:
    """The observation that caused a stop is part of the final evidence."""
    clock = VirtualClock()
    probe = ScriptedProbe(available_values=(Measurement.available(512 * 1024 * 1024),))
    environment = _environment(
        ScriptedLauncher(script=Script(block=True)),
        clock,
        probe=probe,
        statuses={spec().qualified: ExecutionStatus.MEMORY_EXHAUSTED},
    )

    report = await run_supervised(
        (spec(),),
        _config(
            timeout_seconds=100.0,
            sample_interval_seconds=60.0,
            heartbeat_interval_seconds=15.0,
        ),
        environment,
    )

    assert len(probe.samples) == 1
    assert report.results[0].min_host_available.value == 512 * 1024 * 1024


class _ProgressObserver:
    """Keep the last recognised report line, ignoring later noise."""

    def __init__(self) -> None:
        """Start before any recognised line."""
        self.last_progress: str | None = None

    def observe(self, line: OutputLine) -> None:
        """Record a verdict line and ignore incidental chatter."""
        if line.text.startswith("VERIFICATION:"):
            self.last_progress = line.text

    def finish(self) -> None:
        """Return a trivial observation; the classifier reads the evidence."""
        return

    def close(self) -> None:
        """Release nothing; the observer owns no resource."""
        return


@dc.dataclass(frozen=True, slots=True)
class _ProgressAdapter:
    """An adapter backed by one explicit observer."""

    observer_value: _ProgressObserver

    def observer(self, spec: HarnessSpec) -> _ProgressObserver:
        """Return the test's observer for every attempt."""
        del spec
        return self.observer_value

    def classify(
        self,
        evidence: AttemptEvidence,
        observation: object,
    ) -> HarnessClassification:
        """Classify the stopped attempt as a timeout."""
        del evidence, observation
        return _classify(ExecutionStatus.TIMED_OUT)


@_async_test
async def test_a_heartbeat_prefers_recognised_progress_over_later_noise() -> None:
    """A warning after recognised progress does not become the heartbeat's."""
    clock = VirtualClock()
    jsonl = MemorySink()
    observer = _ProgressObserver()
    launcher = ScriptedLauncher(
        script=Script(
            lines=(
                ("stdout", "Checking harness harnesses::a"),
                ("stdout", "VERIFICATION: SUCCESSFUL"),
                ("stdout", "warning: incidental build chatter"),
            ),
            block=True,
        ),
        clock=clock,
        seconds_per_line=2.0,
    )
    environment = _environment(
        launcher,
        clock,
        sinks=Diagnostics(jsonl=jsonl),
        adapter=_ProgressAdapter(observer),
        statuses={spec().qualified: ExecutionStatus.TIMED_OUT},
    )

    await run_supervised(
        (spec(),),
        _config(
            timeout_seconds=10.0,
            sample_interval_seconds=2.0,
            heartbeat_interval_seconds=1.0,
        ),
        environment,
    )

    heartbeats = [record for record in jsonl.records if record["record"] == "heartbeat"]
    assert heartbeats[-1]["last_progress"] == "VERIFICATION: SUCCESSFUL"
