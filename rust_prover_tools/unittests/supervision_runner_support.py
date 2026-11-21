"""Shared fixtures for the supervised-runner tests.

The tests need one coroutine wrapper, one scripted environment, and one policy
shaped for virtual time. They live here so the core and evidence test modules
state only the behaviour they are about.
"""

from __future__ import annotations

import asyncio
import functools
import typing as typ

from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    HarnessClassification,
    HarnessSpec,
    LaunchCommand,
    MemorySink,
    ProofVerdict,
    RunEnvironment,
    SupervisionConfig,
)
from rust_prover_tools.unittests.supervision_doubles import (
    ScriptedCgroup,
    ScriptedLauncher,
    ScriptedProbe,
    StepInterrupts,
    VirtualClock,
    VirtualSleeper,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.supervision import AttemptEvidence

__all__ = ("async_test", "classify", "config", "environment")


def async_test(
    test: cabc.Callable[[], cabc.Coroutine[object, object, None]],
) -> cabc.Callable[[], None]:
    """Run one coroutine test on a fresh event loop.

    The project has no async pytest plugin, so the wrapper makes the test a
    synchronous callable without changing the body's ability to await.
    """

    @functools.wraps(test)
    def wrapper() -> None:
        """Run the coroutine test to completion."""
        asyncio.run(test())

    return wrapper


def classify(status: ExecutionStatus, detail: str = "") -> HarnessClassification:
    """Return a classification with the given status."""
    verdict = (
        ProofVerdict.PASSED
        if status is ExecutionStatus.PASSED
        else ProofVerdict.UNKNOWN
    )
    return HarnessClassification(status=status, proof_verdict=verdict, detail=detail)


# pylint: disable-next=too-many-arguments
def environment(
    launcher: ScriptedLauncher,
    clock: VirtualClock,
    *,
    probe: ScriptedProbe | None = None,
    cgroup: ScriptedCgroup | None = None,
    interrupts: StepInterrupts | None = None,
    statuses: cabc.Mapping[str, ExecutionStatus] | None = None,
    sinks: Diagnostics | None = None,
) -> RunEnvironment:
    """Build a run environment from doubles."""
    status_for = statuses or {}

    def classifier(evidence: AttemptEvidence) -> HarnessClassification:
        """Return the status the test configured for this identity."""
        status = status_for.get(evidence.spec.qualified, ExecutionStatus.PASSED)
        return classify(status, detail=evidence.stop_reason.value)

    def build(identity: HarnessSpec) -> LaunchCommand:
        """Compose a stand-in command that names the requested harness."""
        return LaunchCommand(("driver", "kani", "--harness", identity.harness))

    return RunEnvironment(
        clock=clock,
        launcher=launcher,
        probe=probe or ScriptedProbe(),
        cgroup=cgroup or ScriptedCgroup(),
        diagnostics=sinks or Diagnostics(jsonl=MemorySink()),
        build=build,
        classifier=classifier,
        sleeper=VirtualSleeper(clock),
        interrupts=interrupts,
    )


def config(
    *,
    timeout_seconds: float = 60.0,
    sample_interval_seconds: float = 5.0,
    heartbeat_interval_seconds: float = 15.0,
) -> SupervisionConfig:
    """Return a test policy that ticks quickly in virtual time."""
    return SupervisionConfig(
        timeout_seconds=timeout_seconds,
        sample_interval_seconds=sample_interval_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        watchdog_threshold_bytes=1_024 * 1024 * 1024,
    )
