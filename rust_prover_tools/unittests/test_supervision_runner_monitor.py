"""Hold the runner's sampling, heartbeat, and progress reporting."""

from __future__ import annotations

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
    run_supervised,
)
from rust_prover_tools.unittests.supervision_doubles import (
    Script,
    ScriptedLauncher,
    ScriptedProbe,
    StepInterrupts,
    VirtualClock,
    spec,
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

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision import AttemptEvidence


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
