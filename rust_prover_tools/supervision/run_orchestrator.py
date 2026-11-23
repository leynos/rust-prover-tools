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

import asyncio
import contextlib
import dataclasses as dc
import typing as typ
import uuid

from rust_prover_tools.errors import ProverToolError
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
    CANCELLED_DETAIL,
    INTERRUPTED_DETAIL,
    NOT_RUN_DETAIL,
    ReportInput,
    RunLedger,
    assemble_report,
    not_run_result,
    result_for,
)

if typ.TYPE_CHECKING:
    import datetime as dt

    from rust_prover_tools.supervision.attempt_state import AttemptEvidence
    from rust_prover_tools.supervision.config import SupervisionConfig
    from rust_prover_tools.supervision.results import (
        DiagnosticRef,
        HarnessClassification,
        HarnessResult,
        RunReport,
    )
    from rust_prover_tools.supervision.run_environment import RunEnvironment

__all__ = (
    "SupervisedRun",
    "new_run_id",
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
        if self.environment.interrupts is not None:
            self.environment.interrupts.bind(asyncio.get_running_loop())
        ledger = RunLedger(expected=self.expected)
        self._emit_start(started_at, ledger)
        try:
            await self._run_all(ledger)
        except asyncio.CancelledError:
            # The caller cancelled the run. The active attempt has already
            # stopped and joined its group; account for the harnesses that
            # never completed, publish what evidence exists, then let the
            # cancellation continue.
            self._account_unrecorded(ledger, CANCELLED_DETAIL)
            ledger.interrupted = True
            with contextlib.suppress(ProverToolError):
                self._finalize(ledger, started_at)
            raise
        except Exception:
            # A failure the run did not classify, such as a monitor fault.
            # INV-FINALIZE still applies: publish what was observed, then let
            # the failure continue so the caller sees the real cause.
            self._account_unrecorded(ledger, NOT_RUN_DETAIL)
            with contextlib.suppress(ProverToolError):
                self._finalize(ledger, started_at)
            raise
        return self._finalize(ledger, started_at)

    def _account_unrecorded(self, ledger: RunLedger, detail: str) -> None:
        """Record every identity without a result as uncompleted work.

        The attempt that was in flight has already been stopped and joined by
        the supervision layer. Its own evidence is recorded first, so a
        cancelled or faulted run reports an interrupted attempt with its
        timing and stop reason, and only work that never started is reported
        as `not-run`.
        """
        recorded = len(ledger.results)
        ledger.results.extend(
            not_run_result(spec, detail) for spec in self.expected[recorded:]
        )

    def _emit_start(self, started_at: dt.datetime, ledger: RunLedger) -> None:
        """Write the run's opening evidence record, retaining a sink failure."""
        packages = tuple(dict.fromkeys(spec.package for spec in self.expected))
        try:
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
        except (OSError, ValueError) as error:
            ledger.diagnostic_error = error

    async def _run_all(self, ledger: RunLedger) -> None:
        """Run every expected identity, continuing past individual failures."""
        for spec in self.expected:
            if self._interrupted():
                ledger.interrupted = True
                ledger.interrupt_signal = self._signum()
                ledger.results.append(not_run_result(spec, INTERRUPTED_DETAIL))
                continue
            ledger.results.append(await self._run_one(spec, ledger))

    async def _run_one(self, spec: HarnessSpec, ledger: RunLedger) -> HarnessResult:
        """Run and record one harness invocation."""
        reference = None
        try:
            reference = self.environment.diagnostics.emit(
                harness_start_record(
                    run_id=self.run_id,
                    spec=spec,
                    timestamp=self.environment.clock.now(),
                )
            )
        except (OSError, ValueError) as error:
            ledger.diagnostic_error = error
        try:
            evidence, classification = await self._supervise(spec, ledger)
        except BaseException:
            self._record_interrupted(spec, ledger, reference)
            raise
        self._account(spec, ledger, evidence, classification)
        result = result_for(evidence, classification, reference)
        self._emit_finish(ledger, result)
        return result

    def _record_interrupted(
        self,
        spec: HarnessSpec,
        ledger: RunLedger,
        reference: DiagnosticRef | None,
    ) -> None:
        """Record the attempt a cancellation or fault stopped."""
        attempt = ledger.interrupted_attempt
        ledger.interrupted_attempt = None
        if attempt is None:
            return
        evidence, classification = typ.cast(
            "tuple[AttemptEvidence, HarnessClassification]", attempt
        )
        self._account(spec, ledger, evidence, classification)
        result = result_for(evidence, classification, reference)
        ledger.results.append(result)
        self._emit_finish(ledger, result)

    def _account(
        self,
        spec: HarnessSpec,
        ledger: RunLedger,
        evidence: AttemptEvidence,
        classification: HarnessClassification,
    ) -> None:
        """Account for an attempt's observed identities."""
        if evidence.stop_reason is StopReason.LAUNCH_FAILED:
            return
        if classification.observed is None:
            ledger.executed.append(spec.qualified)
        else:
            # Accounting compares what the verifier actually selected, not
            # only what was requested. A wrong, duplicated, or extra name
            # therefore shows up as drift even when a result was recorded.
            ledger.executed.extend(classification.observed)
        if evidence.stop_reason is StopReason.INTERRUPTED:
            ledger.interrupted = True
            ledger.interrupt_signal = self._signum()

    def _emit_finish(self, ledger: RunLedger, result: HarnessResult) -> None:
        """Emit one harness-finish record, retaining a sink failure."""
        try:
            self.environment.diagnostics.emit(
                harness_finish_record(run_id=self.run_id, result=result)
            )
        except (OSError, ValueError) as error:
            if ledger.diagnostic_error is None:
                ledger.diagnostic_error = error

    async def _supervise(
        self, spec: HarnessSpec, ledger: RunLedger
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
            adapter=self.environment.adapter,
            sleeper=self.environment.sleeper,
            interrupts=self.environment.interrupts,
        )
        try:
            return await supervisor.run()
        except BaseException:
            # The supervision layer stopped and joined the child before
            # re-raising. Keep the evidence of the attempt that was in flight
            # so the caller can report an interrupted attempt with its timing,
            # stop reason, and diagnostic reference.
            ledger.interrupted_attempt = supervisor.interrupted
            raise

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

    def _finalize(self, ledger: RunLedger, started_at: dt.datetime) -> RunReport:
        """Build, emit, and optionally write the run's final report."""
        signum = self._signum()
        if signum is not None:
            ledger.interrupted = True
            ledger.interrupt_signal = signum
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
        diagnostic_error = ledger.diagnostic_error or self.environment.diagnostics.error
        try:
            self.environment.diagnostics.emit(
                run_finish_record(
                    run_id=self.run_id,
                    timestamp=ledger.finished_at,
                    report=report,
                )
            )
            self.environment.diagnostics.flush()
        except (OSError, ValueError) as error:
            # The report destination is independent of the live sinks. Publish
            # the completed report first, then surface the first evidence
            # failure, whether it happened during the run or at finalization.
            if diagnostic_error is None:
                diagnostic_error = error
        if diagnostic_error is None:
            diagnostic_error = self.environment.diagnostics.error
        self._write_report(report)
        if diagnostic_error is not None:
            raise diagnostic_error
        return report

    def _write_report(self, report: RunReport) -> None:
        """Write the JSON report when a destination is configured."""
        path = self.config.sinks.report_path
        if path is not None:
            write_report(path, report)
