"""Emit one run's live evidence and publish its final report.

The supervision loop decides what happened; this collaborator decides what to
write about it. Keeping the two apart means the loop can read as the stop and
accounting rules, while every sink write and the report publication sit in one
place that a test can replace or make fail without touching the loop.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.events import (
    RunStart,
    harness_finish_record,
    harness_start_record,
    run_finish_record,
    run_start_record,
)
from rust_prover_tools.supervision.run_finalize import (
    ReportInput,
    RunLedger,
    assemble_report,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from rust_prover_tools.supervision.config import SupervisionConfig
    from rust_prover_tools.supervision.models import HarnessSpec
    from rust_prover_tools.supervision.protocols import Clock, ReportWriter
    from rust_prover_tools.supervision.results import (
        DiagnosticRef,
        HarnessResult,
        RunReport,
    )
    from rust_prover_tools.supervision.sinks import Diagnostics

__all__ = ("RunJournal",)


@dc.dataclass(slots=True)
class RunJournal:
    """Write one run's live records and publish its final report.

    Attributes
    ----------
    run_id : str
        Unique run identifier.
    config : SupervisionConfig
        Policy supplying the configured report destination.
    diagnostics : Diagnostics
        Live evidence destinations.
    report_writer : ReportWriter
        Publishes the final report.
    clock : Clock
        Wall-clock source for the closing record.
    """

    run_id: str
    config: SupervisionConfig
    diagnostics: Diagnostics
    report_writer: ReportWriter
    clock: Clock

    def emit_start(
        self,
        expected: cabc.Sequence[HarnessSpec],
        started_at: dt.datetime,
        ledger: RunLedger,
    ) -> None:
        """Write the run's opening evidence record, retaining a sink failure."""
        packages = tuple(dict.fromkeys(spec.package for spec in expected))
        try:
            self.diagnostics.emit(
                run_start_record(
                    RunStart(
                        run_id=self.run_id,
                        timestamp=started_at,
                        packages=packages,
                        harness_count=len(expected),
                        config=self.config,
                    )
                )
            )
        except (OSError, ValueError) as error:
            ledger.diagnostic_error = error

    def emit_harness_start(
        self,
        spec: HarnessSpec,
        timestamp: dt.datetime,
        ledger: RunLedger,
    ) -> DiagnosticRef | None:
        """Write one harness's opening record, retaining a sink failure."""
        try:
            return self.diagnostics.emit(
                harness_start_record(
                    run_id=self.run_id,
                    spec=spec,
                    timestamp=timestamp,
                )
            )
        except (OSError, ValueError) as error:
            ledger.diagnostic_error = error
            return None

    def emit_finish(self, result: HarnessResult, ledger: RunLedger) -> None:
        """Write one harness-finish record, retaining a sink failure."""
        try:
            self.diagnostics.emit(
                harness_finish_record(run_id=self.run_id, result=result)
            )
        except (OSError, ValueError) as error:
            if ledger.diagnostic_error is None:
                ledger.diagnostic_error = error

    def finalize(
        self,
        ledger: RunLedger,
        started_at: dt.datetime,
        expected: cabc.Sequence[HarnessSpec],
        signum: int | None,
    ) -> RunReport:
        """Build, emit, and optionally write the run's final report."""
        if signum is not None:
            ledger.interrupted = True
            ledger.interrupt_signal = signum
        ledger.finished_at = self.clock.now()
        report = assemble_report(
            ReportInput(
                expected=tuple(expected),
                results=tuple(ledger.results),
                executed=tuple(ledger.executed),
                interrupted=ledger.interrupted,
                interrupt_signal=ledger.interrupt_signal,
                run_id=self.run_id,
                started_at=started_at,
                finished_at=ledger.finished_at,
            )
        )
        diagnostic_error = ledger.diagnostic_error or self.diagnostics.error
        try:
            self.diagnostics.emit(
                run_finish_record(
                    run_id=self.run_id,
                    timestamp=ledger.finished_at,
                    report=report,
                )
            )
            self.diagnostics.flush()
        except (OSError, ValueError) as error:
            # The report destination is independent of the live sinks. Publish
            # the completed report first, then surface the first evidence
            # failure, whether it happened during the run or at finalization.
            if diagnostic_error is None:
                diagnostic_error = error
        if diagnostic_error is None:
            diagnostic_error = self.diagnostics.error
        self._write_report(report)
        if diagnostic_error is not None:
            raise diagnostic_error
        return report

    def _write_report(self, report: RunReport) -> None:
        """Write the JSON report when a destination is configured."""
        path = self.config.sinks.report_path
        if path is not None:
            self.report_writer(path, report)
