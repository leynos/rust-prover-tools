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

from rust_prover_tools.supervision.attempt import AttemptSupervisor
from rust_prover_tools.supervision.models import (
    HarnessSpec,
    StopReason,
)
from rust_prover_tools.supervision.run_finalize import (
    CANCELLED_DETAIL,
    INTERRUPTED_DETAIL,
    NOT_RUN_DETAIL,
    RunLedger,
    not_run_result,
    result_for,
)
from rust_prover_tools.supervision.run_journal import RunJournal

if typ.TYPE_CHECKING:
    import datetime as dt

    from rust_prover_tools.supervision.attempt_evidence import AttemptEvidence
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
        The execution, evidence, and policy values the run reads.
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
        execution = self.environment.execution
        interrupts = self.environment.policy.interrupts
        started_at = execution.clock.now()
        if interrupts is not None:
            interrupts.bind(asyncio.get_running_loop())
        ledger = RunLedger(expected=self.expected)
        self._journal_for().emit_start(self.expected, started_at, ledger)
        try:
            await self._run_all(ledger)
        except asyncio.CancelledError:
            # The caller cancelled the run. The active attempt has already
            # stopped and joined its group; account for the harnesses that
            # never completed, publish what evidence exists, then let the
            # cancellation continue.
            self._account_unrecorded(ledger, CANCELLED_DETAIL)
            ledger.interrupted = True
            # Publication failures must not replace the cancellation that is
            # already unwinding this run.
            with contextlib.suppress(Exception):
                self._finalize(ledger, started_at)
            raise
        except Exception:
            # A failure the run did not classify, such as a monitor fault.
            # INV-FINALIZE still applies: publish what was observed, then let
            # the failure continue so the caller sees the real cause.
            self._account_unrecorded(ledger, NOT_RUN_DETAIL)
            # Likewise, keep the original monitor or runner failure visible.
            with contextlib.suppress(Exception):
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

    def _journal_for(self) -> RunJournal:
        """Build the evidence writer for this run."""
        return RunJournal(
            run_id=self.run_id,
            config=self.config,
            diagnostics=self.environment.evidence.diagnostics,
            report_writer=self.environment.evidence.report_writer,
            clock=self.environment.execution.clock,
        )

    def _emit_finish(self, ledger: RunLedger, result: HarnessResult) -> None:
        """Emit one harness-finish record, retaining a sink failure."""
        self._journal_for().emit_finish(result, ledger)

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
        reference = self._journal_for().emit_harness_start(
            spec, self.environment.execution.clock.now(), ledger
        )
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

    async def _supervise(
        self, spec: HarnessSpec, ledger: RunLedger
    ) -> tuple[AttemptEvidence, HarnessClassification]:
        """Run one attempt to completion, however it ends."""
        execution = self.environment.execution
        policy = self.environment.policy
        supervisor = AttemptSupervisor(
            spec=spec,
            config=self.config,
            clock=execution.clock,
            launcher=execution.launcher,
            probe=execution.probe,
            cgroup=execution.cgroup,
            diagnostics=self.environment.evidence.diagnostics,
            run_id=self.run_id,
            build=execution.build,
            adapter=execution.adapter,
            sleeper=execution.sleeper,
            interrupts=policy.interrupts,
            cancellations=policy.cancellations,
        )
        try:
            return await supervisor.run()
        except BaseException:
            # The supervision layer stopped and joined the child before
            # re-raising. Keep the evidence of the attempt that was in flight
            # so the caller can report an interrupted attempt with its timing,
            # stop reason, and diagnostic reference.
            ledger.interrupted_attempt = supervisor.attempt
            raise

    def _interrupted(self) -> bool:
        """Whether a signal has asked the run to stop."""
        interrupts = self.environment.policy.interrupts
        return interrupts is not None and interrupts.signum is not None

    def _signum(self) -> int | None:
        """Return the signal that interrupted the run, when one did."""
        interrupts = self.environment.policy.interrupts
        return None if interrupts is None else interrupts.signum

    def _finalize(self, ledger: RunLedger, started_at: dt.datetime) -> RunReport:
        """Build, emit, and optionally write the run's final report."""
        return self._journal_for().finalize(
            ledger,
            started_at,
            self.expected,
            self._signum(),
        )
