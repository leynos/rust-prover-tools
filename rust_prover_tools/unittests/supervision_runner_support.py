"""Shared fixtures for the supervised-runner tests.

The tests need one coroutine wrapper, one scripted environment, and one policy
shaped for virtual time. They live here so the core and evidence test modules
state only the behaviour they are about.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import functools
import typing as typ

from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    HarnessClassification,
    HarnessSpec,
    LaunchCommand,
    MemorySink,
    OutputLine,
    ProofVerdict,
    RunEnvironment,
    SupervisionConfig,
    new_run_id,
)
from rust_prover_tools.unittests.supervision_doubles import (
    ScriptedCgroup,
    ScriptedLauncher,
    ScriptedProbe,
    VirtualClock,
    VirtualSleeper,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.supervision import (
        AttemptEvidence,
        LineObserver,
    )
    from rust_prover_tools.supervision.protocols import (
        CgroupReader,
        HarnessAdapter,
        InterruptSource,
        ResourceProbe,
        Sleeper,
    )

__all__ = ("NullObserver", "async_test", "classify", "config", "environment")


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


class NullObserver:
    """An observer that records nothing, for tests that do not parse output."""

    last_progress: str | None = None

    def observe(self, line: OutputLine) -> None:
        """Discard one line."""

    def finish(self) -> None:
        """Return a trivial observation; the scripted adapter ignores it."""

    def close(self) -> None:
        """Release nothing; the null observer owns no resource."""


@dc.dataclass(frozen=True, slots=True)
class _ScriptedAdapter:
    """A prover adapter whose status is fixed by identity."""

    statuses: cabc.Mapping[str, ExecutionStatus] = dc.field(default_factory=dict)

    def observer(self, spec: HarnessSpec) -> LineObserver:
        """Return the null observer for this attempt."""
        del spec
        return NullObserver()

    def classify(
        self,
        evidence: AttemptEvidence,
        observation: object,
    ) -> HarnessClassification:
        """Return the status configured for this identity."""
        del observation
        status = self.statuses.get(evidence.spec.qualified, ExecutionStatus.PASSED)
        return classify(status, detail=evidence.stop_reason.value)


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
    probe: ResourceProbe | None = None,
    cgroup: CgroupReader | None = None,
    interrupts: InterruptSource | None = None,
    statuses: cabc.Mapping[str, ExecutionStatus] | None = None,
    sinks: Diagnostics | None = None,
    sleeper: Sleeper | None = None,
    adapter: HarnessAdapter | None = None,
    run_id: cabc.Callable[[], str] = new_run_id,
) -> RunEnvironment:
    """Build a run environment from doubles."""

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
        adapter=adapter or _ScriptedAdapter(statuses or {}),
        sleeper=sleeper or VirtualSleeper(clock),
        interrupts=interrupts,
        run_id=run_id,
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
