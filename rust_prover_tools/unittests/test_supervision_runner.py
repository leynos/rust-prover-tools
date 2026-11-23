"""Hold the supervised run to continuation, accounting, and classification."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import datetime as dt
import json
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    HarnessClassification,
    HarnessSpec,
    LaunchResult,
    Measurement,
    MemorySink,
    NullSink,
    OutputLine,
    ProofVerdict,
    ResourceSample,
    RunningHarness,
    RunOutcome,
    StopReason,
    run_supervised,
    validate_report_document,
)
from rust_prover_tools.supervision.config import DiagnosticSinks
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
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.supervision import (
        AttemptEvidence,
        LaunchCommand,
        LineObserver,
    )

from rust_prover_tools.unittests.supervision_runner_support import (
    NullObserver,
)
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
        observer: LineObserver,
    ) -> HarnessClassification:
        """Classify the stopped attempt as a timeout."""
        del evidence, observer
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


class _FailOnRunFinish:
    """A JSONL sink that fails only when the run closes."""

    @property
    def offset(self) -> int:
        """Report no bytes, because this sink stores nothing."""
        return 0

    def write(self, record: dict[str, object]) -> None:
        """Refuse the closing record, as a full disk would."""
        if record.get("record") == "run-finish":
            msg = "the evidence destination is full"
            raise OSError(msg)

    def flush(self) -> None:
        """Do nothing; writes already flush or fail synchronously."""


def test_a_diagnostic_failure_still_publishes_the_report(
    tmp_path: Path,
) -> None:
    """The report destination does not depend on the live sinks."""
    asyncio.run(_diagnostic_failure_scenario(tmp_path))


async def _diagnostic_failure_scenario(tmp_path: Path) -> None:
    """Run one passing harness whose JSONL sink fails at finalization."""
    clock = VirtualClock()
    launcher = ScriptedLauncher(script=Script(exit_code=0))
    environment = _environment(
        launcher,
        clock,
        sinks=Diagnostics(human=NullSink(), jsonl=_FailOnRunFinish()),
    )
    report_path = tmp_path / "report.json"
    config = dc.replace(
        _config(timeout_seconds=10.0),
        sinks=DiagnosticSinks(report_path=report_path),
    )

    with pytest.raises(OSError, match="full"):
        await run_supervised((spec(),), config, environment)

    document = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_document(document)


class _FailOnHarnessFinish:
    """A JSONL sink that fails when one harness closes."""

    @property
    def offset(self) -> int:
        """Report no bytes, because this sink stores nothing."""
        return 0

    def write(self, record: dict[str, object]) -> None:
        """Refuse a harness-finish record, as a broken pipe would."""
        if record.get("record") == "harness-finish":
            msg = "the harness sink failed"
            raise OSError(msg)

    def flush(self) -> None:
        """Do nothing; writes fail synchronously."""


def test_a_harness_finish_failure_still_publishes_the_report(
    tmp_path: Path,
) -> None:
    """A completed result is not discarded when its live record fails."""
    asyncio.run(_harness_finish_failure_scenario(tmp_path))


async def _harness_finish_failure_scenario(tmp_path: Path) -> None:
    """Run one passing harness whose finish record cannot be written."""
    clock = VirtualClock()
    launcher = ScriptedLauncher(script=Script(exit_code=0))
    environment = _environment(
        launcher,
        clock,
        sinks=Diagnostics(human=NullSink(), jsonl=_FailOnHarnessFinish()),
    )
    report_path = tmp_path / "report.json"
    config = dc.replace(
        _config(timeout_seconds=10.0),
        sinks=DiagnosticSinks(report_path=report_path),
    )

    with pytest.raises(OSError, match="harness sink failed"):
        await run_supervised((spec(),), config, environment)

    document = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_document(document)
    assert len(document["results"]) == 1


class _FixedPid:
    """Report a fixed process-group id to a running handle."""

    pid: int | None = 4321
    settled_exit_code: int | None = None


def test_cancelling_one_waiter_does_not_cancel_the_launch() -> None:
    """The monitor and runner can both wait on one child safely."""

    async def scenario() -> None:
        """Cancel one waiter and prove the child is still alive."""
        release = asyncio.Event()

        async def child() -> LaunchResult:
            """Wait for the test to release the child."""
            await release.wait()
            return LaunchResult(exit_code=0)

        task = asyncio.create_task(child())
        running = RunningHarness(task, _FixedPid())
        waiter = asyncio.create_task(running.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter

        assert not task.cancelled()
        release.set()
        assert (await running.wait()).exit_code == 0

    asyncio.run(scenario())


class _SettlingPid:
    """Report a fixed pid and no settled status."""

    pid: int | None = 4321
    settled_exit_code: int | None = None


def test_a_cancelled_waiter_does_not_report_an_unsettled_stop() -> None:
    """A stop is not reported until Cuprum has settled the owned group."""

    async def scenario() -> None:
        """Cancel one waiter while the group is still being torn down."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def child() -> LaunchResult:
            """Enter teardown on cancellation and settle when released."""
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await release.wait()
                raise
            return LaunchResult(exit_code=0)

        task = asyncio.create_task(child())
        await started.wait()
        running = RunningHarness(task, _SettlingPid())
        running.stop(StopReason.TIMEOUT)
        await asyncio.sleep(0)

        waiter = asyncio.create_task(running.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)

        assert not waiter.done(), "the waiter reported before the group settled"
        release.set()
        result = await asyncio.wait_for(waiter, timeout=1)
        assert task.done()
        assert result.exit_code is None
        assert result.signal_number is None

    asyncio.run(scenario())


@_async_test
async def test_a_monitor_failure_stops_and_joins_the_child() -> None:
    """A failing monitor must not leave an unsupervised process group."""
    stopped = asyncio.Event()

    class _TrackingLauncher(ScriptedLauncher):
        """A launcher that records when its child's task is cancelled."""

        def start(
            self,
            spec: HarnessSpec,
            build: cabc.Callable[[HarnessSpec], LaunchCommand],
            on_line: cabc.Callable[[object], None],
        ) -> RunningHarness:
            """Wrap the scripted run so cancellation is observable."""
            running = super().start(spec, build, on_line)
            task = running._task

            async def tracked() -> LaunchResult:
                """Record cancellation before re-raising it."""
                try:
                    return await task
                except asyncio.CancelledError:
                    stopped.set()
                    raise

            return RunningHarness(asyncio.create_task(tracked()), running._recorder)

    environment = _environment(
        _TrackingLauncher(script=Script(block=True)),
        VirtualClock(),
        probe=_ExplodingProbe(),
    )

    with pytest.raises(RuntimeError, match="probe failed"):
        await run_supervised(
            (spec(),),
            _config(timeout_seconds=60.0, sample_interval_seconds=1.0),
            environment,
        )

    assert stopped.is_set()


@_async_test
async def test_cancelling_the_run_stops_the_child_and_finalizes() -> None:
    """A cancelled run reports the in-flight attempt, not work that never ran."""
    clock = VirtualClock()
    ready = asyncio.Event()
    launcher = ScriptedLauncher(script=Script(block=True), ready=ready)
    first, second = spec("harnesses::a"), spec("harnesses::b")
    jsonl = MemorySink()
    environment = _environment(
        launcher,
        clock,
        sinks=Diagnostics(jsonl=jsonl),
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
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert StopReason.INTERRUPTED in launcher.stops
    finish = next(
        record for record in jsonl.records if record["record"] == "run-finish"
    )
    assert finish["interrupted"] is True
    assert finish["complete"] is False
    accounting = typ.cast("dict[str, object]", finish["accounting"])
    assert accounting["missing"] == [second.qualified]
    assert accounting["unexpected"] == []


class _ReportingProbe:
    """A probe that fails after recording one sample, and captures the report."""

    def __init__(self) -> None:
        """Create a probe with no failure yet."""
        self.fail = False

    def sample(
        self,
        spec: HarnessSpec,
        *,
        group_id: int | None,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> ResourceSample:
        """Fail once the test asks for it."""
        del spec, group_id, started_at, started_monotonic
        if self.fail:
            msg = "probe failed"
            raise RuntimeError(msg)
        return ResourceSample(
            timestamp_utc=dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.UTC),
            elapsed_seconds=1.0,
            package="p",
            harness="harnesses::verified",
            attempt=1,
            group_rss=Measurement.available(1),
            host_available=Measurement.available(2),
            host_total=Measurement.available(3),
        )


@_async_test
async def test_a_monitor_failure_still_finalizes_the_report() -> None:
    """INV-FINALIZE applies to a fault, not only to an ordinary ending."""
    clock = VirtualClock()
    probe = _ReportingProbe()
    jsonl = MemorySink()
    first, second = spec("harnesses::a"), spec("harnesses::b")
    environment = _environment(
        launcher := ScriptedLauncher(script=Script(block=True)),
        clock,
        probe=probe,
        sinks=Diagnostics(jsonl=jsonl),
    )

    async def run() -> None:
        """Fail the probe after the run has started."""
        probe.fail = True
        with pytest.raises(RuntimeError, match="probe failed"):
            await run_supervised(
                (first, second),
                _config(timeout_seconds=60.0, sample_interval_seconds=1.0),
                environment,
            )

    await run()

    kinds = [record["record"] for record in jsonl.records]
    assert "run-finish" in kinds
    assert StopReason.INTERRUPTED in launcher.stops
    finish = next(
        record for record in jsonl.records if record["record"] == "run-finish"
    )
    assert finish["complete"] is False
    accounting = typ.cast("dict[str, object]", finish["accounting"])
    assert accounting["missing"] == [second.qualified]


class _ObservingAdapter:
    """An adapter that reports scripted observed identities."""

    def __init__(self, observed: tuple[str, ...]) -> None:
        """Record the observed identities every attempt reports."""
        self._observed = observed

    def observer(self, spec: HarnessSpec) -> LineObserver:
        """Return an observer that records nothing."""
        del spec
        return NullObserver()

    def classify(
        self,
        evidence: AttemptEvidence,
        observer: LineObserver,
    ) -> HarnessClassification:
        """Report the scripted observation alongside a passing status."""
        del observer
        return HarnessClassification(
            status=ExecutionStatus.PASSED,
            proof_verdict=ProofVerdict.PASSED,
            observed=tuple(
                f"{evidence.spec.package}::{name}" for name in self._observed
            ),
        )


@_async_test
async def test_a_mismatched_observation_is_accounted_as_drift() -> None:
    """An identity the verifier selected but nobody requested is drift."""
    environment = _environment(
        ScriptedLauncher(),
        VirtualClock(),
        adapter=_ObservingAdapter(("harnesses::other",)),
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert report.accounting.unexpected == (f"{spec().package}::harnesses::other",)
    assert spec().qualified in report.accounting.missing
    assert not report.is_complete


@_async_test
async def test_a_duplicated_observation_is_accounted_as_drift() -> None:
    """The same identity observed twice is a duplicate execution."""
    environment = _environment(
        ScriptedLauncher(),
        VirtualClock(),
        adapter=_ObservingAdapter((spec().harness, spec().harness)),
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert report.accounting.duplicate == (spec().qualified,)
    assert not report.is_complete


@_async_test
async def test_no_observation_is_accounted_as_missing() -> None:
    """A child that selected nothing leaves the expected identity missing."""
    environment = _environment(
        ScriptedLauncher(),
        VirtualClock(),
        adapter=_ObservingAdapter(()),
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert report.accounting.missing == (spec().qualified,)
    assert not report.is_complete


@_async_test
async def test_a_matching_observation_is_complete() -> None:
    """The requested identity observed exactly once is not drift."""
    environment = _environment(
        ScriptedLauncher(),
        VirtualClock(),
        adapter=_ObservingAdapter((spec().harness,)),
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert report.accounting.is_empty
    assert report.is_complete


def test_a_cancelled_attempt_is_reported_as_interrupted(tmp_path: Path) -> None:
    """The reported result names the stop, not an ordinary exit."""
    asyncio.run(_cancelled_attempt_scenario(tmp_path))


async def _cancelled_attempt_scenario(tmp_path: Path) -> None:
    """Cancel a run and inspect its report and JSONL evidence."""
    from rust_prover_tools.supervision.config import DiagnosticSinks
    from rust_prover_tools.supervision.kani import KaniAdapter

    clock = VirtualClock()
    ready = asyncio.Event()
    launcher = ScriptedLauncher(
        script=Script(
            lines=(
                ("stdout", "Checking harness harnesses::a..."),
                ("stdout", "VERIFICATION:- SUCCESSFUL"),
            ),
            block=True,
        ),
        ready=ready,
    )
    first, second = spec("harnesses::a"), spec("harnesses::b")
    jsonl = MemorySink()
    report_path = tmp_path / "report.json"
    config = dc.replace(
        _config(timeout_seconds=600.0),
        sinks=DiagnosticSinks(report_path=report_path),
    )
    environment = _environment(
        launcher,
        clock,
        sinks=Diagnostics(jsonl=jsonl),
        adapter=KaniAdapter(version="0.67.0"),
    )
    task = asyncio.create_task(run_supervised((first, second), config, environment))
    await asyncio.wait_for(ready.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    finish = next(
        record
        for record in jsonl.records
        if record["record"] == "harness-finish" and record["harness"] == first.harness
    )
    assert finish["status"] == "interrupted"
    assert finish["stop_reason"] == "interrupted"
    document = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_document(document)
    results = typ.cast("list[dict[str, object]]", document["results"])
    assert [entry["status"] for entry in results] == ["interrupted", "not-run"]
    assert results[0]["stop_reason"] == "interrupted"
    assert results[0]["diagnostic"] is not None
    accounting = typ.cast("dict[str, object]", document["accounting"])
    assert accounting["missing"] == [second.qualified]
    assert accounting["unexpected"] == []


def test_a_monitor_fault_is_reported_as_interrupted(tmp_path: Path) -> None:
    """A fault the supervisor caused is recorded as its own stop."""
    asyncio.run(_monitor_fault_scenario(tmp_path))


async def _monitor_fault_scenario(tmp_path: Path) -> None:
    """Fail the probe and inspect the reported stop reason."""
    from rust_prover_tools.supervision.kani import KaniAdapter

    class _FailingProbe(ScriptedProbe):
        """A probe that fails on every sample."""

        def sample(self, spec: HarnessSpec, **kwargs: object) -> ResourceSample:
            """Raise the fault the monitor must survive."""
            del spec, kwargs
            msg = "probe failed"
            raise RuntimeError(msg)

    from rust_prover_tools.supervision.config import DiagnosticSinks

    clock = VirtualClock()
    jsonl = MemorySink()
    report_path = tmp_path / "report.json"
    config = dc.replace(
        _config(timeout_seconds=60.0, sample_interval_seconds=1.0),
        sinks=DiagnosticSinks(report_path=report_path),
    )
    environment = _environment(
        ScriptedLauncher(
            script=Script(
                lines=(("stdout", "Checking harness harnesses::verified..."),),
                block=True,
            )
        ),
        clock,
        probe=_FailingProbe(),
        sinks=Diagnostics(jsonl=jsonl),
        adapter=KaniAdapter(version="0.67.0"),
    )

    with pytest.raises(RuntimeError, match="probe failed"):
        await run_supervised((spec(),), config, environment)

    finish = next(
        record for record in jsonl.records if record["record"] == "harness-finish"
    )
    assert finish["status"] == "interrupted"
    assert finish["stop_reason"] == "interrupted"
    document = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_document(document)
    results = typ.cast("list[dict[str, object]]", document["results"])
    assert results[0]["status"] == "interrupted"
    assert results[0]["diagnostic"] is not None
    accounting = typ.cast("dict[str, object]", document["accounting"])
    assert accounting["missing"] == []
    assert accounting["unexpected"] == []
