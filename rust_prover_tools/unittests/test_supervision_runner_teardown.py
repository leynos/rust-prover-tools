"""Hold cancellation, monitor failure, and report publication to their rules.

The core runner tests own continuation, accounting, and classification; these
own what happens when a run is cancelled or its monitor fails, and how the
report is published when a sink fails. They share the same support module.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import datetime as dt
import json
import os
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
    StopReason,
    run_supervised,
    validate_report_document,
)
from rust_prover_tools.supervision.config import DiagnosticSinks
from rust_prover_tools.unittests.supervision_doubles import (
    Script,
    ScriptedLauncher,
    ScriptedProbe,
    VirtualClock,
    spec,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    NullObserver,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    async_test as _async_test,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    config as _config,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    environment as _environment,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.supervision import (
        AttemptEvidence,
        LaunchCommand,
        LineObserver,
        RunReport,
    )


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
    results = typ.cast("list[dict[str, object]]", document["results"])
    assert len(results) == 1
    # The published report must keep the completed result, not a not-run slot.
    assert results[0]["harness"] == spec().harness
    assert results[0]["status"] == "passed"


class _FixedPid:
    """Report a fixed process-group id to a running handle."""

    pid: int | None = 4321
    settled_exit_code: int | None = None
    failure: BaseException | None = None


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
    failure: BaseException | None = None


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
async def test_a_monitor_failure_stops_and_joins_the_child() -> None:
    """A failing monitor must not leave an unsupervised process group.

    The child's tracked task does not finish when it observes cancellation; it
    waits on an event the test releases. The run must therefore still be
    pending after teardown starts, which shows the runner waited for the join
    rather than returning as soon as it requested a stop.
    """
    teardown_started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class _TrackingLauncher(ScriptedLauncher):
        """A launcher whose child's task holds teardown open."""

        def start(
            self,
            spec: HarnessSpec,
            build: cabc.Callable[[HarnessSpec], LaunchCommand],
            on_line: cabc.Callable[[object], None],
        ) -> RunningHarness:
            """Wrap the scripted run so teardown can be held."""
            running = super().start(spec, build, on_line)
            task = running._task

            async def tracked() -> LaunchResult:
                """Wait for the test before finishing cancellation."""
                try:
                    return await task
                except asyncio.CancelledError:
                    teardown_started.set()
                    await release.wait()
                    finished.set()
                    raise

            return RunningHarness(asyncio.create_task(tracked()), running._recorder)

    environment = _environment(
        _TrackingLauncher(script=Script(block=True)),
        VirtualClock(),
        probe=_ExplodingProbe(),
    )
    run = asyncio.create_task(
        run_supervised(
            (spec(),),
            _config(timeout_seconds=60.0, sample_interval_seconds=1.0),
            environment,
        )
    )
    await asyncio.wait_for(teardown_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not run.done(), "the run returned before its child finished teardown"

    release.set()
    with pytest.raises(RuntimeError, match="probe failed"):
        await run

    assert finished.is_set()


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


@_async_test
async def test_a_cancellation_during_the_monitor_join_is_re_raised() -> None:
    """A cancel swallowed while joining the monitor still stops the run."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class _StubbornSleeper:
        """A sleeper that holds its own cancellation until released."""

        async def sleep(self, seconds: float) -> None:
            """Enter the monitor tick and wait to be released."""
            del seconds
            entered.set()
            try:
                await asyncio.wait_for(release.wait(), timeout=60)
            except asyncio.CancelledError:
                await release.wait()

    clock = VirtualClock()
    jsonl = MemorySink()
    environment = _environment(
        ScriptedLauncher(script=Script()),
        clock,
        sinks=Diagnostics(jsonl=jsonl),
        sleeper=_StubbornSleeper(),
        statuses={spec().qualified: ExecutionStatus.INTERRUPTED},
    )
    task = asyncio.create_task(
        run_supervised((spec(),), _config(timeout_seconds=600.0), environment)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    finish = next(
        record for record in jsonl.records if record["record"] == "harness-finish"
    )
    assert finish["status"] == "interrupted"
    assert finish["stop_reason"] == "interrupted"
    run_finish = next(
        record for record in jsonl.records if record["record"] == "run-finish"
    )
    assert run_finish["interrupted"] is True


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


def test_a_cancelled_attempt_accounts_for_a_mismatched_observation(
    tmp_path: Path,
) -> None:
    """Accounting uses what the verifier selected, not what was requested."""
    asyncio.run(_mismatched_cancellation_scenario(tmp_path))


async def _mismatched_cancellation_scenario(tmp_path: Path) -> None:
    """Cancel a run whose child selected a different harness."""
    from rust_prover_tools.supervision.config import DiagnosticSinks
    from rust_prover_tools.supervision.kani import KaniAdapter

    clock = VirtualClock()
    ready = asyncio.Event()
    first, second = spec("harnesses::a"), spec("harnesses::b")
    launcher = ScriptedLauncher(
        script=Script(
            lines=(("stdout", "Checking harness harnesses::other..."),),
            block=True,
        ),
        ready=ready,
    )
    report_path = tmp_path / "report.json"
    config = dc.replace(
        _config(timeout_seconds=600.0),
        sinks=DiagnosticSinks(report_path=report_path),
    )
    environment = _environment(
        launcher,
        clock,
        sinks=Diagnostics(jsonl=MemorySink()),
        adapter=KaniAdapter(version="0.67.0"),
    )
    task = asyncio.create_task(run_supervised((first, second), config, environment))
    await asyncio.wait_for(ready.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    document = json.loads(report_path.read_text(encoding="utf-8"))
    accounting = typ.cast("dict[str, list[str]]", document["accounting"])
    assert accounting["unexpected"] == ["example::harnesses::other"]
    assert first.qualified in accounting["missing"]


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


@_async_test
async def test_a_custom_sink_failure_does_not_replace_a_cancellation() -> None:
    """Any secondary publication failure keeps the original exception.

    The sink protocol does not restrict implementations to OSError, so a
    runtime failure while the cancellation unwinds must be suppressed too.
    """
    ready = asyncio.Event()
    launcher = ScriptedLauncher(script=Script(block=True), ready=ready)
    environment = _environment(
        launcher,
        VirtualClock(),
        sinks=Diagnostics(jsonl=_FailsOnRunFinishWith(RuntimeError("sink broke"))),
    )
    task = asyncio.create_task(
        run_supervised((spec(),), _config(timeout_seconds=600.0), environment)
    )
    await asyncio.wait_for(ready.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


class _FailsOnRunFinishWith:
    """A JSONL sink that raises a chosen exception on the closing record."""

    def __init__(self, error: BaseException) -> None:
        """Remember the failure to raise."""
        self._error = error

    @property
    def offset(self) -> int:
        """Report no bytes, because this sink stores nothing."""
        return 0

    def write(self, record: dict[str, object]) -> None:
        """Raise on the closing record."""
        if record.get("record") == "run-finish":
            raise self._error

    def flush(self) -> None:
        """Do nothing; writes fail synchronously."""


class _SettledPid:
    """Report a fixed pid and the status the owned group settled with."""

    pid: int | None = 4321
    settled_exit_code: int | None = -15
    failure: BaseException | None = None


def test_a_stopped_run_reports_the_status_the_group_settled_with() -> None:
    """A signalled child's settled status is evidence, not an unknown."""

    async def scenario() -> None:
        """Stop the child and read the status its settle reported."""
        started = asyncio.Event()

        async def child() -> LaunchResult:
            """Wait to be stopped, as a supervised child does."""
            started.set()
            await asyncio.sleep(60)
            return LaunchResult(exit_code=0)

        task = asyncio.create_task(child())
        await started.wait()
        running = RunningHarness(task, _SettledPid())
        running.stop(StopReason.TIMEOUT)
        await asyncio.sleep(0)

        result = await running.wait()

        assert result.exit_code == -15
        assert result.signal_number == 15

    asyncio.run(scenario())


def test_a_monitor_fault_keeps_the_settled_exit_evidence(tmp_path: Path) -> None:
    """A stopped child's settled status survives the monitor's failure."""
    asyncio.run(_monitor_fault_settled_scenario(tmp_path))


async def _monitor_fault_settled_scenario(tmp_path: Path) -> None:
    """Fail the probe, then inspect the stored settled status."""
    from rust_prover_tools.supervision.kani import KaniAdapter

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
            ),
            settled_exit_code=-15,
        ),
        clock,
        probe=_ExplodingProbe(),
        sinks=Diagnostics(jsonl=jsonl),
        adapter=KaniAdapter(version="0.67.0"),
    )

    with pytest.raises(_ProbeFailedError, match="probe failed"):
        await run_supervised((spec(),), config, environment)

    finish = next(
        record for record in jsonl.records if record["record"] == "harness-finish"
    )
    assert finish["status"] == "interrupted"
    assert finish["exit_code"] == -15
    assert finish["signal_number"] == 15
    document = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_document(document)
    results = typ.cast("list[dict[str, object]]", document["results"])
    assert results[0]["status"] == "interrupted"
    assert results[0]["exit_code"] == -15
    assert results[0]["signal_number"] == 15


def test_a_callback_failure_keeps_the_settled_exit_evidence(
    tmp_path: Path,
) -> None:
    """A recorder failure outranks the status but must not erase it."""
    asyncio.run(_callback_failure_settled_scenario(tmp_path))


async def _callback_failure_settled_scenario(tmp_path: Path) -> None:
    """Fail a live observer, then inspect the stored settled status."""
    from rust_prover_tools.supervision.kani import KaniAdapter, KaniObserver

    class _FailingObserver(KaniObserver):
        """A live observer that fails on the first selection record."""

        def observe(self, line: OutputLine) -> None:
            """Fail as a full spool would, before recording the line."""
            if line.text.startswith("Checking harness "):
                msg = "the spool is full"
                raise OSError(msg)
            super().observe(line)

    class _FailingAdapter(KaniAdapter):
        """The real Kani adapter with a failing live observer."""

        def observer(self, spec: HarnessSpec) -> KaniObserver:
            """Return the observer that fails on the selection record."""
            del spec
            return _FailingObserver()

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
            ),
            settled_exit_code=-15,
        ),
        clock,
        sinks=Diagnostics(jsonl=jsonl),
        adapter=_FailingAdapter(version="0.67.0"),
    )

    with pytest.raises(OSError, match="the spool is full"):
        await run_supervised((spec(),), config, environment)

    finish = next(
        record for record in jsonl.records if record["record"] == "harness-finish"
    )
    assert finish["status"] == "interrupted"
    assert finish["exit_code"] == -15
    assert finish["signal_number"] == 15
    document = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_document(document)
    results = typ.cast("list[dict[str, object]]", document["results"])
    assert results[0]["status"] == "interrupted"
    assert results[0]["exit_code"] == -15
    assert results[0]["signal_number"] == 15


def test_a_report_writer_failure_does_not_replace_a_cancellation(
    tmp_path: Path,
) -> None:
    """A publication failure while the cancellation unwinds is suppressed."""
    ready = asyncio.Event()
    report_path = tmp_path / "report.json"
    calls: list[tuple[Path, RunReport]] = []

    def writer(path: Path, report: RunReport) -> None:
        """Record the attempt, then fail as a full destination would."""
        calls.append((path, report))
        msg = "report writer failed"
        raise OSError(msg)

    environment = dc.replace(
        _environment(
            ScriptedLauncher(script=Script(block=True), ready=ready),
            VirtualClock(),
        ),
        report_writer=writer,
    )
    config = dc.replace(
        _config(timeout_seconds=600.0),
        sinks=DiagnosticSinks(report_path=report_path),
    )

    async def scenario() -> None:
        """Cancel the run once its child is launched."""
        task = asyncio.create_task(run_supervised((spec(),), config, environment))
        await asyncio.wait_for(ready.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    # The writer still received the configured path and the report that
    # existed when the cancellation unwound; the cancellation stayed primary.
    ((path, report),) = calls
    assert path == report_path
    assert report.interrupted is True
    assert [result.spec for result in report.results] == [spec()]
    assert report.results[0].diagnostic is not None


def test_a_report_writer_failure_does_not_replace_a_monitor_failure(
    tmp_path: Path,
) -> None:
    """A publication failure must not mask the monitor's own fault."""
    report_path = tmp_path / "report.json"
    calls: list[tuple[Path, RunReport]] = []

    def writer(path: Path, report: RunReport) -> None:
        """Record the attempt, then fail as a full destination would."""
        calls.append((path, report))
        msg = "report writer failed"
        raise OSError(msg)

    environment = dc.replace(
        _environment(
            ScriptedLauncher(script=Script(block=True)),
            VirtualClock(),
            probe=_ExplodingProbe(),
        ),
        report_writer=writer,
    )
    config = dc.replace(
        _config(timeout_seconds=60.0, sample_interval_seconds=1.0),
        sinks=DiagnosticSinks(report_path=report_path),
    )

    with pytest.raises(RuntimeError, match="probe failed"):
        asyncio.run(run_supervised((spec(),), config, environment))

    ((path, report),) = calls
    assert path == report_path
    assert report.interrupted is True
    assert [result.spec for result in report.results] == [spec()]


def _blocking_child(tmp_path: Path) -> tuple[Path, Path]:
    """Write a child that reports its pid, prints one line, then blocks."""
    script = tmp_path / "blocking.py"
    pid_file = tmp_path / "child.pid"
    script.write_text(
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stdout.write('first\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    return script, pid_file


class _FailsOnFirstStdoutLine:
    """Fail the first stdout line, recording whether the child was alive."""

    def __init__(self, pid_file: Path) -> None:
        """Remember where the child wrote its pid."""
        self._pid_file = pid_file
        self.delivered: list[str] = []
        self.alive_at_failure: list[bool] = []

    def __call__(self, item: object) -> None:
        """Fail as a full spool would, while the child is still running."""
        line = typ.cast("typ.Any", item)
        if line.stream != "stdout" or line.text != "first":
            return
        self.delivered.append(line.text)
        pid = int(self._pid_file.read_text(encoding="utf-8"))
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            self.alive_at_failure.append(False)
        else:
            self.alive_at_failure.append(True)
        msg = "the spool is full"
        raise OSError(msg)


def test_a_live_callback_failure_is_raised_and_the_report_validates(
    tmp_path: Path,
) -> None:
    """A real launcher's evidence failure reaches the caller and still reports.

    The supervisor sees the failure as a monitor fault: it stops and joins the
    attempt, accounts for the unrecorded harnesses, publishes a schema-valid
    report, then re-raises the original failure.
    """
    import sys

    from rust_prover_tools.supervision.clock import AsyncSleeper, SystemClock
    from rust_prover_tools.supervision.kani import KaniAdapter, KaniObserver
    from rust_prover_tools.supervision.launch_command import LaunchCommand
    from rust_prover_tools.supervision.launcher import CuprumHarnessLauncher
    from rust_prover_tools.supervision.run_environment import RunEnvironment
    from rust_prover_tools.unittests.supervision_doubles import (
        ScriptedCgroup as _ScriptedCgroup,
    )
    from rust_prover_tools.unittests.supervision_doubles import (
        ScriptedProbe as _ScriptedProbe,
    )

    script, pid_file = _blocking_child(tmp_path)
    fail = _FailsOnFirstStdoutLine(pid_file)

    def build(identity: HarnessSpec) -> LaunchCommand:
        """Run the blocking script from the test's directory."""
        del identity
        return LaunchCommand((sys.executable, "-I", str(script)), cwd=tmp_path)

    class _FailingObserver(KaniObserver):
        """A live observer that fails on the child's first stdout line."""

        def observe(self, line: OutputLine) -> None:
            """Record the intended line, fail, and otherwise forward."""
            fail(line)
            super().observe(line)

    class _FailingAdapter(KaniAdapter):
        """The real Kani adapter, with a failing live observer."""

        def observer(self, spec: HarnessSpec) -> KaniObserver:
            """Return the observer that fails on the first stdout line."""
            del spec
            return _FailingObserver()

    clock = SystemClock()
    launcher = CuprumHarnessLauncher(
        clock=clock.now,
        monotonic=clock.monotonic,
    )
    report_path = tmp_path / "report.json"
    environment = RunEnvironment(
        clock=clock,
        launcher=launcher,
        probe=_ScriptedProbe(),
        cgroup=_ScriptedCgroup(),
        diagnostics=Diagnostics(jsonl=MemorySink()),
        build=build,
        adapter=_FailingAdapter(version="0.67.0"),
        sleeper=AsyncSleeper(),
    )
    config = dc.replace(
        _config(
            timeout_seconds=30.0,
            sample_interval_seconds=30.0,
            heartbeat_interval_seconds=30.0,
        ),
        sinks=DiagnosticSinks(report_path=report_path),
    )

    with pytest.raises(OSError, match="the spool is full"):
        asyncio.run(run_supervised((spec(),), config, environment))

    # The failing write came from the child's stdout while that child was
    # alive and blocked, so the launcher stopped a real long-lived process
    # rather than losing a write from one that had already exited.
    assert fail.delivered == ["first"]
    assert fail.alive_at_failure == [True]
    document = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_document(document)
    results = typ.cast("list[dict[str, object]]", document["results"])
    assert len(results) == 1
    assert results[0]["harness"] == spec().harness
    assert results[0]["status"] == "interrupted"
    assert results[0]["stop_reason"] == "interrupted"
    assert report_path.exists()


def test_the_report_writer_is_injected_without_touching_the_filesystem(
    tmp_path: Path,
) -> None:
    """A recording writer receives the report; no file is created."""
    import asyncio as _asyncio

    recorded: list[tuple[Path, RunReport]] = []

    def writer(path: Path, report: RunReport) -> None:
        """Record the publication instead of writing."""
        recorded.append((path, report))

    clock = VirtualClock()
    environment = dc.replace(
        _environment(ScriptedLauncher(clock=clock), clock),
        report_writer=writer,
    )
    report_path = tmp_path / "report.json"
    config = dc.replace(
        _config(),
        sinks=DiagnosticSinks(report_path=report_path),
    )

    report = _asyncio.run(run_supervised((spec(),), config, environment))

    assert recorded == [(report_path, report)]
    assert not report_path.exists()
