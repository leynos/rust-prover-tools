"""Drive one attempt's monitor loop: sampling, heartbeats, and limits.

The monitor owns every stop decision the supervisor makes on its own: the
deadline, the host-memory watchdog, and an installed interrupt source. It keeps
its own accumulated state so the attempt loop only has to join it and read the
outcome, which is what lets a cancelled or faulted run still report the samples
and the stop the monitor had already decided.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.attempt_state import idle_seconds
from rust_prover_tools.supervision.events import (
    heartbeat_record,
    sample_record,
)
from rust_prover_tools.supervision.models import (
    ResourceSample,
    StopReason,
)
from rust_prover_tools.supervision.watchdog import (
    deadline_verdict,
    first_stop,
    watchdog_verdict,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from rust_prover_tools.supervision.config import SupervisionConfig
    from rust_prover_tools.supervision.launch_running import RunningHarness
    from rust_prover_tools.supervision.models import HarnessSpec
    from rust_prover_tools.supervision.protocols import (
        Clock,
        InterruptSource,
        LineObserver,
        ResourceProbe,
        Sleeper,
    )
    from rust_prover_tools.supervision.sinks import Diagnostics

__all__ = ("AttemptMonitor",)


@dc.dataclass(slots=True)
class AttemptMonitor:
    """Watch one running child until it ends or a limit stops it.

    Attributes
    ----------
    spec : HarnessSpec
        Identity being supervised.
    config : SupervisionConfig
        Per-harness policy supplying the deadline and the watchdog floor.
    clock : Clock
        Monotonic source for elapsed time.
    probe : ResourceProbe
        Resource sampler.
    sleeper : Sleeper
        Pause between monitor ticks.
    diagnostics : Diagnostics
        Live evidence destinations.
    run_id : str
        Owning run identifier.
    interrupts : InterruptSource | None
        Signal source, when the caller installs one.
    observer : cabc.Callable[[], LineObserver | None]
        Returns the attempt's live observer, or `None` before one exists.
    last_output_elapsed : cabc.Callable[[], float | None]
        Returns when the last output line arrived, for idle accounting.
    samples : list[ResourceSample]
        Samples taken so far, in order.
    stop_reason : StopReason | None
        The stop this monitor decided, if any.
    stop_detail : str
        Bounded explanation of that stop.
    """

    spec: HarnessSpec
    config: SupervisionConfig
    clock: Clock
    probe: ResourceProbe
    sleeper: Sleeper
    diagnostics: Diagnostics
    run_id: str
    interrupts: InterruptSource | None
    observer: cabc.Callable[[], LineObserver | None]
    last_output_elapsed: cabc.Callable[[], float | None]
    samples: list[ResourceSample] = dc.field(default_factory=list)
    stop_reason: StopReason | None = None
    stop_detail: str = ""
    _next_sample: float = 0.0
    _next_heartbeat: float = 0.0

    async def run(
        self,
        running: RunningHarness,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> None:
        """Sample and heartbeat until the child ends or a limit stops it."""
        self._next_sample = self.config.sample_interval_seconds
        self._next_heartbeat = self.config.heartbeat_interval_seconds
        done = asyncio.get_running_loop().create_task(running.wait())
        interrupt = (
            None
            if self.interrupts is None
            else asyncio.get_running_loop().create_task(self.interrupts.wait())
        )
        try:
            while not done.done():
                elapsed = max(0.0, self.clock.monotonic() - started_monotonic)
                remaining = min(
                    self._next_sample - elapsed,
                    self._next_heartbeat - elapsed,
                    self.config.timeout_seconds - elapsed,
                )
                sleep = asyncio.get_running_loop().create_task(
                    self.sleeper.sleep(max(0.0, remaining))
                )
                waiters = (
                    {done, sleep} if interrupt is None else {done, sleep, interrupt}
                )
                try:
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                finally:
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
        self.stop_reason = StopReason.INTERRUPTED
        self.stop_detail = "a signal interrupted the harness"
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
        if running.done:
            return False
        elapsed = max(0.0, self.clock.monotonic() - started_monotonic)
        sample = self.probe.sample(
            self.spec,
            group_id=running.group_id,
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
        recorded = False
        if elapsed >= self._next_sample:
            self._next_sample += self.config.sample_interval_seconds
            recorded = True
        if elapsed >= self._next_heartbeat:
            self._next_heartbeat += self.config.heartbeat_interval_seconds
            observer = self.observer()
            self.diagnostics.emit(
                heartbeat_record(
                    run_id=self.run_id,
                    sample=sample,
                    idle_seconds=idle_seconds(self.last_output_elapsed(), elapsed),
                    last_progress=(
                        None if observer is None else observer.last_progress
                    ),
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
        self.samples.append(sample)
        if recorded:
            self.diagnostics.emit(sample_record(run_id=self.run_id, sample=sample))
        if verdict.reason is None:
            return False
        self.stop_reason = verdict.reason
        self.stop_detail = verdict.detail
        running.stop(verdict.reason)
        return True
