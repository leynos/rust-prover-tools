"""Requests and phase dependencies for one supervised Kani run.

The dataclasses live apart from the orchestration that consumes them so a
caller can construct a request, capture a preparation, and type a run without
importing the supervisor. Every type here is inert: no method starts a process
or opens a file.

The dependency overrides are split by phase. `KaniPreparationDependencies`
holds the seams discovery and route qualification touch, while
`KaniExecutionDependencies` holds the supervision seams. Each bag is resolved
once by the function that owns its phase, so a test replaces only the seams of
the phase under test and a caller cannot hand an execution seam to
qualification.

`KaniHarnessRunOptions` carries the request alone: repository, route, and
selection. Per-harness policy is an explicit `SupervisionConfig` argument of
the functions that run or execute a request, and each dependency bag is an
explicit argument of the operation that owns its phase, because neither a
deadline nor a seam is part of what to discover.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_inventory import (
        CargoPackage,
        HarnessInventory,
        InventoryHarness,
    )
    from rust_prover_tools.kani_routes import KaniRoute, PreparedRoute
    from rust_prover_tools.supervision import (
        Diagnostics,
        HarnessAdapter,
        LaunchCommand,
    )
    from rust_prover_tools.supervision.protocols import (
        CgroupReader,
        Clock,
        HarnessLauncher,
        InterruptSource,
        ResourceProbe,
        Sleeper,
    )

__all__ = (
    "KaniExecutionDependencies",
    "KaniHarnessPreparation",
    "KaniHarnessRunOptions",
    "KaniPreparationDependencies",
)


@dc.dataclass(frozen=True, slots=True)
class KaniPreparationDependencies:
    """Overrides for discovery and route qualification.

    Attributes
    ----------
    metadata : cabc.Callable[[Path], cabc.Sequence[CargoPackage]] | None
        Cargo metadata source. `None` runs `cargo metadata`; a caller that has
        already read it, or a test double, can supply another source.
    prepare : cabc.Callable[[KaniRoute], PreparedRoute] | None
        Route qualifier. `None` uses the production qualifier; a test can
        supply a prepared base without a real installation.
    environ : cabc.Mapping[str, str] | None
        Inherited environment for route qualification. `None` reads the
        process environment once in `qualify_harnesses`, so the default is
        resolved at the outermost caller rather than inside a probe.
    process_cwd : Path | None
        The caller's checked working directory, used when a snapshot route
        names none. `None` means the process directory, resolved once at the
        composition root before `prepare_route` is called.
    """

    metadata: cabc.Callable[[Path], cabc.Sequence[CargoPackage]] | None = None
    prepare: cabc.Callable[[KaniRoute], PreparedRoute] | None = None
    environ: cabc.Mapping[str, str] | None = None
    process_cwd: Path | None = None


@dc.dataclass(frozen=True, slots=True)
class KaniExecutionDependencies:
    """Overrides for supervising the harnesses a preparation selected.

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
    adapter : HarnessAdapter | None
        Prover adapter override. `None` builds the default `KaniAdapter` for
        the prepared route.
    """

    clock: Clock | None = None
    launcher: HarnessLauncher | None = None
    probe: ResourceProbe | None = None
    cgroup: CgroupReader | None = None
    sleeper: Sleeper | None = None
    diagnostics: Diagnostics | None = None
    interrupts: InterruptSource | None = None
    adapter: HarnessAdapter | None = None


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
    """

    repo_root: Path
    route: KaniRoute
    packages: tuple[str, ...] = ()
    harnesses: tuple[str, ...] = ()
    inventory: HarnessInventory | None = None
    inventory_path: Path | None = None

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
