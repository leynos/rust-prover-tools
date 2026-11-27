"""The injected dependencies of one supervised run.

A run needs a clock, a launcher, a resource probe, a cgroup reader, evidence
sinks, a command builder, an adapter classifier, an optional interrupt source,
and a run-ID source. They are grouped by the concern that reads them: the
execution value is the process boundary and its observations, the evidence
value is where results are published, and the policy value is the run's
identity and stop account. Splitting them keeps an evidence sink out of the
execution value and a signal source out of the evidence value, so a caller
replaces only the group that owns the seam it needs.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.clock import TaskCancellations
from rust_prover_tools.supervision.report_writer import write_report
from rust_prover_tools.supervision.run_orchestrator import new_run_id

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.supervision.launcher import CommandBuilder
    from rust_prover_tools.supervision.protocols import (
        CancellationProbe,
        CgroupReader,
        Clock,
        HarnessAdapter,
        HarnessLauncher,
        InterruptSource,
        ReportWriter,
        ResourceProbe,
        Sleeper,
    )
    from rust_prover_tools.supervision.sinks import Diagnostics

__all__ = (
    "EvidenceEnvironment",
    "ExecutionEnvironment",
    "RunEnvironment",
    "RunPolicyEnvironment",
)


@dc.dataclass(frozen=True, slots=True)
class ExecutionEnvironment:
    """The process boundary and the observations it produces.

    Attributes
    ----------
    launcher : HarnessLauncher
        Process boundary.
    probe : ResourceProbe
        Resource sampler.
    cgroup : CgroupReader
        Shared cgroup evidence reader.
    sleeper : Sleeper
        Pause between monitor ticks.
    adapter : HarnessAdapter
        Prover adapter supplying the observer and the classifier.
    build : CommandBuilder
        Composes the argv and environment for one identity.
    clock : Clock
        Wall-clock and monotonic sources. Supervision times each harness with
        it, and the run journal stamps the records from the same source, so a
        scripted clock times the harness and its evidence alike.
    """

    launcher: HarnessLauncher
    probe: ResourceProbe
    cgroup: CgroupReader
    sleeper: Sleeper
    adapter: HarnessAdapter
    build: CommandBuilder
    clock: Clock


@dc.dataclass(frozen=True, slots=True)
class EvidenceEnvironment:
    """The destinations one run publishes its evidence to.

    Attributes
    ----------
    diagnostics : Diagnostics
        Live evidence destinations.
    report_writer : ReportWriter
        Publishes the final report. Injected so a test can record the report
        without touching the filesystem.
    """

    diagnostics: Diagnostics
    report_writer: ReportWriter = write_report


@dc.dataclass(frozen=True, slots=True)
class RunPolicyEnvironment:
    """The identity and stop account one run obeys.

    Attributes
    ----------
    run_id : cabc.Callable[[], str]
        Mints the identifier recorded in the report and the JSONL evidence.
        Defaults to `new_run_id`, so a caller that must name its own run can
        supply a fixed value.
    interrupts : InterruptSource | None
        Signal source, when the caller installs one. The library installs
        none of its own.
    cancellations : CancellationProbe
        Reads the supervising task's cancellation count.
    """

    run_id: cabc.Callable[[], str] = new_run_id
    interrupts: InterruptSource | None = None
    cancellations: CancellationProbe = dc.field(default_factory=TaskCancellations)


@dc.dataclass(frozen=True, slots=True)
class RunEnvironment:
    """The three purpose-shaped values one supervised run reads.

    Attributes
    ----------
    execution : ExecutionEnvironment
        Process boundary, samplers, and the shared clock.
    evidence : EvidenceEnvironment
        Live evidence destinations and the final report writer.
    policy : RunPolicyEnvironment
        Run identity, interrupt source, and cancellation probe.
    """

    execution: ExecutionEnvironment
    evidence: EvidenceEnvironment
    policy: RunPolicyEnvironment
