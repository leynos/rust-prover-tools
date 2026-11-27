"""The injected dependencies of one supervised run.

A run needs a clock, a launcher, a resource probe, a cgroup reader, evidence
sinks, a command builder, an adapter classifier, an optional interrupt
source, and a run-ID source. Naming them together makes the composition root
explicit and lets a test replace any one without reaching into the runner.
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

__all__ = ("RunEnvironment",)


@dc.dataclass(frozen=True, slots=True)
class RunEnvironment:
    """Production dependencies for one supervised run.

    Attributes
    ----------
    clock : Clock
        Wall-clock and monotonic sources.
    launcher : CuprumHarnessLauncher
        Process boundary.
    probe : ResourceProbe
        Resource sampler.
    cgroup : CgroupReader
        Shared cgroup evidence reader.
    diagnostics : Diagnostics
        Live evidence destinations.
    build : CommandBuilder
        Composes the argv and environment for one identity.
    adapter : HarnessAdapter
        Prover adapter supplying the observer and the classifier.
    sleeper : Sleeper
        Pause between monitor ticks.
    interrupts : InterruptSource | None
        Signal source, when the caller installs one. The library installs
        none of its own.
    report_writer : ReportWriter
        Publishes the final report. Injected so a test can record the report
        without touching the filesystem.
    cancellations : CancellationProbe
        Reads the supervising task's cancellation count.
    run_id : cabc.Callable[[], str]
        Mints the identifier recorded in the report and the JSONL evidence.
        Defaults to `new_run_id`, so a caller that must name its own run can
        supply a fixed value.
    """

    clock: Clock
    launcher: HarnessLauncher
    probe: ResourceProbe
    cgroup: CgroupReader
    diagnostics: Diagnostics
    build: CommandBuilder
    adapter: HarnessAdapter
    sleeper: Sleeper
    interrupts: InterruptSource | None = None
    report_writer: ReportWriter = write_report
    cancellations: CancellationProbe = dc.field(default_factory=TaskCancellations)
    run_id: cabc.Callable[[], str] = new_run_id
