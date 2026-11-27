"""Requests and preparation values for one supervised Kani run.

The dataclasses live apart from the orchestration that consumes them so a
caller can construct a request, capture a preparation, and type a run without
importing the supervisor. Every type here is inert: no method starts a process
or opens a file.

`KaniHarnessDependencies` deliberately stays one bag rather than splitting into
process, resource, and sink groups. Every field is an optional override of the
same production environment, resolved in one place; splitting the bag would
duplicate that defaulting, and every test that needs two overrides would
thread two values. The class is the composition point, not a capability
boundary, and the module's inertness is what keeps one bag honest.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision import SupervisionConfig

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_inventory import (
        CargoPackage,
        HarnessInventory,
        InventoryHarness,
    )
    from rust_prover_tools.kani_routes import KaniRoute, PreparedRoute
    from rust_prover_tools.supervision import Diagnostics, LaunchCommand
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
    "KaniHarnessPreparation",
    "KaniHarnessRunOptions",
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
    metadata : cabc.Callable[[Path], cabc.Sequence[CargoPackage]] | None
        Cargo metadata source. `None` runs `cargo metadata`; a caller that has
        already read it, or a test double, can supply another source.
    environ : cabc.Mapping[str, str] | None
        Inherited environment for route qualification. `None` reads the
        process environment once in `prepare_harnesses`, so the default is
        resolved at the outermost caller rather than inside a probe.
    """

    clock: Clock | None = None
    launcher: HarnessLauncher | None = None
    probe: ResourceProbe | None = None
    cgroup: CgroupReader | None = None
    sleeper: Sleeper | None = None
    diagnostics: Diagnostics | None = None
    interrupts: InterruptSource | None = None
    prepare: cabc.Callable[[KaniRoute], PreparedRoute] | None = None
    metadata: cabc.Callable[[Path], cabc.Sequence[CargoPackage]] | None = None
    environ: cabc.Mapping[str, str] | None = None


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
class KaniHarnessPreparation:
    """Discovery, selection, and route qualification completed before a run.

    Preparation does no supervision and opens no evidence destination. A
    caller that owns signal policy can therefore bracket only the execution
    phase, leaving discovery and qualification under the application's normal
    signal disposition.

    Attributes
    ----------
    inventory : HarnessInventory
        Inventory the selection was resolved from.
    selected : tuple[InventoryHarness, ...]
        Harnesses the run intends to execute, in inventory order.
    prepared : PreparedRoute
        Qualified route every harness command is derived from.
    commands : collections.abc.Mapping[str, LaunchCommand]
        One exact command per selected identity.
    """

    inventory: HarnessInventory
    selected: tuple[InventoryHarness, ...]
    prepared: PreparedRoute
    commands: cabc.Mapping[str, LaunchCommand]
