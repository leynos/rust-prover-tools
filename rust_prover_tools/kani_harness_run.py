"""Run a Kani harness inventory under supervision, through one qualified route.

This module is the composition root for the shared capability. It joins the
inventory, the exact selector, one qualified Kani route, the Kani log adapter,
and the generic supervision layer, and returns a typed report. It deliberately
does not own timing, measurement, evidence, or classification; each of those
belongs to the layer it composes.

The route is qualified once. Every harness invocation is derived from that one
validated installation, generation, environment, and grammar, so a run cannot
observe one compiler identity while launching another, and a concurrent
snapshot repair cannot swap the generation between harnesses.

Example
-------
```python
run = run_harnesses(
    KaniHarnessRunOptions(
        repo_root=Path.cwd(),
        packages=("cuprum-native-io",),
        route=LegacyRoute(
            installation=Path.home() / ".kani" / "kani-0.67.0",
            cwd=Path.cwd(),
        ),
    )
)
print(run.report.passed)
```
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_diagnostics import open_diagnostics
from rust_prover_tools.kani_harness_models import (
    KaniHarnessDependencies,
    KaniHarnessPreparation,
    KaniHarnessRunOptions,
)
from rust_prover_tools.kani_inventory import (
    HarnessInventory,
    InventoryHarness,
    cargo_metadata,
    discover_inventory,
    load_inventory,
    restrict_inventory,
    select_harnesses,
    verify_test_target_attribution,
)
from rust_prover_tools.kani_routes import (
    KaniRoute,
    LegacyRoute,
    SnapshotRoute,
    prepare_route,
    route_working_directory,
)
from rust_prover_tools.supervision import (
    AsyncSleeper,
    CgroupV2Reader,
    CuprumHarnessLauncher,
    Diagnostics,
    LaunchCommand,
    ProcfsResourceProbe,
    RunEnvironment,
    RunReport,
    SupervisionConfig,
    SystemClock,
    render_table,
    run_supervised_sync,
)
from rust_prover_tools.supervision.kani import KaniAdapter

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_inventory import CargoPackage
    from rust_prover_tools.supervision import HarnessSpec

__all__ = (
    "KaniHarnessDependencies",
    "KaniHarnessPreparation",
    "KaniHarnessRun",
    "KaniHarnessRunOptions",
    "KaniRoute",
    "LegacyRoute",
    "SnapshotRoute",
    "execute_harnesses",
    "prepare_harnesses",
    "run_harnesses",
    "run_kani_harnesses",
)


@dc.dataclass(frozen=True, slots=True)
class KaniHarnessRun:
    """One completed supervised Kani run.

    Attributes
    ----------
    report : RunReport
        Typed per-harness results and aggregate accounting.
    inventory : HarnessInventory
        Inventory the run was selected from.
    selected : tuple[InventoryHarness, ...]
        Harnesses the run intended to execute.
    backend_version : str
        Kani release the route was qualified against.
    route_kind : str
        ``legacy`` or ``snapshot``.
    """

    report: RunReport
    inventory: HarnessInventory
    selected: tuple[InventoryHarness, ...]
    backend_version: str
    route_kind: str

    @property
    def exit_status(self) -> int:
        """The CLI status this run implies.

        Returns
        -------
        int
            ``128 + signal`` for an interrupted run, zero only for a complete
            passing run, and one otherwise.
        """
        if self.report.interrupt_signal is not None:
            return 128 + self.report.interrupt_signal
        return 0 if self.report.passed else 1

    @property
    def table(self) -> tuple[str, ...]:
        """The deterministic human summary for this run."""
        return render_table(self.report)


def run_harnesses(options: KaniHarnessRunOptions) -> KaniHarnessRun:
    """Discover or load, select, and supervise Kani harnesses.

    This is preparation followed by execution, with no suspension point in
    between. An application that installs its own signal handlers should call
    `prepare_harnesses` and `execute_harnesses` separately, so the handlers
    cover only the phase whose in-flight children they can stop.

    Parameters
    ----------
    options : KaniHarnessRunOptions
        Repository, selection, route, and policy.

    Returns
    -------
    KaniHarnessRun
        The typed report and the inventory it was selected from.

    Raises
    ------
    InventoryDiscoveryError
        Raised when discovery cannot produce a complete inventory.
    InventoryFormatError
        Raised when a supplied inventory document is invalid.
    SelectionError
        Raised when the harness selection is ambiguous or overridden.
    ProverToolError
        Raised when the route cannot be qualified.
    UnsupportedKaniGrammarError
        Raised when the route's Kani release has no registered grammar.
    """
    return execute_harnesses(
        prepare_harnesses(options),
        options.config,
        options.dependencies,
    )


def run_kani_harnesses(options: KaniHarnessRunOptions) -> KaniHarnessRun:
    """Discover or load, select, and supervise Kani harnesses.

    This is the name the approved plan prescribes for the one-call entry point,
    and `run_harnesses` is kept as the shorter alias the package already
    exported. Both refer to the same implementation, so a consumer may use
    either without risking a different code path.

    Parameters
    ----------
    options : KaniHarnessRunOptions
        Repository, selection, route, and policy.

    Returns
    -------
    KaniHarnessRun
        The typed report and the inventory it was selected from.

    Raises
    ------
    InventoryDiscoveryError
        Raised when discovery cannot produce a complete inventory.
    InventoryFormatError
        Raised when a supplied inventory document is invalid.
    SelectionError
        Raised when the harness selection is ambiguous or overridden.
    ProverToolError
        Raised when the route cannot be qualified.
    UnsupportedKaniGrammarError
        Raised when the route's Kani release has no registered grammar.
    """
    return run_harnesses(options)


def prepare_harnesses(options: KaniHarnessRunOptions) -> KaniHarnessPreparation:
    """Discover or load, select, and qualify one route without running it.

    Parameters
    ----------
    options : KaniHarnessRunOptions
        Repository, selection, and route. The policy is not used.

    Returns
    -------
    KaniHarnessPreparation
        The inventory, selection, qualified route, and exact commands.

    Raises
    ------
    InventoryDiscoveryError
        Raised when discovery cannot produce a complete inventory.
    InventoryFormatError
        Raised when a supplied inventory document is invalid.
    SelectionError
        Raised when the harness selection is ambiguous or overridden.
    ProverToolError
        Raised when the route cannot be qualified.
    UnsupportedKaniGrammarError
        Raised when the route's Kani release has no registered grammar.
    """
    root = options.repo_root.resolve()
    route_cwd = route_working_directory(options.route).resolve()
    if route_cwd != root:
        msg = (
            "the route working directory must be the repository root: "
            f"{route_cwd} is not {root}"
        )
        raise ProverToolError(msg)
    inventory = _inventory(options)
    selected = select_harnesses(inventory, options.harnesses)
    _verify_test_targets(root, selected, options.metadata)
    dependencies = options.dependencies or KaniHarnessDependencies()
    qualifier = dependencies.prepare or prepare_route
    prepared = qualifier(options.route)
    return KaniHarnessPreparation(
        inventory=inventory,
        selected=selected,
        prepared=prepared,
        commands=_command_map(selected, prepared.build),
    )


def _verify_test_targets(
    root: Path,
    selected: cabc.Sequence[InventoryHarness],
    metadata: cabc.Sequence[CargoPackage] | None,
) -> None:
    """Refuse an unattributable test-target harness against the live tree.

    Kani 0.67.0 cannot select one integration-test target, so the check reads
    the same Cargo metadata the run will build from rather than trusting a
    possibly stale inventory. Packages without a selected test-target harness
    are not inspected.
    """
    if not any(harness.target_kind == "test" for harness in selected):
        return
    packages = tuple(metadata) if metadata is not None else cargo_metadata(root)
    verify_test_target_attribution(selected, packages)


def execute_harnesses(
    preparation: KaniHarnessPreparation,
    config: SupervisionConfig,
    dependencies: KaniHarnessDependencies | None = None,
    *,
    diagnostics: Diagnostics | None = None,
) -> KaniHarnessRun:
    """Supervise the harnesses selected by `prepare_harnesses`.

    Parameters
    ----------
    preparation : KaniHarnessPreparation
        Completed discovery, selection, and route qualification.
    config : SupervisionConfig
        Per-harness policy and configured evidence destinations.
    dependencies : KaniHarnessDependencies | None
        Overridden production dependencies. `None` uses the defaults.
    diagnostics : Diagnostics | None
        Caller-owned sinks, which override the configured destinations. The
        caller retains ownership and must flush and close them.

    Returns
    -------
    KaniHarnessRun
        The typed report and the inventory it was selected from.
    """
    dependencies = dependencies or KaniHarnessDependencies()
    with open_diagnostics(
        config,
        diagnostics or dependencies.diagnostics,
    ) as opened:
        environment = _environment(preparation, dependencies, opened)
        report = run_supervised_sync(
            tuple(harness.to_spec() for harness in preparation.selected),
            config,
            environment,
        )
    return KaniHarnessRun(
        report=report,
        inventory=preparation.inventory,
        selected=preparation.selected,
        backend_version=preparation.prepared.grammar.version,
        route_kind=preparation.prepared.kind,
    )


def _inventory(options: KaniHarnessRunOptions) -> HarnessInventory:
    """Load, discover, or narrow the inventory."""
    if options.inventory is not None:
        inventory = options.inventory
    elif options.inventory_path is not None:
        inventory = load_inventory(options.inventory_path)
    else:
        inventory = discover_inventory(
            options.repo_root,
            options.packages,
            metadata=options.metadata,
        )
    if not options.packages:
        return inventory
    return restrict_inventory(inventory, options.packages)


def _command_map(
    selected: cabc.Sequence[InventoryHarness],
    build: cabc.Callable[[InventoryHarness], LaunchCommand],
) -> dict[str, LaunchCommand]:
    """Compose one exact command per selected harness."""
    return {harness.identity: build(harness) for harness in selected}


def _environment(
    preparation: KaniHarnessPreparation,
    dependencies: KaniHarnessDependencies,
    diagnostics: Diagnostics,
) -> RunEnvironment:
    """Build the supervision environment around the prepared route."""
    clock = dependencies.clock or SystemClock()
    launcher = dependencies.launcher or CuprumHarnessLauncher(
        clock=clock.now,
        monotonic=clock.monotonic,
    )

    def build(spec: HarnessSpec) -> LaunchCommand:
        """Return the command composed for this exact identity."""
        return preparation.commands[spec.qualified]

    return RunEnvironment(
        clock=clock,
        launcher=launcher,
        probe=dependencies.probe or ProcfsResourceProbe(clock=clock),
        cgroup=dependencies.cgroup or CgroupV2Reader(),
        diagnostics=diagnostics,
        build=build,
        adapter=KaniAdapter(version=preparation.prepared.grammar.version),
        sleeper=dependencies.sleeper or AsyncSleeper(),
        interrupts=dependencies.interrupts,
    )
