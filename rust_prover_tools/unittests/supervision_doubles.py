"""Deterministic doubles for supervised-execution tests.

Every test of the supervision layer needs the same five controls: time that
advances only when asked, a launcher whose child does exactly what the test
says, a resource probe with scripted values, a cgroup reader with scripted
files, and an interrupt source the test can trigger. They live here so each
test states only the behaviour it is about.

Two rules keep the doubles honest:

- The virtual clock advances only through `advance`, and the sleeper advances
  it, so a monitor loop makes progress without real time passing.
- The scripted launcher reports exactly the exit status and output a test
  scripts, and records the stop it received, so a test can assert that a
  timeout or watchdog stop was actually requested rather than inferred.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import datetime as dt
import typing as typ

from rust_prover_tools.supervision.launcher import (
    LaunchCommand,
    PidSource,
    RunningHarness,
)
from rust_prover_tools.supervision.models import (
    CgroupMemory,
    HarnessSpec,
    LaunchResult,
    Measurement,
    OutputLine,
    ResourceSample,
    StopReason,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "Script",
    "ScriptedCgroup",
    "ScriptedLauncher",
    "ScriptedProbe",
    "StepInterrupts",
    "VirtualClock",
    "VirtualSleeper",
    "spec",
)

_EPOCH = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.UTC)


class VirtualClock:
    """A clock that advances only when told to."""

    def __init__(self, start: dt.datetime = _EPOCH) -> None:
        """Start at `start` with a monotonic count of zero."""
        self._now = start
        self._monotonic = 0.0

    def now(self) -> dt.datetime:
        """Return the current virtual UTC time."""
        return self._now

    def monotonic(self) -> float:
        """Return the current virtual monotonic count."""
        return self._monotonic

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by `seconds`."""
        self._monotonic += seconds
        self._now += dt.timedelta(seconds=seconds)


class VirtualSleeper:
    """A sleeper that advances a virtual clock instead of waiting."""

    def __init__(self, clock: VirtualClock) -> None:
        """Create a sleeper driving `clock`."""
        self._clock = clock
        self.calls: list[float] = []

    async def sleep(self, seconds: float) -> None:
        """Advance the virtual clock and yield to the loop."""
        self.calls.append(seconds)
        self._clock.advance(seconds)
        await asyncio.sleep(0)


class StepInterrupts:
    """An interrupt source a test fires explicitly."""

    def __init__(self) -> None:
        """Create a source that has received no signal."""
        self._signum: int | None = None
        self._fired = asyncio.Event()

    @property
    def signum(self) -> int | None:
        """The interrupting signal, or `None` while the run may continue."""
        return self._signum

    @staticmethod
    def bind(loop: asyncio.AbstractEventLoop) -> None:
        """Accept the run's loop; the double needs no scheduling."""
        del loop

    async def wait(self) -> None:
        """Wait until the test fires a signal."""
        await self._fired.wait()

    def fire(self, signum: int) -> None:
        """Record a signal and release every waiter."""
        if self._signum is None:
            self._signum = signum
        self._fired.set()


@dc.dataclass(frozen=True, slots=True)
class Script:
    """What one scripted child does.

    Attributes
    ----------
    lines : tuple[tuple[str, str], ...]
        ``(stream, text)`` pairs the child emits.
    exit_code : int
        Child status when it settles.
    launch_error : str | None
        Launch failure description, which suppresses the exit status.
    block : bool
        Whether the child waits to be stopped instead of settling.
    """

    lines: tuple[tuple[str, str], ...] = ()
    exit_code: int = 0
    launch_error: str | None = None
    block: bool = False


class _ScriptedRunningHarness(RunningHarness):
    """A running handle that records the stops it receives."""

    def __init__(
        self,
        task: asyncio.Task[LaunchResult],
        recorder: PidSource,
        stops: list[StopReason],
    ) -> None:
        """Wrap the scripted run task and its stop recorder."""
        super().__init__(task, recorder)
        self._stops = stops

    def stop(self, reason: StopReason) -> None:
        """Record the stop before delivering it."""
        self._stops.append(reason)
        super().stop(reason)


@dc.dataclass(slots=True)
class ScriptedLauncher:
    """A launcher whose child does exactly what a test scripts.

    Attributes
    ----------
    script : Script
        Default behaviour for every identity.
    per_harness : dict[str, Script]
        Behaviour keyed by qualified identity, overriding the default.
    stops : list[StopReason]
        Every stop the supervisor requested, in order.
    commands : list[LaunchCommand]
        Every command the builder produced.
    clock : VirtualClock | None
        When supplied, the child advances it as it emits output so a monitor
        tick sees plausible elapsed time.
    seconds_per_line : float
        Virtual seconds consumed by each emitted line.
    ready : asyncio.Event | None
        Set as soon as a child is launched, for tests that need readiness.
    """

    script: Script = dc.field(default_factory=Script)
    per_harness: dict[str, Script] = dc.field(default_factory=dict)
    stops: list[StopReason] = dc.field(default_factory=list)
    commands: list[LaunchCommand] = dc.field(default_factory=list)
    clock: VirtualClock | None = None
    seconds_per_line: float = 0.0
    ready: asyncio.Event | None = None
    settled_exit_code: int | None = None

    def start(
        self,
        spec: HarnessSpec,
        build: cabc.Callable[[HarnessSpec], LaunchCommand],
        on_line: cabc.Callable[[object], None],
    ) -> RunningHarness:
        """Start the scripted child and return its handle."""
        self.commands.append(build(spec))
        script = self.per_harness.get(spec.qualified, self.script)
        if self.ready is not None:
            self.ready.set()
        recorder = _PidRecorder(4321, settled_exit_code=self.settled_exit_code)
        task = asyncio.get_running_loop().create_task(
            self._run(script, on_line, recorder)
        )
        return _ScriptedRunningHarness(task, recorder, self.stops)

    async def _run(
        self,
        script: Script,
        on_line: cabc.Callable[[object], None],
        recorder: _PidRecorder,
    ) -> LaunchResult:
        """Emit the scripted output and settle, or wait to be stopped."""
        started = self.clock.monotonic() if self.clock is not None else 0.0
        try:
            for index, (stream, text) in enumerate(script.lines):
                if self.clock is not None:
                    self.clock.advance(self.seconds_per_line)
                on_line(
                    OutputLine(
                        stream=stream,
                        text=text,
                        timestamp_utc=(
                            self.clock.now() if self.clock is not None else _EPOCH
                        ),
                        elapsed_seconds=(
                            self.clock.monotonic() - started
                            if self.clock is not None
                            else float(index)
                        ),
                    )
                )
        except Exception as error:
            # A real launcher records the relayed callback failure and stops
            # the child from inside, so settle reports the failure first.
            recorder.failure = error
            raise
        if script.block:
            await asyncio.Event().wait()
        if script.launch_error is not None:
            return LaunchResult(launch_error=script.launch_error)
        return LaunchResult(exit_code=script.exit_code)


class _PidRecorder:
    """Report a fixed process-group ID, as a real run would."""

    def __init__(
        self,
        pid: int | None,
        *,
        settled_exit_code: int | None = None,
    ) -> None:
        """Record the pid and settled status to report."""
        self.pid = pid
        self.settled_exit_code = settled_exit_code
        self.failure: BaseException | None = None


@dc.dataclass(slots=True)
class ScriptedProbe:
    """A resource probe returning scripted values in order.

    Attributes
    ----------
    rss_values : tuple[Measurement, ...]
        Process-group RSS per sample; the last value repeats.
    available_values : tuple[Measurement, ...]
        Host ``MemAvailable`` per sample; the last value repeats.
    total : Measurement
        Host ``MemTotal`` for every sample.
    samples : list[ResourceSample]
        Samples this probe produced.
    """

    rss_values: tuple[Measurement, ...] = dc.field(
        default_factory=lambda: (Measurement.available(100 * 1024 * 1024),)
    )
    available_values: tuple[Measurement, ...] = dc.field(
        default_factory=lambda: (Measurement.available(8 * 1024 * 1024 * 1024),)
    )
    total: Measurement = dc.field(
        default_factory=lambda: Measurement.available(16 * 1024 * 1024 * 1024)
    )
    samples: list[ResourceSample] = dc.field(default_factory=list)

    def sample(
        self,
        spec: HarnessSpec,
        *,
        group_id: int | None,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> ResourceSample:
        """Return the next scripted sample."""
        index = len(self.samples)
        sample = ResourceSample(
            timestamp_utc=started_at + dt.timedelta(seconds=index + 1),
            elapsed_seconds=float(index + 1),
            package=spec.package,
            harness=spec.harness,
            attempt=spec.attempt,
            group_rss=_at(self.rss_values, index),
            host_available=_at(self.available_values, index),
            host_total=self.total,
        )
        self.samples.append(sample)
        return sample


def _at(values: tuple[Measurement, ...], index: int) -> Measurement:
    """Return the value at `index`, repeating the last."""
    return values[min(index, len(values) - 1)]


@dc.dataclass(slots=True)
class ScriptedCgroup:
    """A cgroup reader with scripted values.

    Attributes
    ----------
    path : str | None
        Path to report, or `None` when the host has no cgroup v2 evidence.
    reads : list[CgroupMemory]
        Values returned per read; the last repeats.
    read_count : int
        How many reads have been served.
    """

    path: str | None = "/user.slice/job.scope"
    reads: list[CgroupMemory] = dc.field(
        default_factory=lambda: [
            CgroupMemory(
                current=Measurement.available(64 * 1024 * 1024),
                peak=Measurement.available(96 * 1024 * 1024),
                oom_kill=Measurement.available(0),
            ),
            CgroupMemory(
                current=Measurement.available(32 * 1024 * 1024),
                peak=Measurement.available(128 * 1024 * 1024),
                oom_kill=Measurement.available(1),
            ),
        ]
    )
    read_count: int = 0

    def discover(self) -> str | None:
        """Return the scripted cgroup path."""
        return self.path

    def read(self, path: str | None) -> CgroupMemory:
        """Return the next scripted memory values."""
        del path
        index = min(self.read_count, len(self.reads) - 1)
        self.read_count += 1
        return self.reads[index]


def spec(
    harness: str = "harnesses::verified",
    package: str = "example",
) -> HarnessSpec:
    """Return one harness identity for a test.

    Parameters
    ----------
    harness : str
        Pretty name.
    package : str
        Package that owns it.

    Returns
    -------
    HarnessSpec
        The identity.
    """
    return HarnessSpec(package=package, harness=harness)


def unwritten(path: Path) -> bool:
    """Whether a path does not exist or is empty."""
    return not path.exists() or path.stat().st_size == 0
