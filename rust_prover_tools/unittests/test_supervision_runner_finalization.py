"""Hold cancellation and fault reporting to the failure that stays primary."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import json
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    Diagnostics,
    HarnessSpec,
    MemorySink,
    ResourceSample,
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
    _UnclosableAdapter,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.supervision import (
        RunReport,
    )


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

    environment = _environment(
        ScriptedLauncher(script=Script(block=True), ready=ready),
        VirtualClock(),
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

    environment = _environment(
        ScriptedLauncher(script=Script(block=True)),
        VirtualClock(),
        probe=_ExplodingProbe(),
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


def test_a_cancellation_survives_a_failing_close(tmp_path: Path) -> None:
    """A cleanup failure must not replace the cancellation that is unwinding."""
    asyncio.run(_cancellation_with_failing_close_scenario(tmp_path))


async def _cancellation_with_failing_close_scenario(tmp_path: Path) -> None:
    """Cancel an attempt whose observer cannot be closed."""
    clock = VirtualClock()
    ready = asyncio.Event()
    launcher = ScriptedLauncher(
        script=Script(
            lines=(("stdout", "Checking harness harnesses::a..."),),
            block=True,
        ),
        ready=ready,
    )
    (identity,) = (spec("harnesses::a"),)
    config = dc.replace(_config(timeout_seconds=600.0), sinks=DiagnosticSinks())
    environment = _environment(
        launcher,
        clock,
        sinks=Diagnostics(jsonl=MemorySink()),
        adapter=_UnclosableAdapter(),
    )

    task = asyncio.create_task(run_supervised((identity,), config, environment))
    await asyncio.wait_for(ready.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
