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
import contextlib
import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.attempt_state import (
    AttemptEvidence,
    latest,
    minimum,
    peak,
)
from rust_prover_tools.supervision.events import (
    heartbeat_record,
    output_record,
    sample_record,
)
from rust_prover_tools.supervision.models import (
    CgroupObservation,
    HarnessSpec,
    LaunchResult,
    OutputLine,
    ResourceSample,
    StopReason,
)
from rust_prover_tools.supervision.watchdog import (
    deadline_verdict,
    first_stop,
    watchdog_verdict,
)

if typ.TYPE_CHECKING:
    import datetime as dt

    from rust_prover_tools.supervision.config import SupervisionConfig
    from rust_prover_tools.supervision.launcher import (
        CommandBuilder,
        RunningHarness,
    )
    from rust_prover_tools.supervision.protocols import (
        CgroupReader,
        Clock,
        HarnessAdapter,
        HarnessLauncher,
        InterruptSource,
        LineObserver,
        ResourceProbe,
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
    _samples: list[ResourceSample] = dc.field(default_factory=list)
    _last_output: str | None = None
    _last_output_elapsed: float | None = None
    _stop_reason: StopReason | None = None
    _stop_detail: str = ""
    _next_sample: float = 0.0
    _next_heartbeat: float = 0.0
    _observer: LineObserver | None = None

    async def run(self) -> tuple[AttemptEvidence, HarnessClassification]:
        """Run the attempt, returning its evidence and classification."""
        started_at = self.clock.now()
        started_monotonic = self.clock.monotonic()
        self._next_sample = self.config.sample_interval_seconds
        self._next_heartbeat = self.config.heartbeat_interval_seconds
        self._observer = self.adapter.observer(self.spec)
        cgroup_start = self._cgroup_start()
        running = typ.cast(
            "RunningHarness",
            self.launcher.start(self.spec, self.build, self._on_line),
        )
        monitor = asyncio.get_running_loop().create_task(
            self._monitor(running, started_at, started_monotonic)
        )
        result = await running.wait()
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
        evidence = self._evidence(result, started_at, started_monotonic, cgroup_start)
        return evidence, self.adapter.classify(evidence, self._observer)

    @property
    def stop_detail(self) -> str:
        """The bounded explanation for the first stop, when one was requested."""
        return self._stop_detail

    def _cgroup_start(self) -> CgroupObservation:
        """Read the cgroup evidence once, before the child starts."""
        path = self.cgroup.discover()
        return CgroupObservation(
            path=path,
            start=self.cgroup.read(path),
            finish=None,
        )

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

    async def _monitor(
        self,
        running: RunningHarness,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> None:
        """Sample, heartbeat, and stop the child for the first reason."""
        done = asyncio.get_running_loop().create_task(running.wait())
        interrupt = (
            None
            if self.interrupts is None
            else asyncio.get_running_loop().create_task(self.interrupts.wait())
        )
        try:
            while not done.done():
                sleep = asyncio.get_running_loop().create_task(
                    self.sleeper.sleep(min(self._next_sample, self._next_heartbeat))
                )
                waiters = (
                    {done, sleep} if interrupt is None else {done, sleep, interrupt}
                )
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                sleep.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sleep
                if done.done():
                    return
                if interrupt is not None and interrupt.done():
                    self._record_interruption(running)
                    return
                if self._tick(running, started_at, started_monotonic):
                    return
        finally:
            done.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await done
            if interrupt is not None:
                interrupt.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await interrupt

    def _record_interruption(self, running: RunningHarness) -> None:
        """Record and deliver an interruption stop."""
        self._stop_reason = StopReason.INTERRUPTED
        self._stop_detail = "a signal interrupted the harness"
        running.stop(StopReason.INTERRUPTED)

    def _tick(
        self,
        running: RunningHarness,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> bool:
        """Take one sample, emit what is due, and apply the limits.

        Returns
        -------
        bool
            `True` when a stop was requested.
        """
        elapsed = max(0.0, self.clock.monotonic() - started_monotonic)
        sample = self.probe.sample(
            self.spec,
            group_id=running.group_id,
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
        if elapsed >= self._next_sample:
            self._next_sample += self.config.sample_interval_seconds
            self._samples.append(sample)
            self.diagnostics.emit(sample_record(run_id=self.run_id, sample=sample))
        if elapsed >= self._next_heartbeat:
            self._next_heartbeat += self.config.heartbeat_interval_seconds
            self.diagnostics.emit(
                heartbeat_record(
                    run_id=self.run_id,
                    sample=sample,
                    idle_seconds=self._idle_seconds(elapsed),
                    last_progress=self._last_output,
                )
            )
        verdict = first_stop(
            deadline_verdict(
                elapsed_seconds=elapsed,
                timeout_seconds=self.config.timeout_seconds,
            ),
            watchdog_verdict(
                host_available=sample.host_available,
                threshold_bytes=self.config.watchdog_threshold_bytes,
            ),
        )
        if verdict.reason is None:
            return False
        self._stop_reason = verdict.reason
        self._stop_detail = verdict.detail
        running.stop(verdict.reason)
        return True

    def _idle_seconds(self, elapsed: float) -> float:
        """Seconds since the last observed output line."""
        if self._last_output_elapsed is None:
            return 0.0
        return max(0.0, elapsed - self._last_output_elapsed)

    def _evidence(
        self,
        result: LaunchResult,
        started_at: dt.datetime,
        started_monotonic: float,
        cgroup_start: CgroupObservation,
    ) -> AttemptEvidence:
        """Assemble the attempt's termination and resource evidence."""
        finished_at = self.clock.now()
        duration = max(0.0, self.clock.monotonic() - started_monotonic)
        return AttemptEvidence(
            spec=self.spec,
            stop_reason=self._resolved_reason(result),
            exit_code=result.exit_code,
            signal_number=result.signal_number,
            launch_error=result.launch_error,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            samples=tuple(self._samples),
            peak_group_rss=peak([sample.group_rss for sample in self._samples]),
            min_host_available=minimum([
                sample.host_available for sample in self._samples
            ]),
            host_total=latest([sample.host_total for sample in self._samples]),
            cgroup=_finish_cgroup(self.cgroup, cgroup_start),
            last_output=self._last_output,
            stop_detail=self._stop_detail,
        )

    def _resolved_reason(self, result: LaunchResult) -> StopReason:
        """Name why the supervisor stopped waiting for the child."""
        if self._stop_reason is not None:
            return self._stop_reason
        if result.launch_error is not None:
            return StopReason.LAUNCH_FAILED
        return StopReason.EXITED


def _finish_cgroup(
    reader: CgroupReader,
    observation: CgroupObservation,
) -> CgroupObservation:
    """Read the finish-time cgroup values for an observation."""
    if observation.path is None:
        return observation
    return dc.replace(observation, finish=reader.read(observation.path))
