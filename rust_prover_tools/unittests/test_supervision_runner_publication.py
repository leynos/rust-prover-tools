"""Hold evidence publication to the report that survives a sink failure."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import json
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    HarnessClassification,
    HarnessSpec,
    MemorySink,
    NullSink,
    ProofVerdict,
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
        observation: object,
    ) -> HarnessClassification:
        """Report the scripted observation alongside a passing status."""
        del observation
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
    environment = _environment(
        ScriptedLauncher(clock=clock),
        clock,
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


class _UnreadableSpool:
    """A spool that accepts writes but fails when the log is read back."""

    def write(self, text: str) -> None:
        """Accept one piece of the log; the failure is on read."""

    def lines(self) -> cabc.Iterator[str]:
        """Fail as a flush or read failure would."""
        msg = "the spool could not be read"
        raise OSError(msg)

    def close(self) -> None:
        """Release nothing."""


def test_an_unreadable_spool_is_classified_as_an_evidence_error(
    tmp_path: Path,
) -> None:
    """A spool read failure is reported, not raised, on ordinary completion."""
    asyncio.run(_unreadable_spool_scenario(tmp_path, cancel=False))


def test_a_cancelled_attempt_survives_an_unreadable_spool(tmp_path: Path) -> None:
    """Cancellation is preserved when finishing the evidence also fails."""
    asyncio.run(_unreadable_spool_scenario(tmp_path, cancel=True))


async def _unreadable_spool_scenario(tmp_path: Path, *, cancel: bool) -> None:
    """Run one attempt whose spool cannot be read back."""
    from rust_prover_tools.supervision.kani import KaniAdapter

    clock = VirtualClock()
    ready = asyncio.Event()
    launcher = ScriptedLauncher(
        script=Script(
            lines=(("stdout", "Checking harness harnesses::a..."),),
            block=cancel,
        ),
        ready=ready,
    )
    (identity,) = (spec("harnesses::a"),)
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
        adapter=KaniAdapter(
            version="0.67.0",
            spool_factory=_UnreadableSpool,
        ),
    )

    if not cancel:
        report = await run_supervised((identity,), config, environment)
        (result,) = report.results
        assert result.status is ExecutionStatus.ERROR
        assert "cannot retain the verifier log" in result.detail
        assert "the spool could not be read" in result.detail
        return

    task = asyncio.create_task(run_supervised((identity,), config, environment))
    await asyncio.wait_for(ready.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    finish = next(
        record for record in jsonl.records if record["record"] == "harness-finish"
    )
    assert finish["status"] == "interrupted"


def test_a_monitor_fault_survives_an_unreadable_spool(tmp_path: Path) -> None:
    """A monitor failure stays primary when finishing the evidence also fails."""
    asyncio.run(_monitor_fault_with_unreadable_spool_scenario(tmp_path))


async def _monitor_fault_with_unreadable_spool_scenario(tmp_path: Path) -> None:
    """Fail the probe while the spool cannot be read back."""
    from rust_prover_tools.supervision.kani import KaniAdapter

    class _FailingProbe(ScriptedProbe):
        """A probe that fails on every sample."""

        def sample(self, spec: HarnessSpec, **kwargs: object) -> ResourceSample:
            """Raise the fault the monitor must survive."""
            del spec, kwargs
            msg = "probe failed"
            raise RuntimeError(msg)

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
        adapter=KaniAdapter(version="0.67.0", spool_factory=_UnreadableSpool),
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
