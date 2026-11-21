"""Run a sequence of harnesses under supervision and report what happened.

The runner is deliberately friendlier than a fail-fast batch. A prover failure
in one harness is evidence about that harness, not a reason to lose the
evidence about the other eighteen, so the loop continues unless the run itself
was interrupted. That is what makes a single run able to answer "which harness
consumed the memory" after a runner loss rather than merely "something failed".

Three rules bind the loop:

- The first harness a signal interrupts is reported as interrupted, and every
  harness that never ran is reported as ``not-run``. The run finalizes its
  report either way.
- Expected and executed identities are compared as multisets. Completeness is
  therefore a fact about identities, not a count.
- An adapter's classification is preserved verbatim. The runner decides the
  aggregate outcome; it never second-guesses a status.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
import uuid

from rust_prover_tools.supervision.attempt import AttemptSupervisor
from rust_prover_tools.supervision.events import (
    RunStart,
    harness_finish_record,
    harness_start_record,
    run_finish_record,
    run_start_record,
)
from rust_prover_tools.supervision.models import (
    HarnessSpec,
    StopReason,
)
from rust_prover_tools.supervision.report import write_report
from rust_prover_tools.supervision.run_finalize import (
    INTERRUPTED_DETAIL,
    ReportInput,
    assemble_report,
    not_run_result,
    result_for,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from rust_prover_tools.supervision.attempt_state import AttemptEvidence
    from rust_prover_tools.supervision.config import SupervisionConfig
    from rust_prover_tools.supervision.results import (
        HarnessClassification,
        HarnessResult,
        RunReport,
    )
    from rust_prover_tools.supervision.run_environment import RunEnvironment

__all__ = (
    "SupervisedRun",
    "new_run_id",
    "run_supervised",
    "run_supervised_sync",
)


def new_run_id() -> str:
    """Return a short unique run identifier.

    Returns
    -------
    str
        A hexadecimal identifier suitable for logs and report names.
    """
    return uuid.uuid4().hex[:16]


@dc.dataclass(slots=True)
class _RunLedger:
    """The accumulating facts of one run."""

    expected: tuple[HarnessSpec, ...]
    results: list[HarnessResult] = dc.field(default_factory=list)
    executed: list[str] = dc.field(default_factory=list)
    interrupted: bool = False
    interrupt_signal: int | None = None
    finished_at: dt.datetime | None = None


@dc.dataclass(slots=True)
class SupervisedRun:
    """Run one expected inventory and produce its report.

    Attributes
    ----------
    expected : tuple[HarnessSpec, ...]
        Identities the run must execute, in selection order.
    config : SupervisionConfig
        Per-harness policy.
    environment : RunEnvironment
        Injected dependencies.
    run_id : str
        Unique run identifier.
    """

    expected: tuple[HarnessSpec, ...]
    config: SupervisionConfig
    environment: RunEnvironment
    run_id: str = dc.field(default_factory=new_run_id)

    async def execute(self) -> RunReport:
        """Run every expected harness and return the typed report.

        Returns
        -------
        RunReport
            One result per expected identity, the accounting comparison, and
            the aggregate outcome.
        """
        started_at = self.environment.clock.now()
        ledger = _RunLedger(expected=self.expected)
        self._emit_start(started_at)
        await self._run_all(ledger)
        return self._finalize(ledger, started_at)

    def _emit_start(self, started_at: dt.datetime) -> None:
        """Write the run's opening evidence record."""
        packages = tuple(dict.fromkeys(spec.package for spec in self.expected))
        self.environment.diagnostics.emit(
            run_start_record(
                RunStart(
                    run_id=self.run_id,
                    timestamp=started_at,
                    packages=packages,
                    harness_count=len(self.expected),
                    config=self.config,
                )
            )
        )

    async def _run_all(self, ledger: _RunLedger) -> None:
        """Run every expected identity, continuing past individual failures."""
        for spec in self.expected:
            if self._interrupted():
                ledger.interrupted = True
                ledger.interrupt_signal = self._signum()
                ledger.results.append(not_run_result(spec, INTERRUPTED_DETAIL))
                continue
            ledger.results.append(await self._run_one(spec, ledger))

    async def _run_one(self, spec: HarnessSpec, ledger: _RunLedger) -> HarnessResult:
        """Run and record one harness invocation."""
        reference = self.environment.diagnostics.emit(
            harness_start_record(
                run_id=self.run_id,
                spec=spec,
                timestamp=self.environment.clock.now(),
            )
        )
        evidence, classification = await self._supervise(spec)
        ledger.executed.append(spec.qualified)
        if evidence.stop_reason is StopReason.INTERRUPTED:
            ledger.interrupted = True
            ledger.interrupt_signal = self._signum()
        result = result_for(evidence, classification, reference)
        self.environment.diagnostics.emit(
            harness_finish_record(run_id=self.run_id, result=result)
        )
        return result

    async def _supervise(
        self, spec: HarnessSpec
    ) -> tuple[AttemptEvidence, HarnessClassification]:
        """Run one attempt to completion, however it ends."""
        supervisor = AttemptSupervisor(
            spec=spec,
            config=self.config,
            clock=self.environment.clock,
            launcher=self.environment.launcher,
            probe=self.environment.probe,
            cgroup=self.environment.cgroup,
            diagnostics=self.environment.diagnostics,
            run_id=self.run_id,
            build=self.environment.build,
            classifier=self.environment.classifier,
            sleeper=self.environment.sleeper,
            interrupts=self.environment.interrupts,
        )
        return await supervisor.run()

    def _interrupted(self) -> bool:
        """Whether a signal has asked the run to stop."""
        return self.environment.interrupts is not None and (
            self.environment.interrupts.signum is not None
        )

    def _signum(self) -> int | None:
        """Return the signal that interrupted the run, when one did."""
        return (
            None
            if self.environment.interrupts is None
            else self.environment.interrupts.signum
        )

    def _finalize(self, ledger: _RunLedger, started_at: dt.datetime) -> RunReport:
        """Build, emit, and optionally write the run's final report."""
        ledger.finished_at = self.environment.clock.now()
        report = assemble_report(
            ReportInput(
                expected=self.expected,
                results=tuple(ledger.results),
                executed=tuple(ledger.executed),
                interrupted=ledger.interrupted,
                interrupt_signal=ledger.interrupt_signal,
                run_id=self.run_id,
                started_at=started_at,
                finished_at=ledger.finished_at,
            )
        )
        self.environment.diagnostics.emit(
            run_finish_record(
                run_id=self.run_id,
                timestamp=ledger.finished_at,
                report=report,
            )
        )
        self.environment.diagnostics.flush()
        self._write_report(report)
        return report

    def _write_report(self, report: RunReport) -> None:
        """Write the JSON report when a destination is configured."""
        path = self.config.sinks.report_path
        if path is not None:
            write_report(path, report)


async def run_supervised(
    expected: cabc.Sequence[HarnessSpec],
    config: SupervisionConfig,
    environment: RunEnvironment,
    *,
    run_id: str | None = None,
) -> RunReport:
    """Run every expected harness under supervision.

    Parameters
    ----------
    expected : cabc.Sequence[HarnessSpec]
        Identities the run must execute, in selection order.
    config : SupervisionConfig
        Per-harness policy.
    environment : RunEnvironment
        Injected dependencies.
    run_id : str | None
        Run identifier, or `None` to mint one.

    Returns
    -------
    RunReport
        The typed report with one result per expected identity.
    """
    run = SupervisedRun(
        expected=tuple(expected),
        config=config,
        environment=environment,
        run_id=run_id if run_id is not None else new_run_id(),
    )
    return await run.execute()


def run_supervised_sync(
    expected: cabc.Sequence[HarnessSpec],
    config: SupervisionConfig,
    environment: RunEnvironment,
    *,
    run_id: str | None = None,
) -> RunReport:
    """Run every expected harness under supervision, from synchronous code.

    Parameters
    ----------
    expected : cabc.Sequence[HarnessSpec]
        Identities the run must execute, in selection order.
    config : SupervisionConfig
        Per-harness policy.
    environment : RunEnvironment
        Injected dependencies.
    run_id : str | None
        Run identifier, or `None` to mint one.

    Returns
    -------
    RunReport
        The typed report with one result per expected identity.

    Raises
    ------
    RuntimeError
        Raised when called from inside a running event loop, where it cannot
        drive one of its own. An async caller must await `run_supervised`.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_supervised(expected, config, environment, run_id=run_id))
    msg = (
        "run_supervised_sync cannot run inside an event loop; await "
        "run_supervised instead"
    )
    raise RuntimeError(msg)
