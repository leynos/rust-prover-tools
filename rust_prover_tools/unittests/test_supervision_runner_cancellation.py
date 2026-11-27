"""Hold cancellation and settlement across waiters, monitor faults, and joins."""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    HarnessSpec,
    LaunchResult,
    MemorySink,
    RunningHarness,
    StopReason,
    run_supervised,
)
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
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.supervision import (
        LaunchCommand,
    )


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
    try:
        await asyncio.wait_for(teardown_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not run.done(), "the run returned before its child finished teardown"
    finally:
        # A failed wait or assertion must still let the child finish teardown.
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
async def test_an_earlier_cancellation_does_not_interrupt_a_later_attempt() -> None:
    """A cancellation count inherited from earlier work is not this attempt's."""
    from rust_prover_tools.unittests.supervision_doubles import (
        ScriptedCancellations,
    )

    # The probe reports an inherited count that never increases: the attempt
    # must treat that as no cancellation of its own.
    environment = _environment(
        ScriptedLauncher(),
        VirtualClock(),
        cancellations=ScriptedCancellations(counts=[1]),
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert report.is_complete


@_async_test
async def test_a_cancellation_during_settlement_is_re_raised() -> None:
    """A count that grows during the attempt re-raises even without a join."""
    from rust_prover_tools.unittests.supervision_doubles import (
        ScriptedCancellations,
    )

    # Baseline 0, then a positive count once the attempt reaches its check.
    environment = _environment(
        ScriptedLauncher(),
        VirtualClock(),
        cancellations=ScriptedCancellations(counts=[0, 1]),
    )

    with pytest.raises(asyncio.CancelledError):
        await run_supervised((spec(),), _config(), environment)


@_async_test
async def test_a_cancelled_waiter_does_not_hide_a_monitor_failure() -> None:
    """A waiter's cancellation is recorded but a saved monitor fault wins."""
    from unittest import mock

    from rust_prover_tools.supervision import attempt as attempt_module

    async def cancel_wait(*args: object) -> LaunchResult:
        """Raise as the waiter's own cancellation would."""
        del args
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    async def fail_monitor(*args: object) -> None:
        """Fail as the monitor does before its waiter is joined."""
        del args
        await asyncio.sleep(0)
        msg = "the monitor failed"
        raise RuntimeError(msg)

    environment = _environment(
        ScriptedLauncher(script=Script(block=True)), VirtualClock()
    )

    with (
        mock.patch.object(RunningHarness, "wait", cancel_wait),
        mock.patch.object(attempt_module.AttemptSupervisor, "_monitor", fail_monitor),
        pytest.raises(RuntimeError, match="the monitor failed"),
    ):
        await run_supervised((spec(),), _config(timeout_seconds=60.0), environment)


@_async_test
async def test_a_cancellation_during_the_monitor_join_is_re_raised() -> None:
    """A cancel swallowed while joining the monitor still stops the run."""
    from unittest import mock

    from rust_prover_tools.supervision import attempt as attempt_module

    sleeping = asyncio.Event()
    joining = asyncio.Event()
    release = asyncio.Event()

    class _StubbornSleeper:
        """A sleeper that holds its own cancellation until released."""

        async def sleep(self, seconds: float) -> None:
            """Enter the tick and wait to be released."""
            del seconds
            sleeping.set()
            try:
                await asyncio.wait_for(release.wait(), timeout=60)
            except asyncio.CancelledError:
                await release.wait()

    clock = VirtualClock()
    jsonl = MemorySink()
    child_done = asyncio.Event()
    environment = _environment(
        ScriptedLauncher(script=Script(), completion_gate=child_done),
        clock,
        sinks=Diagnostics(jsonl=jsonl),
        sleeper=_StubbornSleeper(),
        statuses={spec().qualified: ExecutionStatus.INTERRUPTED},
    )
    real_join = attempt_module.join_reporting_cancellation

    async def recording_join(task: asyncio.Task[object]) -> bool:
        """Report reaching the supervisor's join, then join for real."""
        joining.set()
        return await real_join(task)

    with mock.patch.object(
        attempt_module, "join_reporting_cancellation", recording_join
    ):
        task = asyncio.create_task(
            run_supervised((spec(),), _config(timeout_seconds=600.0), environment)
        )
        try:
            # The monitor must be sleeping before the child settles, so the
            # supervisor's own join is what holds the run open.
            await asyncio.wait_for(sleeping.wait(), timeout=1)
            child_done.set()
            await asyncio.wait_for(joining.wait(), timeout=1)
            assert not task.done(), "the run returned before its join completed"
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "the run finished while cleanup was still held"
        finally:
            # A failed assertion or setup error must not leave the sleeper
            # blocked, or the loop shutdown would wait on it.
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
