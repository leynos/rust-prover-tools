"""Supervise one harness attempt: stream, sample, watch, and stop.

One attempt is a launcher task and one monitor loop. The monitor owns every
decision, which is what keeps the first stop reason first: it samples on its
own interval, heartbeats on its own interval, checks the deadline and the host
memory floor, and stops the child for the first reason that applies. Later
ticks see that a stop is already recorded and do nothing.

Output is observed, not captured. Each line is timestamped, bounded, forwarded
to the evidence sinks, kept as the attempt's last output, and offered to the
adapter's progress reader. Nothing retains the log.

The attempt never decides what its result *means*; it returns the termination
and resource evidence, and the adapter classifies it.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.attempt_cgroup import (
    finish_cgroup,
    start_cgroup,
)
from rust_prover_tools.supervision.attempt_evidence import (
    AttemptEvidence,
    AttemptSettlement,
)
from rust_prover_tools.supervision.attempt_monitor import AttemptMonitor
from rust_prover_tools.supervision.attempt_state import (
    cancellation_pending,
    join_reporting_cancellation,
    resolved_reason,
    waiter_failed,
)
from rust_prover_tools.supervision.clock import TaskCancellations
from rust_prover_tools.supervision.events import output_record
from rust_prover_tools.supervision.models import (
    CgroupObservation,
    HarnessSpec,
    LaunchResult,
    OutputLine,
    ResourceSample,
    StopReason,
)

if typ.TYPE_CHECKING:
    import datetime as dt

    from rust_prover_tools.supervision.config import SupervisionConfig
    from rust_prover_tools.supervision.launcher import (
        CommandBuilder,
        RunningHarness,
    )
    from rust_prover_tools.supervision.protocols import (
        CancellationProbe,
        CgroupReader,
        Clock,
        HarnessAdapter,
        HarnessLauncher,
        InterruptSource,
        LineObserver,
        ResourceProbe,
        RunningHarnessHandle,
        Sleeper,
    )
    from rust_prover_tools.supervision.results import HarnessClassification
    from rust_prover_tools.supervision.sinks import Diagnostics

__all__ = ("AttemptSupervisor",)


@dc.dataclass(slots=True)
class AttemptSupervisor:
    """Run and watch one harness invocation.

    Attributes
    ----------
    spec : HarnessSpec
        Identity to run.
    config : SupervisionConfig
        Per-harness policy.
    clock : Clock
        Time source.
    launcher : HarnessLauncher
        Process boundary. Any object with ``start`` returning a
        `RunningHarness` shape works, including the production Cuprum launcher
        and a scripted double.
    probe : ResourceProbe
        Resource sampler.
    cgroup : CgroupReader
        Shared cgroup evidence reader.
    diagnostics : Diagnostics
        Live evidence destinations.
    run_id : str
        Owning run identifier.
    build : CommandBuilder
        Composes the argv and environment for the identity.
    adapter : HarnessAdapter
        Prover adapter supplying the observer and the classifier.
    sleeper : Sleeper
        Pause between monitor ticks.
    interrupts : InterruptSource | None
        Signal source, when the caller installs one.
    cancellations : CancellationProbe
        Reader for the running task's cancellation count.
    """

    spec: HarnessSpec
    config: SupervisionConfig
    clock: Clock
    launcher: HarnessLauncher
    probe: ResourceProbe
    cgroup: CgroupReader
    diagnostics: Diagnostics
    run_id: str
    build: CommandBuilder
    adapter: HarnessAdapter
    sleeper: Sleeper
    interrupts: InterruptSource | None = None
    cancellations: CancellationProbe = dc.field(
        default_factory=lambda: typ.cast("CancellationProbe", TaskCancellations())
    )
    _samples: list[ResourceSample] = dc.field(default_factory=list)
    _last_output: str | None = None
    _last_output_elapsed: float | None = None
    _stop_reason: StopReason | None = None
    _stop_detail: str = ""
    _observer: LineObserver | None = None
    _interrupted: tuple[AttemptEvidence, HarnessClassification] | None = None
    _settled: LaunchResult | None = None

    async def run(self) -> tuple[AttemptEvidence, HarnessClassification]:
        """Run the attempt, returning its evidence and classification.

        The attempt owns the child until it has settled. A monitor failure, a
        stop, or a cancellation of this coroutine all leave through the same
        cleanup: stop the child when nothing else has, then join the owned
        process group before reporting or re-raising. The observer is released
        at this boundary, whatever ended the attempt.
        """
        started_at = self.clock.now()
        started_monotonic = self.clock.monotonic()
        self._observer = self.adapter.observer(self.spec)
        failure: BaseException | None = None
        try:
            cgroup_start = start_cgroup(self.cgroup)
            running = typ.cast(
                "RunningHarness",
                self.launcher.start(self.spec, self.build, self._on_line),
            )
            try:
                result = await self._drive(running, started_at, started_monotonic)
            except BaseException as error:
                # The child has already been stopped and joined by `_drive`.
                # Keep the evidence it produced so a cancelled or faulted run
                # can report an interrupted attempt instead of pretending it
                # never started.
                evidence = self._evidence(
                    self._stopped_result, started_at, started_monotonic, cgroup_start
                )
                self._interrupted = (evidence, self._classify(evidence))
                failure = error
                raise
            evidence = self._evidence(
                result, started_at, started_monotonic, cgroup_start
            )
            return evidence, self._classify(evidence)
        finally:
            # `classify` no longer owns the spool, so the boundary that opened
            # the observer releases it. `failure` is the exception escaping
            # `run`, if any, so cleanup never has to infer it.
            self._release_observer(failure)

    def _classify(self, evidence: AttemptEvidence) -> HarnessClassification:
        """Finish the observer and classify the attempt from its observation."""
        observation = typ.cast("LineObserver[typ.Any]", self._observer).finish()
        return self.adapter.classify(evidence, observation)

    def _release_observer(self, failure: BaseException | None) -> None:
        """Close the observer, preserving any failure already unwinding.

        A close failure while a cancellation or monitor fault is propagating
        is attached to `failure` as a note rather than raised, so the original
        failure remains the one the caller sees. On an otherwise successful
        attempt the same failure propagates: losing the evidence file is a
        real error, not a benign cleanup detail.
        """
        if self._observer is None:
            return
        try:
            self._observer.close()
        except Exception as error:
            if failure is None:
                raise
            failure.add_note(f"closing the verifier log spool also failed: {error}")

    @property
    def interrupted(self) -> tuple[AttemptEvidence, HarnessClassification] | None:
        """The evidence of an attempt a cancellation stopped, when one exists.

        Returns
        -------
        tuple[AttemptEvidence, HarnessClassification] | None
            The stopped attempt's evidence and its classification, or `None`
            when the attempt did not end in cancellation.
        """
        return self._interrupted

    @property
    def _stopped_result(self) -> LaunchResult:
        """The result the stopped child settled with."""
        return self._settled or LaunchResult()

    async def _drive(
        self,
        running: RunningHarnessHandle,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> LaunchResult:
        """Drive the monitor and join the child before returning or raising."""
        loop = asyncio.get_running_loop()
        waiter = loop.create_task(running.wait())
        monitor = loop.create_task(
            self._monitor(running, started_at, started_monotonic)
        )
        failure: BaseException | None = None
        result: LaunchResult | None = None
        cancelled = False
        baseline = self.cancellations.cancelling()
        try:
            await asyncio.wait({waiter, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor.done():
                # The monitor owns every stop decision, so once it has failed
                # nothing else will stop the child. Stop it here, then join it
                # below before the failure is surfaced.
                failure = monitor.exception()
                if failure is not None:
                    self._request_stop(running, "the supervisor monitor failed")
            result = await waiter
        finally:
            monitor.cancel()
            # Await the retained failure without re-raising it here: it is
            # re-raised after settlement so the child's exit evidence and
            # cleanup are not lost to the monitor's exception. A cancellation
            # arriving at either join is recorded and re-raised below, never
            # cleared, so the run still reports an interrupted attempt.
            cancelled = await join_reporting_cancellation(monitor) or cancelled
            # A launcher that raised stopped the child from inside because
            # its callback failed; record that as the supervisor's own stop.
            if waiter_failed(waiter):
                self._request_stop(running, "the supervisor stopped the run")
            if not running.done and running.stop_reason is None:
                self._request_stop(running, "the supervisor stopped the run")
            try:
                joined = await running.settle()
            except BaseException:
                self._settled = running.settled_status
                raise
            self._settled = joined
            waiter.cancel()
            cancelled = await join_reporting_cancellation(waiter) or cancelled
        if failure is not None:
            raise failure
        pending = cancellation_pending(
            recorded=cancelled,
            baseline=baseline,
            current_count=self.cancellations.cancelling(),
        )
        if pending:
            # The caller stopped the attempt, so it is interrupted even when
            # the child happened to settle first.
            self._request_stop(running, "the supervisor stopped the run")
            raise asyncio.CancelledError
        return joined if result is None else result

    def _on_line(self, line: object) -> None:
        """Forward one decoded line to the sinks and remember it."""
        typed = typ.cast("OutputLine", line)
        self._last_output = typed.text
        self._last_output_elapsed = typed.elapsed_seconds
        if self._observer is not None:
            self._observer.observe(typed)
        self.diagnostics.emit(
            output_record(run_id=self.run_id, spec=self.spec, line=typed)
        )

    def _request_stop(self, running: RunningHarnessHandle, detail: str) -> None:
        """Record the first stop reason and request it from the launcher.

        A stop the supervisor initiates itself, because its monitor failed or
        its caller cancelled it, is still an interruption of the child. Setting
        the reason here keeps the reported result `interrupted` instead of
        letting the launcher's cancellation look like an ordinary exit.
        """
        if self._stop_reason is None:
            self._stop_reason = StopReason.INTERRUPTED
            self._stop_detail = detail
        running.stop(StopReason.INTERRUPTED)

    async def _monitor(
        self,
        running: RunningHarnessHandle,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> None:
        """Run the attempt monitor until the child ends or a limit stops it."""
        monitor = self._monitor_for()
        try:
            await monitor.run(running, started_at, started_monotonic)
        finally:
            # Samples and a decided stop are evidence even when the monitor
            # was cancelled or raised, so they are copied whatever ended it.
            self._samples = monitor.samples
            if monitor.stop_reason is not None and self._stop_reason is None:
                self._stop_reason = monitor.stop_reason
                self._stop_detail = monitor.stop_detail

    def _monitor_for(self) -> AttemptMonitor:
        """Build the monitor for this attempt."""
        return AttemptMonitor(
            spec=self.spec,
            config=self.config,
            clock=self.clock,
            probe=self.probe,
            sleeper=self.sleeper,
            diagnostics=self.diagnostics,
            run_id=self.run_id,
            interrupts=self.interrupts,
            observer=lambda: self._observer,
            last_output_elapsed=lambda: self._last_output_elapsed,
        )

    def _evidence(
        self,
        result: LaunchResult,
        started_at: dt.datetime,
        started_monotonic: float,
        cgroup_start: CgroupObservation,
    ) -> AttemptEvidence:
        """Assemble the attempt's termination and resource evidence."""
        return AttemptSettlement(
            spec=self.spec,
            stop_reason=resolved_reason(self._stop_reason, result),
            result=result,
            started_at=started_at,
            finished_at=self.clock.now(),
            duration=max(0.0, self.clock.monotonic() - started_monotonic),
            samples=tuple(self._samples),
            cgroup=finish_cgroup(self.cgroup, cgroup_start),
            last_output=self._last_output,
            stop_detail=self._stop_detail,
        ).to_evidence()
