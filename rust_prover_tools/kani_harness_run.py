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

from rust_prover_tools.kani_diagnostics import open_diagnostics
from rust_prover_tools.kani_inventory import (
    CargoPackage,
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryHarness,
    discover_inventory,
    load_inventory,
    select_harnesses,
)
from rust_prover_tools.kani_routes import (
    KaniRoute,
    LegacyRoute,
    PreparedRoute,
    SnapshotRoute,
    prepare_route,
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

    from rust_prover_tools.supervision import HarnessSpec
    from rust_prover_tools.supervision.protocols import (
        CgroupReader,
        Clock,
        HarnessLauncher,
        InterruptSource,
        ResourceProbe,
        Sleeper,
    )

__all__ = (
    "KaniHarnessDependencies",
    "KaniHarnessRun",
    "KaniHarnessRunOptions",
    "KaniRoute",
    "LegacyRoute",
    "SnapshotRoute",
    "run_harnesses",
)


@dc.dataclass(frozen=True, slots=True)
class KaniHarnessDependencies:
    """Production dependencies a caller may override.

    Attributes
    ----------
    clock : Clock | None
        Time sources; defaults to `SystemClock`.
    launcher : HarnessLauncher | None
        Process boundary; defaults to the Cuprum launcher.
    probe : ResourceProbe | None
        Resource sampler; defaults to the Linux procfs probe.
    cgroup : CgroupReader | None
        Shared cgroup reader; defaults to cgroup v2 discovery.
    sleeper : Sleeper | None
        Monitor pause; defaults to the asyncio sleeper.
    diagnostics : Diagnostics | None
        Live evidence sinks; defaults to the configured paths.
    interrupts : InterruptSource | None
        Signal source. The library installs none of its own.
    prepare : cabc.Callable[[KaniRoute], PreparedRoute] | None
        Route qualifier. `None` uses the production qualifier; a test can
        supply a prepared base without a real installation.
    """

    clock: Clock | None = None
    launcher: HarnessLauncher | None = None
    probe: ResourceProbe | None = None
    cgroup: CgroupReader | None = None
    sleeper: Sleeper | None = None
    diagnostics: Diagnostics | None = None
    interrupts: InterruptSource | None = None
    prepare: cabc.Callable[[KaniRoute], PreparedRoute] | None = None


@dc.dataclass(frozen=True, slots=True)
class KaniHarnessRunOptions:
    """One supervised Kani run.

    Attributes
    ----------
    repo_root : Path
        Repository or workspace root.
    packages : tuple[str, ...]
        Packages to discover. An empty tuple selects every package.
    harnesses : tuple[str, ...]
        Pretty names to run. An empty tuple selects every discovered harness.
    route : KaniRoute
        Qualified legacy release or immutable snapshot.
    inventory : HarnessInventory | None
        Authoritative inventory, overriding discovery.
    inventory_path : Path | None
        Path to an authoritative version-1 inventory document.
    config : SupervisionConfig
        Per-harness policy and report destination.
    metadata : cabc.Sequence[CargoPackage] | None
        Pre-read Cargo metadata, for tests.
    dependencies : KaniHarnessDependencies | None
        Overridden production dependencies.
    """

    repo_root: Path
    route: KaniRoute
    packages: tuple[str, ...] = ()
    harnesses: tuple[str, ...] = ()
    inventory: HarnessInventory | None = None
    inventory_path: Path | None = None
    config: SupervisionConfig = dc.field(default_factory=SupervisionConfig)
    metadata: cabc.Sequence[CargoPackage] | None = None
    dependencies: KaniHarnessDependencies | None = None

    def __post_init__(self) -> None:
        """Refuse two authoritative inventories or an unusable route."""
        if self.inventory is not None and self.inventory_path is not None:
            msg = "provide either inventory or inventory_path, not both"
            raise ValueError(msg)


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
    inventory = _inventory(options)
    selected = select_harnesses(inventory, options.harnesses)
    dependencies = options.dependencies or KaniHarnessDependencies()
    qualifier = dependencies.prepare or prepare_route
    prepared = qualifier(options.route)
    commands = _command_map(selected, prepared.build)
    with open_diagnostics(options.config, dependencies.diagnostics) as diagnostics:
        environment = _environment(options, prepared, commands, diagnostics)
        report = run_supervised_sync(
            tuple(harness.to_spec() for harness in selected),
            options.config,
            environment,
        )
    return KaniHarnessRun(
        report=report,
        inventory=inventory,
        selected=selected,
        backend_version=prepared.grammar.version,
        route_kind=prepared.kind,
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
    return _restrict(inventory, options.packages)


def _restrict(
    inventory: HarnessInventory,
    packages: cabc.Sequence[str],
) -> HarnessInventory:
    """Narrow an inventory to the requested packages."""
    by_name = {package.package: package for package in inventory.packages}
    unknown = [name for name in packages if name not in by_name]
    if unknown:
        msg = f"the inventory does not contain packages {unknown}"
        raise InventoryDiscoveryError(msg)
    return HarnessInventory(tuple(by_name[name] for name in packages))


def _command_map(
    selected: cabc.Sequence[InventoryHarness],
    build: cabc.Callable[[InventoryHarness], LaunchCommand],
) -> dict[str, LaunchCommand]:
    """Compose one exact command per selected harness."""
    return {harness.identity: build(harness) for harness in selected}


def _environment(
    options: KaniHarnessRunOptions,
    prepared: PreparedRoute,
    commands: cabc.Mapping[str, LaunchCommand],
    diagnostics: Diagnostics,
) -> RunEnvironment:
    """Build the supervision environment around the prepared route."""
    dependencies = options.dependencies or KaniHarnessDependencies()
    clock = dependencies.clock or SystemClock()
    launcher = dependencies.launcher or CuprumHarnessLauncher(
        clock=clock.now,
        monotonic=clock.monotonic,
    )

    def build(spec: HarnessSpec) -> LaunchCommand:
        """Return the command composed for this exact identity."""
        return commands[spec.qualified]

    return RunEnvironment(
        clock=clock,
        launcher=launcher,
        probe=dependencies.probe or ProcfsResourceProbe(clock=clock),
        cgroup=dependencies.cgroup or CgroupV2Reader(),
        diagnostics=diagnostics,
        build=build,
        adapter=KaniAdapter(version=prepared.grammar.version),
        sleeper=dependencies.sleeper or AsyncSleeper(),
        interrupts=dependencies.interrupts,
    )
