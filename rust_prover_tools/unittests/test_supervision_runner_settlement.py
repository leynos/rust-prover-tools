"""Hold stopped runs to the status and samples their settlement reports."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import datetime as dt
import json
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    HarnessSpec,
    LaunchResult,
    Measurement,
    MemorySink,
    OutputLine,
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
    VirtualClock,
    spec,
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
from rust_prover_tools.unittests.supervision_runner_teardown_support import (
    _ExplodingProbe,
    _ProbeFailedError,
    _UnclosableAdapter,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


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


@_async_test
async def test_a_cancelled_run_keeps_the_samples_it_took() -> None:
    """A cancelled monitor still contributes the samples it had taken."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class _SamplingSleeper:
        """Advance the first sleep so a sample is due, then hold teardown."""

        def __init__(self) -> None:
            """Start before the first sleep."""
            self._calls = 0

        async def sleep(self, seconds: float) -> None:
            """Let one tick happen, then block until the test releases it."""
            self._calls += 1
            if self._calls == 1:
                clock.advance(max(0.0, seconds))
                return
            entered.set()
            try:
                await asyncio.wait_for(release.wait(), timeout=60)
            except asyncio.CancelledError:
                await release.wait()

    clock = VirtualClock()
    jsonl = MemorySink()
    environment = _environment(
        ScriptedLauncher(script=Script(block=True)),
        clock,
        sinks=Diagnostics(jsonl=jsonl),
        sleeper=_SamplingSleeper(),
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
    sample = typ.cast("dict[str, object]", finish["peak_group_rss"])
    assert sample.get("value") is not None, (
        "the cancelled monitor lost the sample it had taken"
    )


def test_an_orphaned_close_failure_is_not_swallowed(tmp_path: Path) -> None:
    """A close failure is reported and the completed attempt still recorded."""
    asyncio.run(_orphaned_close_failure_scenario(tmp_path))


async def _orphaned_close_failure_scenario(
    tmp_path: Path,
) -> None:
    """Run an attempt that settles cleanly but cannot close its observer."""
    clock = VirtualClock()
    launcher = ScriptedLauncher(
        script=Script(
            lines=(("stdout", "Checking harness harnesses::a..."),),
            exit_code=0,
        )
    )
    (identity,) = (spec("harnesses::a"),)
    config = dc.replace(_config(timeout_seconds=60.0), sinks=DiagnosticSinks())
    jsonl = MemorySink()
    report_path = tmp_path / "report.json"
    config = dc.replace(
        _config(timeout_seconds=60.0), sinks=DiagnosticSinks(report_path=report_path)
    )
    environment = _environment(
        launcher,
        clock,
        sinks=Diagnostics(jsonl=jsonl),
        adapter=_UnclosableAdapter(),
    )

    with pytest.raises(RuntimeError, match="could not be closed"):
        await run_supervised((identity,), config, environment)

    finish = [
        record for record in jsonl.records if record["record"] == "harness-finish"
    ]
    assert finish, "the completed attempt was erased by the close failure"
    document = json.loads(report_path.read_text(encoding="utf-8"))
    results = typ.cast("list[dict[str, object]]", document["results"])
    assert results, "the report has no results"
    assert results[0]["status"] == "interrupted"
