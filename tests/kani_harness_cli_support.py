"""Shared fixtures for the `kani harnesses` CLI contract tests."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import typing as typ

from rust_prover_tools.kani_harness_run import (
    KaniHarnessDependencies,
    KaniHarnessPreparation,
    KaniHarnessRun,
    KaniHarnessRunOptions,
)
from rust_prover_tools.kani_inventory import (
    HarnessInventory,
    InventoryHarness,
    PackageInventory,
    write_inventory,
)
from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    HarnessResult,
    HarnessSpec,
    InventoryDrift,
    Measurement,
    ProofVerdict,
    RunOutcome,
    RunReport,
    StopReason,
    SupervisionConfig,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest


__all__ = (
    "CapturedRun",
    "capture_run",
    "inventory_file",
    "make_inventory",
    "result",
    "run",
)


def make_inventory() -> HarnessInventory:
    """Return a two-package inventory."""
    return HarnessInventory((
        PackageInventory(
            "alpha",
            (
                InventoryHarness(
                    package="alpha",
                    target="alpha",
                    target_kind="lib",
                    harness="harnesses::verified",
                    source="src/harnesses.rs",
                    line=5,
                    attributes=("kani::proof",),
                ),
            ),
        ),
        PackageInventory(
            "beta",
            (
                InventoryHarness(
                    package="beta",
                    target="beta",
                    target_kind="bin",
                    harness="in_main",
                    source="src/main.rs",
                    line=3,
                    attributes=("kani::proof",),
                ),
            ),
        ),
    ))


def result(status: ExecutionStatus, spec: HarnessSpec) -> HarnessResult:
    """Return one result for `spec` with the given status."""
    return HarnessResult(
        spec=spec,
        status=status,
        stop_reason=StopReason.EXITED,
        exit_code=0 if status is ExecutionStatus.PASSED else 1,
        signal_number=None,
        proof_verdict=(
            ProofVerdict.PASSED
            if status is ExecutionStatus.PASSED
            else ProofVerdict.FAILED
        ),
        started_at=None,
        finished_at=None,
        duration_seconds=1.0,
        peak_group_rss=Measurement.available(1),
        min_host_available=Measurement.available(2),
        host_total=Measurement.available(3),
        cgroup=None,
        last_output=None,
        last_progress=None,
        diagnostic=None,
        detail="",
    )


def run(
    *,
    status: ExecutionStatus = ExecutionStatus.PASSED,
    interrupt_signal: int | None = None,
) -> KaniHarnessRun:
    """Return a minimal typed run for the CLI adapter."""
    inventory = make_inventory()
    selected = (inventory.packages[0].harnesses[0],)
    current = selected[0].to_spec()
    report = RunReport(
        schema_version=1,
        run_id="r" * 16,
        started_at=dt.datetime.now(tz=dt.UTC),
        finished_at=None,
        expected=(current,),
        results=(result(status, current),),
        accounting=InventoryDrift(),
        outcome=(
            RunOutcome.INTERRUPTED
            if interrupt_signal is not None
            else (
                RunOutcome.PASSED
                if status is ExecutionStatus.PASSED
                else RunOutcome.FAILED
            )
        ),
        interrupted=interrupt_signal is not None,
        interrupt_signal=interrupt_signal,
    )
    return KaniHarnessRun(
        report=report,
        inventory=inventory,
        selected=selected,
        backend_version="0.67.0",
        route_kind="legacy",
    )


def inventory_file(tmp_path: Path) -> Path:
    """Write the test inventory as a strict document."""
    path = tmp_path / "inventory.json"
    write_inventory(path, make_inventory())
    return path


@dc.dataclass(frozen=True, slots=True)
class CapturedRun:
    """The composition one CLI invocation handed to execution.

    Attributes
    ----------
    options : KaniHarnessRunOptions
        Repository, selection, route, and policy the CLI resolved.
    dependencies : KaniHarnessDependencies
        Sinks and interrupt source the CLI supplied to execution.
    """

    options: KaniHarnessRunOptions
    dependencies: KaniHarnessDependencies


def capture_run(
    monkeypatch: pytest.MonkeyPatch,
    run: KaniHarnessRun,
) -> list[CapturedRun]:
    """Patch preparation and execution, returning what execution received."""
    captured: list[CapturedRun] = []
    pending: list[KaniHarnessRunOptions] = []

    def fake_prepare(options: KaniHarnessRunOptions) -> KaniHarnessPreparation:
        """Capture the options and hand execution an opaque preparation."""
        pending.append(options)
        return typ.cast("KaniHarnessPreparation", object())

    def fake_execute(
        preparation: KaniHarnessPreparation,
        config: SupervisionConfig,
        dependencies: KaniHarnessDependencies | None = None,
        *,
        diagnostics: Diagnostics | None = None,
    ) -> KaniHarnessRun:
        """Capture the execution inputs and return the configured run."""
        del preparation, config, diagnostics
        captured.append(
            CapturedRun(
                pending.pop(),
                dependencies or KaniHarnessDependencies(),
            )
        )
        return run

    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli.prepare_harnesses", fake_prepare
    )
    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli.execute_harnesses", fake_execute
    )
    return captured
