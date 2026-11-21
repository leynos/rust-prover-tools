"""rust-prover-tools package."""

from __future__ import annotations

import importlib
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

from rust_prover_tools.kani_harness_run import (
    KaniHarnessDependencies,
    KaniHarnessRun,
    KaniHarnessRunOptions,
    run_harnesses,
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
from rust_prover_tools.kani_log import (
    KaniLog,
    KaniLogError,
    KaniLogReader,
    UnsupportedKaniGrammarError,
    parse,
)
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
    from .pure import hello

__all__ = [
    "Diagnostics",
    "ExecutionStatus",
    "HarnessInventory",
    "HarnessSpec",
    "InventoryDiscoveryError",
    "InventoryFormatError",
    "InventoryHarness",
    "KaniHarnessDependencies",
    "KaniHarnessRun",
    "KaniHarnessRunOptions",
    "KaniLog",
    "KaniLogError",
    "KaniLogReader",
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
    "hello",
    "inventory_document",
    "load_inventory",
    "parse",
    "render_table",
    "restrict_inventory",
    "run_harnesses",
    "run_supervised",
    "run_supervised_sync",
    "select_harnesses",
    "write_inventory",
]
