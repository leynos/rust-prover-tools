"""Prover tooling for Rust repositories, centred on Kani.

The package exposes five public surfaces:

- ``kani_inventory`` discovers, selects, and serialises exact Kani
  harnesses, including package-qualified identities and ``#[path]`` and
  inline modules.
- ``kani_log`` parses Kani 0.67.0 reports with the one grammar the
  supervision layer also reads.
- ``supervision`` runs harnesses in owned process groups, streams live
  evidence, samples resources, applies deadlines and a host-memory
  watchdog, and publishes typed outcomes with a versioned report.
- ``kani_harness_run`` composes those pieces behind one library entry
  point; ``qualify_harnesses`` and ``execute_harnesses`` split discovery
  from execution so a caller's handlers cover exactly the phase whose
  children they can stop.
- ``kani_routes`` and ``kani_snapshot`` pin, install, and qualify the Kani
  release a run executes.

The CLI in ``cli`` binds the same API for ``kani harnesses list`` and
``kani harnesses run``. The package also hosts the Verus installation and
trust-audit helpers used by the wider toolchain.

The Kani and supervision names are resolved on first access, so
``import rust_prover_tools`` stays light: it neither imports the Kani grammar
registry nor runs that registry's import-time alignment check. Import the
owning module directly when a precise static type matters.
"""

from __future__ import annotations

import importlib
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_cli_grammar import UnsupportedKaniGrammarError
    from rust_prover_tools.kani_harness_run import (
        KaniExecutionDependencies,
        KaniHarnessPreparation,
        KaniHarnessRun,
        KaniHarnessRunOptions,
        KaniPreparationDependencies,
        execute_harnesses,
        prepare_harnesses,
        qualify_harnesses,
        run_harnesses,
        run_kani_harnesses,
    )
    from rust_prover_tools.kani_inventory import (
        HarnessInventory,
        InventoryDiscoveryError,
        InventoryFormatError,
        InventoryHarness,
        PackageInventory,
        SelectionError,
        compose_arguments,
        discover_inventory,
        inventory_document,
        load_inventory,
        restrict_inventory,
        select_harnesses,
        write_inventory,
    )
    from rust_prover_tools.kani_log import KaniLog, KaniLogError, parse_kani_log
    from rust_prover_tools.kani_routes import KaniRoute, LegacyRoute, SnapshotRoute
    from rust_prover_tools.supervision import (
        Diagnostics,
        ExecutionStatus,
        HarnessSpec,
        Measurement,
        ProofVerdict,
        RunReport,
        StopReason,
        SupervisionConfig,
        render_table,
        run_supervised,
        run_supervised_sync,
    )

PACKAGE_NAME = "rust_prover_tools"

try:  # pragma: no cover - Rust optional
    rust = importlib.import_module(f"._{PACKAGE_NAME}_rs", package=__name__)
    hello = typ.cast("cabc.Callable[[], str]", rust.hello)
except ModuleNotFoundError:  # pragma: no cover - Python fallback
    # The redundant alias marks this as a re-export: `__all__` is computed,
    # so Ruff cannot see that the name is public.
    from .pure import hello as hello

__all__ = [
    "Diagnostics",
    "ExecutionStatus",
    "HarnessInventory",
    "HarnessSpec",
    "InventoryDiscoveryError",
    "InventoryFormatError",
    "InventoryHarness",
    "KaniExecutionDependencies",
    "KaniHarnessPreparation",
    "KaniHarnessRun",
    "KaniHarnessRunOptions",
    "KaniLog",
    "KaniLogError",
    "KaniPreparationDependencies",
    "KaniRoute",
    "LegacyRoute",
    "Measurement",
    "PackageInventory",
    "ProofVerdict",
    "RunReport",
    "SelectionError",
    "SnapshotRoute",
    "StopReason",
    "SupervisionConfig",
    "UnsupportedKaniGrammarError",
    "compose_arguments",
    "discover_inventory",
    "execute_harnesses",
    "hello",
    "inventory_document",
    "load_inventory",
    "parse_kani_log",
    "prepare_harnesses",
    "qualify_harnesses",
    "render_table",
    "restrict_inventory",
    "run_harnesses",
    "run_kani_harnesses",
    "run_supervised",
    "run_supervised_sync",
    "select_harnesses",
    "write_inventory",
]

#: Public names and the module that owns each one, for the runtime resolver.
_LAZY_NAMES: typ.Final[dict[str, tuple[str, ...]]] = {
    "rust_prover_tools.kani_cli_grammar": ("UnsupportedKaniGrammarError",),
    "rust_prover_tools.kani_harness_run": (
        "KaniExecutionDependencies",
        "KaniHarnessPreparation",
        "KaniHarnessRun",
        "KaniHarnessRunOptions",
        "KaniPreparationDependencies",
        "execute_harnesses",
        "prepare_harnesses",
        "qualify_harnesses",
        "run_harnesses",
        "run_kani_harnesses",
    ),
    "rust_prover_tools.kani_inventory": (
        "HarnessInventory",
        "InventoryDiscoveryError",
        "InventoryFormatError",
        "InventoryHarness",
        "PackageInventory",
        "SelectionError",
        "compose_arguments",
        "discover_inventory",
        "inventory_document",
        "load_inventory",
        "restrict_inventory",
        "select_harnesses",
        "write_inventory",
    ),
    "rust_prover_tools.kani_log": ("KaniLog", "KaniLogError", "parse_kani_log"),
    "rust_prover_tools.kani_routes": ("KaniRoute", "LegacyRoute", "SnapshotRoute"),
    "rust_prover_tools.supervision": (
        "Diagnostics",
        "ExecutionStatus",
        "HarnessSpec",
        "Measurement",
        "ProofVerdict",
        "RunReport",
        "StopReason",
        "SupervisionConfig",
        "render_table",
        "run_supervised",
        "run_supervised_sync",
    ),
}

_OWNERS: typ.Final[dict[str, str]] = {
    name: module for module, names in _LAZY_NAMES.items() for name in names
}


def __getattr__(name: str) -> object:
    """Import and return one public name the first time it is requested.

    Parameters
    ----------
    name : str
        Public name, as listed in ``__all__``.

    Returns
    -------
    object
        The object the owning module defines under `name`.

    Raises
    ------
    AttributeError
        `name` is not one of the package's public names.
    """
    module_name = _OWNERS.get(name)
    if module_name is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value
